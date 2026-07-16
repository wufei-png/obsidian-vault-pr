"""Integrity validation for immutable source captures."""

from __future__ import annotations

import hashlib
import codecs
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from .errors import IngestError
from .snapshot_bundle import ASSET_MIMES, PRIMARY_MIMES, SUPPLEMENT_MIMES
from .source_files import _detected_mime

class CaptureError(RuntimeError):
    """Raised when a source capture cannot be trusted."""


SOURCE_ID_PATTERN = re.compile(r"src_[0-9a-f]{20}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
CAPTURE_FIELDS = {
    "schema_version",
    "source_id",
    "source",
    "captured_at",
    "title",
    "primary",
    "files",
    "derived",
    "acquisition",
}
FILE_FIELDS = {"path", "sha256", "size", "mime"}
SUPPLEMENT_FIELDS = FILE_FIELDS | {"role"}
PROVIDER_FIELDS = {"name", "version", "acquisition"}


def source_identity_digest(payload: dict[str, Any]) -> str:
    """Hash the canonical immutable-source identity payload."""
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def source_identity_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return the manifest fields that define an immutable source version."""
    source = manifest.get("source")
    primary = manifest.get("primary")
    files = manifest.get("files")
    if not isinstance(source, dict):
        source = {}
    identity_source = {
        key: source.get(key)
        for key in ("kind", "access_kind", "requested_url", "final_url")
        if source.get(key) is not None
    }
    return {
        "schema_version": 3,
        "source": identity_source,
        "primary": primary,
        "files": sorted(
            files if isinstance(files, list) else [],
            key=lambda item: (
                str(item.get("path", "")) if isinstance(item, dict) else ""
            ),
        ),
    }


def _artifact_path(raw: Any, label: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise CaptureError(f"capture manifest has invalid {label} path")
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        raise CaptureError(f"capture manifest {label} path escapes its directory")
    return path


def _vault_path(vault: Path, raw: Any, label: str) -> Path:
    relative = _artifact_path(raw, label)
    path = vault / relative
    current = vault
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise CaptureError(f"capture manifest {label} traverses a symbolic link")
    return path


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _capture_root_descriptor(capture_dir: Path) -> int:
    try:
        before = capture_dir.lstat()
    except OSError as exc:
        raise CaptureError("capture path is not a regular directory") from exc
    if not stat.S_ISDIR(before.st_mode):
        raise CaptureError("capture path is not a regular directory")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(capture_dir, flags)
    except OSError as exc:
        raise CaptureError("capture path is not a regular directory") from exc
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or _identity(before) != _identity(opened):
        os.close(descriptor)
        raise CaptureError("capture path changed during validation")
    return descriptor


def _read_capture_file(
    root_descriptor: int,
    relative: Path,
    *,
    include_content: bool = False,
) -> tuple[str, tuple[int, int], int, str, bytes | None]:
    """Hash one capture file through pinned, no-follow directory descriptors."""
    directory_descriptor = os.dup(root_descriptor)
    try:
        for part in relative.parts[:-1]:
            try:
                before = os.stat(
                    part,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise CaptureError(
                    f"capture artifact is missing: {relative}"
                ) from exc
            if not stat.S_ISDIR(before.st_mode):
                raise CaptureError(
                    f"capture artifact parent is not a regular directory: {relative}"
                )
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(
                os, "O_DIRECTORY", 0
            )
            try:
                child_descriptor = os.open(
                    part, flags, dir_fd=directory_descriptor
                )
            except OSError as exc:
                raise CaptureError(
                    f"capture artifact parent changed during validation: {relative}"
                ) from exc
            opened = os.fstat(child_descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or _identity(before) != _identity(opened)
            ):
                os.close(child_descriptor)
                raise CaptureError(
                    f"capture artifact parent changed during validation: {relative}"
                )
            os.close(directory_descriptor)
            directory_descriptor = child_descriptor

        name = relative.name
        try:
            before = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise CaptureError(f"capture artifact is missing: {relative}") from exc
        if not stat.S_ISREG(before.st_mode):
            raise CaptureError(f"capture artifact is missing: {relative}")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            file_descriptor = os.open(
                name, flags, dir_fd=directory_descriptor
            )
        except OSError as exc:
            raise CaptureError(f"capture artifact is missing: {relative}") from exc
        digest = hashlib.sha256()
        content = bytearray() if include_content else None
        prefix = bytearray()
        utf8_decoder = codecs.getincrementaldecoder("utf-8")("strict")
        utf8_valid = True
        try:
            opened = os.fstat(file_descriptor)
            identity = _identity(opened)
            if (
                not stat.S_ISREG(opened.st_mode)
                or _identity(before) != identity
            ):
                raise CaptureError(
                    f"capture artifact changed during validation: {relative}"
                )
            while chunk := os.read(file_descriptor, 65_536):
                digest.update(chunk)
                if len(prefix) < 8192:
                    prefix.extend(chunk[: 8192 - len(prefix)])
                if utf8_valid:
                    try:
                        utf8_decoder.decode(chunk, final=False)
                    except UnicodeDecodeError:
                        utf8_valid = False
                if content is not None:
                    content.extend(chunk)
            if utf8_valid:
                try:
                    utf8_decoder.decode(b"", final=True)
                except UnicodeDecodeError:
                    utf8_valid = False
        finally:
            os.close(file_descriptor)
        try:
            detected_mime = _detected_mime(
                relative.name,
                bytes(prefix),
                utf8_valid=utf8_valid,
            )
        except IngestError as exc:
            raise CaptureError(str(exc)) from exc
        return (
            digest.hexdigest(),
            identity,
            opened.st_size,
            detected_mime,
            bytes(content) if content is not None else None,
        )
    finally:
        os.close(directory_descriptor)


def _read_manifest(
    root_descriptor: int, capture_dir: Path
) -> tuple[dict[str, Any], str, tuple[int, int]]:
    (
        manifest_hash,
        manifest_identity,
        _manifest_size,
        _manifest_mime,
        content,
    ) = _read_capture_file(
        root_descriptor,
        Path("manifest.json"),
        include_content=True,
    )
    assert content is not None
    try:
        manifest = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureError("capture directory is incomplete") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 3
        or set(manifest) != CAPTURE_FIELDS
    ):
        raise CaptureError("capture manifest schema is invalid")
    return manifest, manifest_hash, manifest_identity


def _file_record(
    raw: object,
    *,
    label: str,
    with_role: bool = False,
) -> tuple[Path, str, int, str, str | None]:
    expected_fields = SUPPLEMENT_FIELDS if with_role else FILE_FIELDS
    if not isinstance(raw, dict) or set(raw) != expected_fields:
        raise CaptureError(f"capture manifest {label} record is invalid")
    path = _artifact_path(raw.get("path"), label)
    digest = raw.get("sha256")
    size = raw.get("size")
    mime = raw.get("mime")
    role = raw.get("role") if with_role else None
    if (
        not isinstance(digest, str)
        or not SHA256_PATTERN.fullmatch(digest)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or not isinstance(mime, str)
        or not mime.strip()
        or (
            with_role
            and (
                not isinstance(role, str)
                or role not in {"asset", "supplement"}
            )
        )
    ):
        raise CaptureError(f"capture manifest {label} metadata is invalid")
    return path, digest, size, mime, role


def _validate_source_and_acquisition(
    source: dict[str, Any],
    acquisition: dict[str, Any],
) -> None:
    kind = source.get("kind")
    access_kind = source.get("access_kind")
    adapter = acquisition.get("adapter")
    if not all(isinstance(value, str) for value in (kind, access_kind, adapter)):
        raise CaptureError("capture source and acquisition kinds are invalid")
    expected_source_fields = {
        "kind",
        "access_kind",
        *(
            {"logical_name"}
            if kind == "local_file"
            else {"requested_url", "final_url"}
        ),
    }
    if "supersedes" in source:
        expected_source_fields.add("supersedes")
    if set(source) != expected_source_fields:
        raise CaptureError("capture source fields do not match its kind")
    expected_adapter_access = {
        "url": ("http", "public"),
        "local_file": ("local_file", "local"),
    }
    if kind == "snapshot_bundle":
        if adapter not in {"wechat", "authenticated"} or access_kind != {
            "wechat": "public",
            "authenticated": "authenticated",
        }[adapter]:
            raise CaptureError("capture acquisition does not match its source kind")
    elif expected_adapter_access.get(kind) != (adapter, access_kind):
        raise CaptureError("capture acquisition does not match its source kind")
    if set(acquisition) not in ({"adapter"}, {"adapter", "provider"}):
        raise CaptureError("capture acquisition fields are invalid")
    provider = acquisition.get("provider")
    if provider is not None:
        if (
            kind != "snapshot_bundle"
            or not isinstance(provider, dict)
            or set(provider) != PROVIDER_FIELDS
            or not all(
                isinstance(provider.get(field), str)
                and provider[field].strip() == provider[field]
                and 0 < len(provider[field]) <= 500
                for field in PROVIDER_FIELDS
            )
        ):
            raise CaptureError("capture provider metadata is invalid")


def _validate_capture_tree(
    root_descriptor: int,
    *,
    expected_files: set[Path],
    expected_directories: set[Path],
) -> None:
    """Reject undeclared or non-regular content under a pinned capture root."""
    maximum_entries = len(expected_files) + len(expected_directories) - 1
    visited_entries = 0
    seen_files: set[Path] = set()

    def inspect_directory(
        directory_descriptor: int, relative_directory: Path
    ) -> None:
        nonlocal visited_entries
        names: list[str] = []
        with os.scandir(directory_descriptor) as entries:
            for entry in entries:
                visited_entries += 1
                if visited_entries > maximum_entries:
                    raise CaptureError("capture contains undeclared content")
                names.append(entry.name)
        for name in sorted(names):
            try:
                metadata = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise CaptureError(
                    "capture changed during validation"
                ) from exc
            relative = relative_directory / name
            if stat.S_ISDIR(metadata.st_mode):
                if relative not in expected_directories:
                    raise CaptureError(
                        f"capture contains undeclared content: {relative}/"
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
                    raise CaptureError(
                        f"capture directory changed during validation: {relative}"
                    ) from exc
                try:
                    opened = os.fstat(child_descriptor)
                    if (
                        not stat.S_ISDIR(opened.st_mode)
                        or _identity(metadata) != _identity(opened)
                    ):
                        raise CaptureError(
                            f"capture directory changed during validation: {relative}"
                        )
                    inspect_directory(child_descriptor, relative)
                finally:
                    os.close(child_descriptor)
                continue
            if not stat.S_ISREG(metadata.st_mode) or relative not in expected_files:
                raise CaptureError(
                    f"capture contains undeclared content: {relative}"
                )
            seen_files.add(relative)

    inspect_directory(root_descriptor, Path())
    missing = sorted(str(path) for path in expected_files - seen_files)
    if missing:
        raise CaptureError(
            "capture is missing declared content: " + ", ".join(missing)
        )


def _load_validated_capture(
    capture_dir: Path,
    *,
    source_id: str,
    vault: Path,
    expected_manifest: dict[str, Any] | None,
    content_artifact: str | None,
) -> tuple[dict[str, Any], bytes | None]:
    if not SOURCE_ID_PATTERN.fullmatch(source_id):
        raise CaptureError("invalid source id")
    root_descriptor = _capture_root_descriptor(capture_dir)
    requested_content: bytes | None = None
    try:
        manifest, manifest_hash, manifest_identity = _read_manifest(
            root_descriptor, capture_dir
        )
        if manifest.get("source_id") != source_id:
            raise CaptureError(
                "capture manifest source id does not match its directory"
            )
        source = manifest.get("source")
        acquisition = manifest.get("acquisition")
        derived = manifest.get("derived")
        files = manifest.get("files")
        if (
            not isinstance(source, dict)
            or set(source)
            - {
                "kind",
                "access_kind",
                "requested_url",
                "final_url",
                "logical_name",
                "supersedes",
            }
            or not isinstance(source.get("kind"), str)
            or source["kind"] not in {"url", "local_file", "snapshot_bundle"}
            or not isinstance(source.get("access_kind"), str)
            or source["access_kind"] not in {
                "public",
                "authenticated",
                "local",
            }
            or not isinstance(acquisition, dict)
            or set(acquisition) - {"adapter", "provider"}
            or not isinstance(derived, dict)
            or set(derived) != {"extracted"}
            or not isinstance(files, list)
        ):
            raise CaptureError("capture manifest metadata is invalid")
        _validate_source_and_acquisition(source, acquisition)
        if source["kind"] == "local_file":
            if not isinstance(source.get("logical_name"), str) or not source[
                "logical_name"
            ].strip():
                raise CaptureError("local capture logical_name is invalid")
            if "requested_url" in source or "final_url" in source:
                raise CaptureError("local capture must not contain URL fields")
        else:
            for field in ("requested_url", "final_url"):
                if not isinstance(source.get(field), str) or not source[field]:
                    raise CaptureError(f"capture source {field} is invalid")
        supersedes = source.get("supersedes")
        if supersedes is not None and (
            not isinstance(supersedes, str)
            or not SOURCE_ID_PATTERN.fullmatch(supersedes)
            or supersedes == source_id
        ):
            raise CaptureError("capture source supersedes is invalid")
        identity = source_identity_payload(manifest)
        expected_source_id = f"src_{source_identity_digest(identity)[:20]}"
        if source_id != expected_source_id:
            raise CaptureError(
                "capture source id does not match its immutable identity"
            )
        if expected_manifest is not None and manifest != expected_manifest:
            raise CaptureError("existing capture metadata does not match")

        primary_path, primary_hash, primary_size, primary_mime, _role = _file_record(
            manifest.get("primary"), label="primary"
        )
        extracted_path, extracted_hash, extracted_size, extracted_mime, _role = _file_record(
            derived.get("extracted"), label="extracted"
        )
        if primary_mime not in PRIMARY_MIMES:
            raise CaptureError("capture primary MIME type is unsupported")
        if extracted_mime != "text/markdown":
            raise CaptureError("capture extracted artifact must be Markdown")
        expected_records: dict[Path, tuple[str, int]] = {
            primary_path: (primary_hash, primary_size),
            extracted_path: (extracted_hash, extracted_size),
        }
        for index, raw_file in enumerate(files):
            path, digest, size, mime, role = _file_record(
                raw_file,
                label=f"files[{index}]",
                with_role=True,
            )
            allowed = ASSET_MIMES if role == "asset" else SUPPLEMENT_MIMES
            if mime not in allowed:
                raise CaptureError(
                    f"capture files[{index}] MIME type is invalid for its role"
                )
            if path in expected_records:
                raise CaptureError(
                    f"capture manifest has duplicate artifact path: {path}"
                )
            expected_records[path] = (digest, size)

        requested_path: Path | None = None
        if content_artifact is not None:
            if content_artifact == "extracted":
                requested_path = extracted_path
            elif content_artifact == "primary":
                requested_path = primary_path
            else:
                raise CaptureError(
                    f"capture manifest has unknown artifact: {content_artifact}"
                )

        expected_files = {Path("manifest.json"), *expected_records}
        expected_directories = {Path(".")}
        for relative in expected_files:
            parent = relative.parent
            while parent != Path("."):
                expected_directories.add(parent)
                parent = parent.parent
        _validate_capture_tree(
            root_descriptor,
            expected_files=expected_files,
            expected_directories=expected_directories,
        )

        seen_identities: set[tuple[int, int]] = set()
        for relative, (expected_hash, expected_size) in expected_records.items():
            (
                actual_hash,
                file_identity,
                actual_size,
                actual_mime,
                content,
            ) = _read_capture_file(
                root_descriptor,
                relative,
                include_content=relative == requested_path,
            )
            if file_identity in seen_identities:
                raise CaptureError(
                    f"capture manifest has physically duplicate artifacts: {relative}"
                )
            seen_identities.add(file_identity)
            if actual_hash != expected_hash:
                raise CaptureError(
                    f"capture artifact integrity failed: {relative}"
                )
            if actual_size != expected_size:
                raise CaptureError(f"capture artifact size failed: {relative}")
            expected_mime = next(
                record[3]
                for record in [
                    _file_record(manifest["primary"], label="primary"),
                    _file_record(
                        manifest["derived"]["extracted"], label="extracted"
                    ),
                    *[
                        _file_record(
                            raw,
                            label=f"files[{index}]",
                            with_role=True,
                        )
                        for index, raw in enumerate(manifest["files"])
                    ],
                ]
                if record[0] == relative
            )
            if actual_mime != expected_mime:
                raise CaptureError(
                    f"capture artifact MIME type failed: {relative}"
                )
            if relative == requested_path:
                requested_content = content

        (
            final_manifest_hash,
            final_manifest_identity,
            _final_manifest_size,
            _final_manifest_mime,
            _content,
        ) = _read_capture_file(root_descriptor, Path("manifest.json"))
        if (
            final_manifest_hash != manifest_hash
            or final_manifest_identity != manifest_identity
        ):
            raise CaptureError("capture manifest changed during validation")
    finally:
        os.close(root_descriptor)

    return manifest, requested_content


def load_validated_capture(
    capture_dir: Path,
    *,
    source_id: str,
    vault: Path,
    expected_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load a capture only after verifying identity, files, and source-note linkage."""
    manifest, _content = _load_validated_capture(
        capture_dir,
        source_id=source_id,
        vault=vault,
        expected_manifest=expected_manifest,
        content_artifact=None,
    )
    return manifest


def load_validated_capture_artifact_text(
    capture_dir: Path,
    *,
    source_id: str,
    vault: Path,
    artifact: str,
) -> tuple[dict[str, Any], str]:
    """Validate a capture and decode one artifact read through the pinned tree."""
    manifest, content = _load_validated_capture(
        capture_dir,
        source_id=source_id,
        vault=vault,
        expected_manifest=None,
        content_artifact=artifact,
    )
    assert content is not None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CaptureError(
            f"capture {artifact} artifact is not valid UTF-8"
        ) from exc
    return manifest, text
