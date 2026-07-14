"""Integrity validation for immutable source captures."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


class CaptureError(RuntimeError):
    """Raised when a source capture cannot be trusted."""


SOURCE_ID_PATTERN = re.compile(r"src_[0-9a-f]{20}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
IDENTITY_FIELDS = (
    "schema_version",
    "source_id",
    "requested_url",
    "final_url",
    "domain",
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
        "domain": manifest.get("domain"),
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
    return bool(
        re.search(
            rf'^source_id:\s*(?:"{re.escape(source_id)}"|{re.escape(source_id)})\s*$',
            content,
            re.MULTILINE,
        )
    )


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


def _read_manifest(capture_dir: Path) -> dict[str, Any]:
    if capture_dir.is_symlink() or not capture_dir.is_dir():
        raise CaptureError(f"capture path is not a regular directory: {capture_dir}")
    try:
        manifest = json.loads(
            (capture_dir / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptureError(f"capture directory is incomplete: {capture_dir}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise CaptureError(f"capture manifest schema is invalid: {capture_dir}")
    return manifest


def load_validated_capture(
    capture_dir: Path,
    *,
    source_id: str,
    vault: Path,
    expected_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load a capture only after verifying identity, files, and source-note linkage."""
    if not SOURCE_ID_PATTERN.fullmatch(source_id):
        raise CaptureError("invalid source id")
    manifest = _read_manifest(capture_dir)
    if manifest.get("source_id") != source_id:
        raise CaptureError("capture manifest source id does not match its directory")
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
        raise CaptureError("capture source id does not match its immutable identity")
    if expected_manifest is not None and any(
        manifest.get(field) != expected_manifest.get(field) for field in IDENTITY_FIELDS
    ):
        raise CaptureError(f"existing capture metadata does not match: {capture_dir}")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise CaptureError("capture manifest artifact metadata is invalid")
    raw_path = _artifact_path(artifacts.get("raw"), "raw artifact")
    extracted_path = _artifact_path(artifacts.get("extracted"), "extracted artifact")
    expected_hashes = {raw_path: raw_hash, extracted_path: extracted_hash}
    for raw_asset, digest in assets.items():
        asset = _artifact_path(raw_asset, "asset")
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
            raise CaptureError(f"capture manifest asset hash is invalid: {raw_asset}")
        if asset in expected_hashes:
            raise CaptureError(f"capture manifest has duplicate artifact path: {asset}")
        expected_hashes[asset] = digest

    expected_files = {Path("manifest.json"), *expected_hashes}
    expected_directories = {Path(".")}
    for relative in expected_files:
        parent = relative.parent
        while parent != Path("."):
            expected_directories.add(parent)
            parent = parent.parent
    unexpected: list[str] = []
    for path in capture_dir.rglob("*"):
        relative = path.relative_to(capture_dir)
        if path.is_symlink():
            unexpected.append(str(relative))
        elif path.is_dir():
            if relative not in expected_directories:
                unexpected.append(str(relative) + "/")
        elif relative not in expected_files:
            unexpected.append(str(relative))
    if unexpected:
        raise CaptureError(
            "capture contains undeclared content: " + ", ".join(sorted(unexpected))
        )
    for relative, expected_hash in expected_hashes.items():
        path = capture_dir / relative
        if path.is_symlink() or not path.is_file():
            raise CaptureError(f"capture artifact is missing: {relative}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise CaptureError(f"capture artifact integrity failed: {relative}")

    source_note = _vault_path(
        vault, artifacts.get("source_note"), "source note artifact"
    )
    if not source_note_has_id(source_note, source_id):
        raise CaptureError("capture source note does not contain its source id")
    return manifest
