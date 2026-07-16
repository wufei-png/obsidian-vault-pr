"""Integrity validation for immutable source captures."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

import yaml


class CaptureError(RuntimeError):
    """Raised when a source capture cannot be trusted."""


SOURCE_ID_PATTERN = re.compile(r"src_[0-9a-f]{20}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
IDENTITY_FIELDS = (
    "schema_version",
    "source_id",
    "requested_url",
    "final_url",
    "raw_sha256",
    "extracted_sha256",
    "adapter",
    "asset_sha256",
    "artifacts",
)


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
    return {
        "schema_version": manifest.get("schema_version"),
        "requested_url": manifest.get("requested_url"),
        "final_url": manifest.get("final_url"),
        "adapter": manifest.get("adapter"),
        "raw_sha256": manifest.get("raw_sha256"),
        "extracted_sha256": manifest.get("extracted_sha256"),
        "asset_sha256": manifest.get("asset_sha256"),
        "artifacts": manifest.get("artifacts"),
    }


def source_note_has_id(path: Path, source_id: str) -> bool:
    """Check the exact source_id property in a visible source note."""
    if path.is_symlink() or not path.is_file():
        raise CaptureError(f"source note is not a regular file: {path}")
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CaptureError(f"unable to inspect source note: {path}") from exc
    lines = content.splitlines()
    if not lines or lines[0] != "---":
        return False
    try:
        end = next(
            index for index, line in enumerate(lines[1:], start=1) if line == "---"
        )
    except StopIteration as exc:
        raise CaptureError(f"source note has unterminated frontmatter: {path}") from exc
    try:
        frontmatter = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise CaptureError(f"source note has invalid frontmatter: {path}") from exc
    if not isinstance(frontmatter, dict):
        return False
    return frontmatter.get("source_id") == source_id


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
        raise CaptureError(
            f"capture path is not a regular directory: {capture_dir}"
        ) from exc
    if not stat.S_ISDIR(before.st_mode):
        raise CaptureError(f"capture path is not a regular directory: {capture_dir}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(capture_dir, flags)
    except OSError as exc:
        raise CaptureError(
            f"capture path is not a regular directory: {capture_dir}"
        ) from exc
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or _identity(before) != _identity(opened):
        os.close(descriptor)
        raise CaptureError(f"capture path changed during validation: {capture_dir}")
    return descriptor


def _read_capture_file(
    root_descriptor: int,
    relative: Path,
    *,
    include_content: bool = False,
) -> tuple[str, tuple[int, int], bytes | None]:
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
                if content is not None:
                    content.extend(chunk)
        finally:
            os.close(file_descriptor)
        return digest.hexdigest(), identity, bytes(content) if content is not None else None
    finally:
        os.close(directory_descriptor)


def _read_manifest(
    root_descriptor: int, capture_dir: Path
) -> tuple[dict[str, Any], str, tuple[int, int]]:
    manifest_hash, manifest_identity, content = _read_capture_file(
        root_descriptor,
        Path("manifest.json"),
        include_content=True,
    )
    assert content is not None
    try:
        manifest = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureError(f"capture directory is incomplete: {capture_dir}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
        raise CaptureError(f"capture manifest schema is invalid: {capture_dir}")
    return manifest, manifest_hash, manifest_identity


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
        identity = source_identity_payload(manifest)
        raw_hash = identity["raw_sha256"]
        extracted_hash = identity["extracted_sha256"]
        assets = identity["asset_sha256"]
        if (
            not isinstance(raw_hash, str)
            or not SHA256_PATTERN.fullmatch(raw_hash)
            or not isinstance(extracted_hash, str)
            or not SHA256_PATTERN.fullmatch(extracted_hash)
            or not isinstance(assets, dict)
        ):
            raise CaptureError("capture manifest contains invalid identity hashes")
        expected_source_id = f"src_{source_identity_digest(identity)[:20]}"
        if source_id != expected_source_id:
            raise CaptureError(
                "capture source id does not match its immutable identity"
            )
        if expected_manifest is not None and any(
            manifest.get(field) != expected_manifest.get(field)
            for field in IDENTITY_FIELDS
        ):
            raise CaptureError(
                f"existing capture metadata does not match: {capture_dir}"
            )

        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict):
            raise CaptureError("capture manifest artifact metadata is invalid")
        raw_path = _artifact_path(artifacts.get("raw"), "raw artifact")
        extracted_path = _artifact_path(
            artifacts.get("extracted"), "extracted artifact"
        )
        if raw_path == extracted_path:
            raise CaptureError(
                f"capture manifest has duplicate artifact path: {raw_path}"
            )
        expected_hashes = {raw_path: raw_hash, extracted_path: extracted_hash}
        for raw_asset, digest in assets.items():
            asset = _artifact_path(raw_asset, "asset")
            if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
                raise CaptureError(
                    f"capture manifest asset hash is invalid: {raw_asset}"
                )
            if asset in expected_hashes:
                raise CaptureError(
                    f"capture manifest has duplicate artifact path: {asset}"
                )
            expected_hashes[asset] = digest

        requested_path: Path | None = None
        if content_artifact is not None:
            requested_path = _artifact_path(
                artifacts.get(content_artifact),
                f"{content_artifact} artifact",
            )
            if requested_path not in expected_hashes:
                raise CaptureError(
                    f"capture manifest has unknown artifact: {content_artifact}"
                )

        expected_files = {Path("manifest.json"), *expected_hashes}
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
        for relative, expected_hash in expected_hashes.items():
            actual_hash, file_identity, content = _read_capture_file(
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
            if relative == requested_path:
                requested_content = content

        final_manifest_hash, final_manifest_identity, _content = _read_capture_file(
            root_descriptor, Path("manifest.json")
        )
        if (
            final_manifest_hash != manifest_hash
            or final_manifest_identity != manifest_identity
        ):
            raise CaptureError("capture manifest changed during validation")
    finally:
        os.close(root_descriptor)

    source_note = _vault_path(
        vault, manifest["artifacts"].get("source_note"), "source note artifact"
    )
    if not source_note_has_id(source_note, source_id):
        raise CaptureError("capture source note does not contain its source id")
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
