"""Validation rules for promoting immutable Capture assets into a vault."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .capture import CaptureError, load_validated_capture
from .config import (
    source_archive_dir,
    vault_asset_dir,
    vault_relative_path,
)
from .git import run_git


class AssetPromotionError(ValueError):
    """Raised when an Asset Promotion operation is invalid or unsafe."""


ASSET_EXTENSIONS = {
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/gif": {".gif"},
    "image/webp": {".webp"},
    "application/pdf": {".pdf"},
}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _managed_asset_candidates(
    vault: Path,
    asset_dir: Path,
    *,
    digest: str,
) -> list[Path]:
    relative_dir = asset_dir.relative_to(vault).as_posix()
    result = run_git(vault, "ls-files", "-z", "--", relative_dir)
    if result.returncode != 0:
        raise AssetPromotionError("unable to enumerate existing Vault Assets")
    marker = f"--{digest[:12]}"
    paths: list[Path] = []
    for raw in result.stdout.split("\0"):
        if not raw:
            continue
        candidate = vault / raw
        if candidate.parent != asset_dir or not candidate.stem.endswith(marker):
            continue
        if candidate.is_symlink() or not candidate.is_file():
            raise AssetPromotionError(f"tracked Vault Asset is unsafe: {raw}")
        paths.append(candidate)
    return sorted(paths)


def normalize_asset_promotion(
    config: dict[str, Any],
    vault: Path,
    raw_operation: dict[str, Any],
    index: int,
    source_ids: set[str],
) -> dict[str, Any]:
    """Validate and normalize one explicit Asset Promotion operation."""
    if set(raw_operation) != {"action", "source_id", "source_path", "path"}:
        raise AssetPromotionError(
            f"operation {index} promote_asset has unknown or missing fields"
        )
    source_id = raw_operation.get("source_id")
    source_path = raw_operation.get("source_path")
    if (
        not isinstance(source_id, str)
        or source_id not in source_ids
        or not isinstance(source_path, str)
        or not source_path
    ):
        raise AssetPromotionError(
            f"operation {index} promotion must use declared "
            "source_capture evidence"
        )
    capture_dir = source_archive_dir(config, vault) / source_id
    try:
        manifest = load_validated_capture(
            capture_dir,
            source_id=source_id,
            vault=vault,
        )
    except CaptureError as exc:
        raise AssetPromotionError(
            f"source capture integrity failed: {exc}"
        ) from exc
    asset = next(
        (
            item
            for item in manifest["files"]
            if item.get("path") == source_path and item.get("role") == "asset"
        ),
        None,
    )
    if asset is None:
        raise AssetPromotionError(
            f"operation {index} source_path is not a promotable capture asset"
        )
    raw_target = raw_operation.get("path")
    if isinstance(raw_target, str) and (vault / raw_target).is_symlink():
        raise AssetPromotionError(
            f"operation {index} promotion target must not be a symlink"
        )
    target = vault_relative_path(
        vault,
        raw_target,
        f"operations[{index}].path",
    )
    asset_dir = vault_asset_dir(config, vault)
    if target.parent != asset_dir:
        raise AssetPromotionError(
            f"operation {index} promotion target must be directly under "
            f"{asset_dir.relative_to(vault)}"
        )
    mime = str(asset["mime"])
    suffix = target.suffix.casefold()
    allowed_extensions = ASSET_EXTENSIONS.get(mime)
    if allowed_extensions is None:
        raise AssetPromotionError(
            f"operation {index} asset MIME is not promotable: {mime}"
        )
    if suffix not in allowed_extensions:
        raise AssetPromotionError(
            f"operation {index} promotion target extension does not match {mime}"
        )
    digest = str(asset["sha256"])
    marker = f"--{digest[:12]}"
    if not target.stem.endswith(marker) or target.stem == marker:
        raise AssetPromotionError(
            f"operation {index} promotion target must use a readable name "
            f"ending in {marker}"
        )

    exact_matches = [
        candidate
        for candidate in _managed_asset_candidates(
            vault,
            asset_dir,
            digest=digest,
        )
        if _sha256_file(candidate) == digest
    ]
    if exact_matches and target not in exact_matches:
        existing = exact_matches[0].relative_to(vault)
        raise AssetPromotionError(f"asset_reuse_required: {existing}")
    if target.exists():
        if (
            target.is_symlink()
            or not target.is_file()
            or target not in exact_matches
        ):
            raise AssetPromotionError(
                f"operation {index} promotion target exists with "
                "different content"
            )
        disposition = "reuse"
    else:
        disposition = "create"
    return {
        "action": "promote_asset",
        "path": target,
        "source_id": source_id,
        "source_path": source_path,
        "sha256": digest,
        "size": int(asset["size"]),
        "mime": mime,
        "disposition": disposition,
    }


def validate_asset_promotion_batch(
    config: dict[str, Any],
    promotions: list[dict[str, Any]],
) -> None:
    """Apply the configured Capture asset bounds to one transaction."""
    sources = config.get("sources", {})
    if not isinstance(sources, dict):
        raise AssetPromotionError("sources must be a table")
    maximum_files = int(sources.get("max_asset_files", 200))
    maximum_bytes = int(sources.get("max_asset_bytes", 50_000_000))
    if len(promotions) > maximum_files:
        raise AssetPromotionError(
            f"plan promotes {len(promotions)} assets; maximum is {maximum_files}"
        )
    total_bytes = sum(int(operation["size"]) for operation in promotions)
    if total_bytes > maximum_bytes:
        raise AssetPromotionError(
            f"plan promotes {total_bytes} asset bytes; maximum is {maximum_bytes}"
        )
