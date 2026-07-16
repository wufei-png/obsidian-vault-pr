"""Validation of untrusted provider-produced local Snapshot Bundles."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import IngestError
from .source_files import (
    FileIdentity,
    InspectedSourceFile,
    MAX_ASSET_DEPTH,
    _directory_descriptor,
    _identity,
    inspect_regular_file,
    inspect_regular_file_at,
)

SNAPSHOT_MANIFEST_NAME = "snapshot-manifest.json"
SNAPSHOT_MANIFEST_MAX_BYTES = 1_000_000

_MANIFEST_FIELDS = {"schema_version", "primary", "files", "provider"}
_FILE_FIELDS = {"path", "sha256", "size", "mime"}
_SECONDARY_FILE_FIELDS = _FILE_FIELDS | {"role"}
_PROVIDER_FIELDS = {"name", "version", "acquisition"}
_SHA256 = re.compile(r"[0-9a-f]{64}")

PRIMARY_MIMES = frozenset(
    {
        "text/html",
        "text/markdown",
        "application/pdf",
    }
)
ASSET_MIMES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "application/pdf",
    }
)
SUPPLEMENT_MIMES = frozenset(
    {
        *PRIMARY_MIMES,
        *ASSET_MIMES,
        "text/plain",
        "application/json",
    }
)


class SnapshotBundleError(IngestError):
    """Raised when a local Snapshot Bundle does not satisfy its manifest."""


def _relative_path(raw: object, label: str) -> Path:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\0" in raw:
        raise SnapshotBundleError(f"{label} path is invalid")
    pure = PurePosixPath(raw)
    if (
        pure.is_absolute()
        or pure == PurePosixPath(".")
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != raw
    ):
        raise SnapshotBundleError(f"{label} path is invalid")
    path = Path(*pure.parts)
    if path == Path(SNAPSHOT_MANIFEST_NAME):
        raise SnapshotBundleError(f"{label} cannot replace {SNAPSHOT_MANIFEST_NAME}")
    return path


def _bounded_string(raw: object, label: str) -> str:
    if not isinstance(raw, str):
        raise SnapshotBundleError(f"{label} must be a string")
    value = " ".join(raw.split()).strip()
    if not value or len(value) > 500 or raw != value:
        raise SnapshotBundleError(f"{label} must be between 1 and 500 characters")
    return value


def _file_record(
    raw: object,
    *,
    label: str,
    with_role: bool,
) -> dict[str, Any]:
    expected_fields = _SECONDARY_FILE_FIELDS if with_role else _FILE_FIELDS
    if not isinstance(raw, dict) or set(raw) != expected_fields:
        raise SnapshotBundleError(f"{label} record has unknown or missing fields")
    path = _relative_path(raw.get("path"), label)
    digest = raw.get("sha256")
    size = raw.get("size")
    mime = raw.get("mime")
    role = raw.get("role") if with_role else None
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise SnapshotBundleError(f"{label} sha256 is invalid")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise SnapshotBundleError(f"{label} size is invalid")
    if not isinstance(mime, str) or not mime.strip() or mime != mime.strip():
        raise SnapshotBundleError(f"{label} MIME type is invalid")
    if role is not None and (
        not isinstance(role, str) or role not in {"asset", "supplement"}
    ):
        raise SnapshotBundleError(f"{label} role must be asset or supplement")
    allowed_mimes = (
        PRIMARY_MIMES
        if role is None
        else ASSET_MIMES
        if role == "asset"
        else SUPPLEMENT_MIMES
    )
    if mime not in allowed_mimes:
        raise SnapshotBundleError(f"{label} MIME type is not allowed for its role")
    result: dict[str, Any] = {
        "path": path.as_posix(),
        "sha256": digest,
        "size": size,
        "mime": mime,
    }
    if role is not None:
        result["role"] = role
    return result


def _provider_record(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != _PROVIDER_FIELDS:
        raise SnapshotBundleError("snapshot provider has unknown or missing fields")
    return {
        field: _bounded_string(raw.get(field), f"snapshot provider {field}")
        for field in sorted(_PROVIDER_FIELDS)
    }


def inspect_single_snapshot(
    path: Path,
    *,
    max_bytes: int,
    destination: Path | None = None,
) -> dict[str, Any]:
    """Inspect a standalone local primary document without retaining its path."""
    try:
        inspected = inspect_regular_file(
            path,
            max_bytes=max_bytes,
            label="snapshot",
            destination=destination,
        )
    except IngestError as exc:
        raise SnapshotBundleError(str(exc)) from exc
    if inspected.mime not in PRIMARY_MIMES:
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise SnapshotBundleError("snapshot primary MIME type is unsupported")
    return {
        "path": path.name,
        "sha256": inspected.sha256,
        "size": inspected.size,
        "mime": inspected.mime,
    }


def _open_child_directory(
    parent_descriptor: int,
    name: str,
    before: os.stat_result,
    relative: Path,
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise SnapshotBundleError(
            f"snapshot bundle directory changed during validation: {relative}"
        ) from exc
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or _identity(before) != _identity(opened):
        os.close(descriptor)
        raise SnapshotBundleError(
            f"snapshot bundle directory changed during validation: {relative}"
        )
    return descriptor


def _matches_record(
    relative: Path,
    inspected: InspectedSourceFile,
    record: dict[str, Any],
) -> None:
    if (
        inspected.sha256 != record["sha256"]
        or inspected.size != record["size"]
        or inspected.mime != record["mime"]
    ):
        raise SnapshotBundleError(
            f"snapshot bundle file does not match its manifest: {relative}"
        )


def validate_snapshot_bundle(
    root: Path,
    *,
    max_primary_bytes: int,
    max_files: int,
    max_total_bytes: int,
    destination: Path | None = None,
) -> dict[str, Any]:
    """Validate and optionally materialize one exact manifest-declared bundle."""
    if (
        isinstance(max_primary_bytes, bool)
        or not isinstance(max_primary_bytes, int)
        or max_primary_bytes < 0
        or isinstance(max_files, bool)
        or not isinstance(max_files, int)
        or max_files < 0
        or isinstance(max_total_bytes, bool)
        or not isinstance(max_total_bytes, int)
        or max_total_bytes < 0
    ):
        raise SnapshotBundleError("snapshot bundle limits are invalid")

    root = Path(os.path.abspath(root.expanduser()))
    resolved_root = root.resolve()
    if destination is not None:
        destination = destination.expanduser().resolve()
        if destination == resolved_root or destination.is_relative_to(resolved_root):
            raise SnapshotBundleError(
                "snapshot bundle destination must be outside the source bundle"
            )
        if destination.exists() or destination.is_symlink():
            raise SnapshotBundleError("snapshot bundle destination already exists")
        destination.mkdir(parents=True)

    root_descriptor: int | None = None
    try:
        try:
            root_metadata = root.lstat()
        except OSError as exc:
            raise SnapshotBundleError(
                "snapshot bundle must be a regular directory"
            ) from exc
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise SnapshotBundleError("snapshot bundle must be a regular directory")
        root_descriptor = _directory_descriptor(
            root, "snapshot bundle", expected=root_metadata
        )

        manifest_copy = destination / SNAPSHOT_MANIFEST_NAME if destination else None
        manifest_inspection = inspect_regular_file_at(
            root_descriptor,
            SNAPSHOT_MANIFEST_NAME,
            display_name=SNAPSHOT_MANIFEST_NAME,
            max_bytes=SNAPSHOT_MANIFEST_MAX_BYTES,
            label="snapshot manifest",
            destination=manifest_copy,
            include_content=True,
        )
        if manifest_inspection.mime != "application/json":
            raise SnapshotBundleError("snapshot manifest must be JSON")
        try:
            manifest = json.loads(
                (manifest_inspection.content or b"").decode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotBundleError("snapshot manifest is not valid UTF-8 JSON") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != 1
            or set(manifest) != _MANIFEST_FIELDS
        ):
            raise SnapshotBundleError(
                "snapshot manifest has an unsupported schema or unknown fields"
            )

        primary = _file_record(
            manifest.get("primary"), label="snapshot primary", with_role=False
        )
        raw_files = manifest.get("files")
        if not isinstance(raw_files, list):
            raise SnapshotBundleError("snapshot manifest files must be an array")
        if len(raw_files) > max_files:
            raise SnapshotBundleError(
                f"snapshot bundle exceeds the {max_files}-file limit"
            )
        files = [
            _file_record(item, label=f"snapshot files[{index}]", with_role=True)
            for index, item in enumerate(raw_files)
        ]
        provider = _provider_record(manifest.get("provider"))
        records = [primary, *files]
        paths = [str(record["path"]) for record in records]
        if len(set(paths)) != len(paths):
            raise SnapshotBundleError("snapshot manifest declares duplicate file paths")
        if primary["size"] > max_primary_bytes:
            raise SnapshotBundleError(
                f"snapshot primary exceeds {max_primary_bytes} bytes"
            )
        declared_total = sum(int(record["size"]) for record in records)
        if declared_total > max_total_bytes:
            raise SnapshotBundleError(
                f"snapshot bundle exceeds {max_total_bytes} bytes"
            )

        by_path = {Path(str(record["path"])): record for record in records}
        expected_directories = {Path(".")}
        for relative in by_path:
            parent = relative.parent
            while parent != Path("."):
                expected_directories.add(parent)
                parent = parent.parent

        seen_files: set[Path] = set()
        seen_identities: set[FileIdentity] = {manifest_inspection.identity}
        seen_hashes: set[str] = set()
        actual_total = 0
        visited_entries = 0
        maximum_entries = len(by_path) + len(expected_directories)

        def inspect_directory(
            directory_descriptor: int,
            relative_dir: Path,
        ) -> None:
            nonlocal actual_total, visited_entries
            if len(relative_dir.parts) > MAX_ASSET_DEPTH:
                raise SnapshotBundleError(
                    f"snapshot bundle exceeds the {MAX_ASSET_DEPTH}-directory depth limit"
                )
            names: list[str] = []
            with os.scandir(directory_descriptor) as entries:
                for entry in entries:
                    visited_entries += 1
                    if visited_entries > maximum_entries:
                        raise SnapshotBundleError(
                            "snapshot bundle contains undeclared content"
                        )
                    names.append(entry.name)
            for name in sorted(names):
                try:
                    metadata = os.stat(
                        name,
                        dir_fd=directory_descriptor,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    raise SnapshotBundleError(
                        "snapshot bundle changed during validation"
                    ) from exc
                relative = relative_dir / name
                if stat.S_ISLNK(metadata.st_mode):
                    raise SnapshotBundleError(
                        f"snapshot bundle contains a symbolic link: {relative}"
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    if relative not in expected_directories:
                        raise SnapshotBundleError(
                            f"snapshot bundle contains undeclared content: {relative}/"
                        )
                    child_descriptor = _open_child_directory(
                        directory_descriptor, name, metadata, relative
                    )
                    try:
                        inspect_directory(child_descriptor, relative)
                    finally:
                        os.close(child_descriptor)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise SnapshotBundleError(
                        f"snapshot bundle content is not a regular file: {relative}"
                    )
                if relative == Path(SNAPSHOT_MANIFEST_NAME):
                    if (
                        relative_dir != Path()
                        or _identity(metadata) != manifest_inspection.identity
                    ):
                        raise SnapshotBundleError(
                            "snapshot manifest changed during validation"
                        )
                    continue
                record = by_path.get(relative)
                if record is None:
                    raise SnapshotBundleError(
                        f"snapshot bundle contains undeclared content: {relative}"
                    )
                maximum = (
                    max_primary_bytes
                    if relative == Path(str(primary["path"]))
                    else max_total_bytes
                )
                copied = destination / relative if destination else None
                inspected = inspect_regular_file_at(
                    directory_descriptor,
                    name,
                    display_name=relative.as_posix(),
                    max_bytes=maximum,
                    label="snapshot bundle",
                    destination=copied,
                )
                if inspected.identity in seen_identities:
                    raise SnapshotBundleError(
                        f"snapshot bundle files physically alias each other: {relative}"
                    )
                seen_identities.add(inspected.identity)
                if inspected.sha256 in seen_hashes:
                    raise SnapshotBundleError(
                        f"snapshot bundle contains duplicate file content: {relative}"
                    )
                seen_hashes.add(inspected.sha256)
                _matches_record(relative, inspected, record)
                actual_total += inspected.size
                if actual_total > max_total_bytes:
                    raise SnapshotBundleError(
                        f"snapshot bundle exceeds {max_total_bytes} bytes"
                    )
                seen_files.add(relative)

        inspect_directory(root_descriptor, Path())
        missing = sorted(path.as_posix() for path in set(by_path) - seen_files)
        if missing:
            raise SnapshotBundleError(
                "snapshot bundle is missing declared files: " + ", ".join(missing)
            )

        final_manifest = inspect_regular_file_at(
            root_descriptor,
            SNAPSHOT_MANIFEST_NAME,
            display_name=SNAPSHOT_MANIFEST_NAME,
            max_bytes=SNAPSHOT_MANIFEST_MAX_BYTES,
            label="snapshot manifest",
        )
        if (
            final_manifest.identity != manifest_inspection.identity
            or final_manifest.sha256 != manifest_inspection.sha256
            or final_manifest.size != manifest_inspection.size
        ):
            raise SnapshotBundleError("snapshot manifest changed during validation")

        return {
            "schema_version": 1,
            "manifest_sha256": manifest_inspection.sha256,
            "primary": primary,
            "files": sorted(files, key=lambda item: str(item["path"])),
            "provider": provider,
        }
    except IngestError as exc:
        if destination is not None:
            shutil.rmtree(destination, ignore_errors=True)
        if isinstance(exc, SnapshotBundleError):
            raise
        raise SnapshotBundleError(str(exc)) from exc
    except Exception:
        if destination is not None:
            shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)
