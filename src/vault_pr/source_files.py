"""Bounded, race-resistant reads of untrusted local source files."""

from __future__ import annotations

import codecs
import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from .errors import IngestError

FileIdentity = tuple[int, int]
MAX_ASSET_DEPTH = 32
STREAM_CHUNK_BYTES = 65_536


@dataclass(frozen=True)
class InspectedSourceFile:
    """Facts computed from one pinned regular-file descriptor."""

    sha256: str
    size: int
    mime: str
    identity: FileIdentity
    content: bytes | None = None


def _identity(metadata: os.stat_result) -> FileIdentity:
    return metadata.st_dev, metadata.st_ino


def _regular_file_descriptor(path: Path, label: str) -> int:
    """Open one stable regular file without following its final symlink."""
    try:
        before = path.lstat()
    except OSError as exc:
        raise IngestError(f"{label} file does not exist") from exc
    if stat.S_ISLNK(before.st_mode):
        raise IngestError(f"{label} file must not be a symbolic link")
    if not stat.S_ISREG(before.st_mode):
        raise IngestError(f"{label} path is not a regular file")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise IngestError(f"unable to open {label} file") from exc
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or _identity(before) != _identity(opened)
    ):
        os.close(descriptor)
        raise IngestError(f"{label} path changed or is not a regular file")
    return descriptor


def read_bounded_regular_file(
    path: Path, *, max_bytes: int, label: str
) -> bytes:
    """Read at most one byte beyond the configured limit from a regular file."""
    descriptor = _regular_file_descriptor(path, label)
    with os.fdopen(descriptor, "rb") as file:
        body = file.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise IngestError(f"{label} exceeds {max_bytes} bytes")
    return body


def sha256_regular_file_identity(
    path: Path, label: str
) -> tuple[str, FileIdentity]:
    """Hash one stable regular file and return its physical identity."""
    descriptor = _regular_file_descriptor(path, label)
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as file:
        identity = _identity(os.fstat(file.fileno()))
        while chunk := file.read(65_536):
            digest.update(chunk)
    return digest.hexdigest(), identity


def sha256_regular_file(path: Path, label: str) -> str:
    """Hash one stable regular file without following a final symlink."""
    return sha256_regular_file_identity(path, label)[0]


def _detected_mime(name: str, prefix: bytes, *, utf8_valid: bool) -> str:
    suffix = Path(name).suffix.casefold()
    if prefix.startswith(b"%PDF-") and suffix == ".pdf":
        return "application/pdf"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n") and suffix == ".png":
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff") and suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if prefix.startswith((b"GIF87a", b"GIF89a")) and suffix == ".gif":
        return "image/gif"
    if (
        len(prefix) >= 12
        and prefix[:4] == b"RIFF"
        and prefix[8:12] == b"WEBP"
        and suffix == ".webp"
    ):
        return "image/webp"
    if not utf8_valid:
        raise IngestError(f"source file type is unsupported or mismatched: {name}")
    if suffix in {".md", ".markdown"}:
        return "text/markdown"
    if suffix in {".html", ".htm"}:
        lowered = prefix.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
        if not any(
            marker in lowered
            for marker in (
                b"<!doctype html",
                b"<html",
                b"<head",
                b"<body",
                b"<main",
                b"<article",
            )
        ):
            raise IngestError(f"source file type is unsupported or mismatched: {name}")
        return "text/html"
    if suffix == ".txt":
        return "text/plain"
    if suffix == ".json":
        return "application/json"
    raise IngestError(f"source file type is unsupported or mismatched: {name}")


def inspect_open_regular_file(
    descriptor: int,
    *,
    name: str,
    max_bytes: int,
    label: str,
    destination: Path | None = None,
    include_content: bool = False,
) -> InspectedSourceFile:
    """Stream one already-pinned file while hashing, sizing, typing, and copying."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise IngestError(f"{label} byte limit is invalid")
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise IngestError(f"{label} path is not a regular file")
    if metadata.st_size > max_bytes:
        raise IngestError(f"{label} exceeds {max_bytes} bytes")

    digest = hashlib.sha256()
    identity = _identity(metadata)
    size = 0
    prefix = bytearray()
    content = bytearray() if include_content else None
    utf8_decoder = codecs.getincrementaldecoder("utf-8")("strict")
    utf8_valid = True
    destination_descriptor: int | None = None
    destination_created = False
    if destination is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            destination_descriptor = os.open(destination, flags, 0o600)
            destination_created = True
        except OSError as exc:
            raise IngestError(f"unable to create copied {label} file") from exc

    try:
        while chunk := os.read(descriptor, STREAM_CHUNK_BYTES):
            size += len(chunk)
            if size > max_bytes:
                raise IngestError(f"{label} exceeds {max_bytes} bytes")
            digest.update(chunk)
            if len(prefix) < 4096:
                prefix.extend(chunk[: 4096 - len(prefix)])
            if content is not None:
                content.extend(chunk)
            if utf8_valid:
                try:
                    utf8_decoder.decode(chunk)
                except UnicodeDecodeError:
                    utf8_valid = False
            if destination_descriptor is not None:
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_descriptor, view)
                    view = view[written:]
        if utf8_valid:
            try:
                utf8_decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                utf8_valid = False
        final_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(final_metadata.st_mode)
            or _identity(final_metadata) != identity
            or final_metadata.st_size != size
            or final_metadata.st_mtime_ns != metadata.st_mtime_ns
            or final_metadata.st_ctime_ns != metadata.st_ctime_ns
        ):
            raise IngestError(f"{label} changed during inspection")
        mime = _detected_mime(name, bytes(prefix), utf8_valid=utf8_valid)
        if destination_descriptor is not None:
            os.fchmod(destination_descriptor, 0o644)
        return InspectedSourceFile(
            sha256=digest.hexdigest(),
            size=size,
            mime=mime,
            identity=identity,
            content=bytes(content) if content is not None else None,
        )
    except Exception:
        if destination_created and destination is not None:
            destination.unlink(missing_ok=True)
        raise
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)


def inspect_regular_file(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    destination: Path | None = None,
    include_content: bool = False,
) -> InspectedSourceFile:
    """Inspect and optionally copy one regular source file without buffering it all."""
    descriptor = _regular_file_descriptor(path, label)
    try:
        return inspect_open_regular_file(
            descriptor,
            name=path.name,
            max_bytes=max_bytes,
            label=label,
            destination=destination,
            include_content=include_content,
        )
    finally:
        os.close(descriptor)


def inspect_regular_file_at(
    directory_descriptor: int,
    name: str,
    *,
    display_name: str,
    max_bytes: int,
    label: str,
    destination: Path | None = None,
    include_content: bool = False,
) -> InspectedSourceFile:
    """Inspect a child file through a pinned directory descriptor."""
    try:
        before = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise IngestError(f"{label} file does not exist: {display_name}") from exc
    if stat.S_ISLNK(before.st_mode):
        raise IngestError(f"{label} file must not be a symbolic link")
    if not stat.S_ISREG(before.st_mode):
        raise IngestError(f"{label} path is not a regular file: {display_name}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise IngestError(f"unable to open {label} file: {display_name}") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _identity(before) != _identity(opened)
        ):
            raise IngestError(
                f"{label} path changed or is not a regular file: {display_name}"
            )
        return inspect_open_regular_file(
            descriptor,
            name=display_name,
            max_bytes=max_bytes,
            label=label,
            destination=destination,
            include_content=include_content,
        )
    finally:
        os.close(descriptor)


def _directory_descriptor(
    path: Path, label: str, expected: os.stat_result | None = None
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise IngestError(f"{label} must be a regular directory") from exc
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or (
        expected is not None and _identity(expected) != _identity(opened)
    ):
        os.close(descriptor)
        raise IngestError(f"{label} changed or is not a regular directory")
    return descriptor


def copy_snapshot_assets(
    snapshot_path: Path,
    output_dir: Path,
    *,
    max_files: int,
    max_bytes: int,
    forbidden_identities: set[FileIdentity] | None = None,
) -> dict[str, str]:
    """Copy sibling image assets through pinned directory and file descriptors."""
    images = snapshot_path.parent / "images"
    try:
        images_metadata = images.lstat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise IngestError("unable to inspect snapshot image directory") from exc
    if not stat.S_ISDIR(images_metadata.st_mode):
        raise IngestError("snapshot image directory must be a regular directory")

    destination = output_dir / "images"
    if destination.is_symlink() or destination.exists():
        raise IngestError("snapshot assets duplicate an extracted artifact path")

    root_descriptor = _directory_descriptor(
        images, "snapshot image directory", images_metadata
    )
    hashes: dict[str, str] = {}
    seen_identities = set(forbidden_identities or ())
    visited_entries = 0
    copied_bytes = 0

    def copy_directory(directory_descriptor: int, relative_dir: Path) -> None:
        nonlocal visited_entries, copied_bytes
        if len(relative_dir.parts) > MAX_ASSET_DEPTH:
            raise IngestError(
                f"snapshot assets exceed the {MAX_ASSET_DEPTH}-directory depth limit"
            )
        names: list[str] = []
        with os.scandir(directory_descriptor) as entries:
            for entry in entries:
                visited_entries += 1
                if visited_entries > max_files:
                    raise IngestError(
                        f"snapshot assets exceed the {max_files}-entry limit"
                    )
                names.append(entry.name)
        for name in sorted(names):
            try:
                metadata = os.stat(
                    name, dir_fd=directory_descriptor, follow_symlinks=False
                )
            except OSError as exc:
                raise IngestError("snapshot asset changed during capture") from exc
            relative = relative_dir / name
            if stat.S_ISLNK(metadata.st_mode):
                raise IngestError("snapshot image directory must not contain symlinks")
            if stat.S_ISDIR(metadata.st_mode):
                flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(
                    os, "O_DIRECTORY", 0
                )
                try:
                    child_descriptor = os.open(
                        name, flags, dir_fd=directory_descriptor
                    )
                except OSError as exc:
                    raise IngestError(
                        "snapshot asset directory changed during capture"
                    ) from exc
                try:
                    opened_directory = os.fstat(child_descriptor)
                    if (
                        not stat.S_ISDIR(opened_directory.st_mode)
                        or _identity(metadata) != _identity(opened_directory)
                    ):
                        raise IngestError(
                            "snapshot asset directory changed during capture"
                        )
                    copy_directory(child_descriptor, relative)
                finally:
                    os.close(child_descriptor)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise IngestError("snapshot assets must be regular files")

            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                source_descriptor = os.open(
                    name, flags, dir_fd=directory_descriptor
                )
            except OSError as exc:
                raise IngestError("snapshot asset changed during capture") from exc
            copied_asset = destination / relative
            copied_asset.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            try:
                opened_file = os.fstat(source_descriptor)
                opened_identity = _identity(opened_file)
                if (
                    not stat.S_ISREG(opened_file.st_mode)
                    or _identity(metadata) != opened_identity
                ):
                    raise IngestError("snapshot asset changed during capture")
                if opened_identity in seen_identities:
                    raise IngestError(
                        "snapshot assets physically alias another capture artifact"
                    )
                seen_identities.add(opened_identity)
                destination_flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                destination_descriptor = os.open(
                    copied_asset, destination_flags, 0o600
                )
                try:
                    while chunk := os.read(source_descriptor, 65_536):
                        copied_bytes += len(chunk)
                        if copied_bytes > max_bytes:
                            raise IngestError(
                                f"snapshot assets exceed the {max_bytes}-byte limit"
                            )
                        digest.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(destination_descriptor, view)
                            view = view[written:]
                    os.fchmod(destination_descriptor, 0o644)
                finally:
                    os.close(destination_descriptor)
            finally:
                os.close(source_descriptor)
            hashes[(Path("images") / relative).as_posix()] = digest.hexdigest()

    try:
        copy_directory(root_descriptor, Path())
        return hashes
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        os.close(root_descriptor)


def materialize_regular_tree(
    root: Path,
    expected_hashes: dict[Path, str],
    *,
    destination: Path | None = None,
) -> None:
    """Validate an exact regular-file tree and optionally copy only declared files."""
    expected_files = set(expected_hashes)
    expected_directories = {Path(".")}
    for relative in expected_files:
        parent = relative.parent
        while parent != Path("."):
            expected_directories.add(parent)
            parent = parent.parent

    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise IngestError("unable to inspect staged capture directory") from exc
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise IngestError("staged capture must be a regular directory")
    root_descriptor = _directory_descriptor(
        root, "staged capture directory", root_metadata
    )
    seen_files: set[Path] = set()
    seen_identities: set[FileIdentity] = set()
    if destination is not None:
        destination.mkdir(parents=True, exist_ok=False)
    visited_entries = 0
    maximum_entries = len(expected_files) + len(expected_directories) - 1

    def inspect_directory(directory_descriptor: int, relative_dir: Path) -> None:
        nonlocal visited_entries
        names: list[str] = []
        with os.scandir(directory_descriptor) as entries:
            for entry in entries:
                visited_entries += 1
                if visited_entries > maximum_entries:
                    raise IngestError("staged capture contains undeclared content")
                names.append(entry.name)
        for name in sorted(names):
            try:
                metadata = os.stat(
                    name, dir_fd=directory_descriptor, follow_symlinks=False
                )
            except OSError as exc:
                raise IngestError("staged capture changed during validation") from exc
            relative = relative_dir / name
            if stat.S_ISLNK(metadata.st_mode):
                raise IngestError(
                    f"staged capture contains a symbolic link: {relative}"
                )
            if stat.S_ISDIR(metadata.st_mode):
                if relative not in expected_directories:
                    raise IngestError(
                        f"staged capture contains undeclared content: {relative}/"
                    )
                flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(
                    os, "O_DIRECTORY", 0
                )
                try:
                    child_descriptor = os.open(
                        name, flags, dir_fd=directory_descriptor
                    )
                except OSError as exc:
                    raise IngestError(
                        "staged capture directory changed during validation"
                    ) from exc
                try:
                    opened_directory = os.fstat(child_descriptor)
                    if (
                        not stat.S_ISDIR(opened_directory.st_mode)
                        or _identity(metadata) != _identity(opened_directory)
                    ):
                        raise IngestError(
                            "staged capture directory changed during validation"
                        )
                    inspect_directory(child_descriptor, relative)
                finally:
                    os.close(child_descriptor)
                continue
            if not stat.S_ISREG(metadata.st_mode) or relative not in expected_files:
                raise IngestError(
                    f"staged capture contains undeclared content: {relative}"
                )

            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                source_descriptor = os.open(
                    name, flags, dir_fd=directory_descriptor
                )
            except OSError as exc:
                raise IngestError(
                    f"staged capture file changed during validation: {relative}"
                ) from exc
            destination_descriptor: int | None = None
            digest = hashlib.sha256()
            try:
                opened_file = os.fstat(source_descriptor)
                opened_identity = _identity(opened_file)
                if (
                    not stat.S_ISREG(opened_file.st_mode)
                    or _identity(metadata) != opened_identity
                ):
                    raise IngestError(
                        f"staged capture file changed during validation: {relative}"
                    )
                if opened_identity in seen_identities:
                    raise IngestError(
                        f"staged capture has physically duplicate artifacts: {relative}"
                    )
                seen_identities.add(opened_identity)
                if destination is not None:
                    copied = destination / relative
                    copied.parent.mkdir(parents=True, exist_ok=True)
                    destination_flags = (
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0)
                    )
                    destination_descriptor = os.open(
                        copied, destination_flags, 0o600
                    )
                while chunk := os.read(source_descriptor, 65_536):
                    digest.update(chunk)
                    if destination_descriptor is not None:
                        view = memoryview(chunk)
                        while view:
                            written = os.write(destination_descriptor, view)
                            view = view[written:]
                if digest.hexdigest() != expected_hashes[relative]:
                    raise IngestError(
                        f"staged capture integrity failed: {relative}"
                    )
                if destination_descriptor is not None:
                    os.fchmod(destination_descriptor, 0o644)
            finally:
                if destination_descriptor is not None:
                    os.close(destination_descriptor)
                os.close(source_descriptor)
            seen_files.add(relative)

    try:
        inspect_directory(root_descriptor, Path())
        missing = sorted(str(path) for path in expected_files - seen_files)
        if missing:
            raise IngestError(
                "staged capture is missing declared content: " + ", ".join(missing)
            )
    except Exception:
        if destination is not None:
            shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        os.close(root_descriptor)
