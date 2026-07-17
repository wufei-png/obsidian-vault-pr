"""Validation rules for promoting immutable Capture assets into a vault."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any

from .capture import CaptureError, load_validated_capture_assets_bytes
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


_MANAGED_HASH_MARKER = re.compile(r"--([0-9a-f]{12})$")


def load_managed_asset_index(
    config: dict[str, Any],
    vault: Path,
) -> tuple[Path, dict[str, list[Path]]]:
    """Enumerate hash-named managed assets once per Plan validation."""
    asset_dir = vault_asset_dir(config, vault)
    relative_dir = asset_dir.relative_to(vault).as_posix()
    result = run_git(vault, "ls-files", "-z", "--", relative_dir)
    if result.returncode != 0:
        raise AssetPromotionError("unable to enumerate existing Vault Assets")
    by_hash_prefix: dict[str, list[Path]] = {}
    for raw in result.stdout.split("\0"):
        if not raw:
            continue
        candidate = vault / raw
        if candidate.parent != asset_dir:
            continue
        match = _MANAGED_HASH_MARKER.search(candidate.stem)
        if match is None:
            continue
        if candidate.is_symlink() or not candidate.is_file():
            raise AssetPromotionError(f"tracked Vault Asset is unsafe: {raw}")
        by_hash_prefix.setdefault(match.group(1), []).append(candidate)
    return asset_dir, {
        prefix: sorted(paths) for prefix, paths in by_hash_prefix.items()
    }


def normalize_asset_promotion(
    vault: Path,
    raw_operation: dict[str, Any],
    index: int,
    source_ids: set[str],
    source_manifests: dict[str, dict[str, Any]],
    asset_dir: Path,
    managed_assets: dict[str, list[Path]],
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
    manifest = source_manifests[source_id]
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
        for candidate in managed_assets.get(digest[:12], [])
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
    vault: Path,
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
    target_by_digest: dict[str, Path] = {}
    for operation in promotions:
        digest = str(operation["sha256"])
        target = operation["path"]
        existing = target_by_digest.setdefault(digest, target)
        if target != existing:
            raise AssetPromotionError(
                "asset_reuse_required: "
                + existing.relative_to(vault).as_posix()
            )


def validate_promotion_references(
    vault: Path,
    promotions: list[dict[str, Any]],
    modified_note_paths: set[str],
    resolved_assets_by_path: dict[str, list[str]],
) -> None:
    """Require every promotion target to be referenced by this transaction."""
    promoted_paths = {
        str(operation["path"].relative_to(vault))
        for operation in promotions
    }
    referenced_assets = {
        asset
        for note_path in modified_note_paths
        for asset in resolved_assets_by_path.get(note_path, [])
    }
    unreferenced = sorted(promoted_paths - referenced_assets)
    if unreferenced:
        raise AssetPromotionError(
            "promoted asset must be referenced by a modified visible note: "
            + ", ".join(unreferenced)
        )


def promotion_review_evidence(
    vault: Path,
    promotions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build bounded review evidence for normalized promotions."""
    return [
        {
            "evidence_id": f"asset:{operation['path'].relative_to(vault)}",
            "source_id": operation["source_id"],
            "source_path": operation["source_path"],
            "path": str(operation["path"].relative_to(vault)),
            "sha256": operation["sha256"],
            "size": operation["size"],
            "mime": operation["mime"],
            "disposition": operation["disposition"],
        }
        for operation in promotions
    ]


def load_promotion_snapshots(
    config: dict[str, Any],
    vault: Path,
    promotions: list[dict[str, Any]],
) -> dict[Path, bytes]:
    """Revalidate each Capture once and pin promoted bytes for apply."""
    snapshots: dict[Path, bytes] = {}
    by_source: dict[str, list[dict[str, Any]]] = {}
    for operation in promotions:
        by_source.setdefault(operation["source_id"], []).append(operation)
    for source_id, source_promotions in by_source.items():
        capture_dir = source_archive_dir(config, vault) / source_id
        try:
            _manifest, assets = load_validated_capture_assets_bytes(
                capture_dir,
                source_id=source_id,
                vault=vault,
                artifact_paths=[
                    operation["source_path"]
                    for operation in source_promotions
                ],
            )
        except CaptureError as exc:
            raise AssetPromotionError(
                f"promoted asset changed during validation: {exc}"
            ) from exc
        for operation in source_promotions:
            asset, content = assets[operation["source_path"]]
            if (
                asset["sha256"] != operation["sha256"]
                or asset["size"] != operation["size"]
                or asset["mime"] != operation["mime"]
            ):
                raise AssetPromotionError(
                    "promoted asset changed during validation"
                )
            snapshots[operation["path"]] = content
    return snapshots


def promotion_results(
    vault: Path,
    promotions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the transaction result projection for Asset Promotions."""
    return [
        {
            "source_id": operation["source_id"],
            "source_path": operation["source_path"],
            "path": str(operation["path"].relative_to(vault)),
            "sha256": operation["sha256"],
            "mime": operation["mime"],
            "status": (
                "created"
                if operation["disposition"] == "create"
                else "reused"
            ),
        }
        for operation in promotions
    ]


def install_created_promotion(
    vault: Path,
    path: Path,
    content: bytes,
    git_mode: str,
) -> None:
    """Create a promoted asset through pinned, no-follow directory handles."""
    try:
        relative = path.relative_to(vault)
    except ValueError as exc:
        raise AssetPromotionError("promotion destination escapes the vault") from exc
    if not relative.parts or relative.name in {"", ".", ".."}:
        raise AssetPromotionError("promotion destination is invalid")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptors: list[int] = []
    file_descriptor: int | None = None
    identity: os.stat_result | None = None
    parent_descriptor: int | None = None
    try:
        current_descriptor = os.open(vault, directory_flags)
        descriptors.append(current_descriptor)
        for part in relative.parent.parts:
            if part in {"", ".", ".."}:
                raise AssetPromotionError("promotion destination is invalid")
            try:
                os.mkdir(part, 0o755, dir_fd=current_descriptor)
            except FileExistsError:
                pass
            current_descriptor = os.open(
                part,
                directory_flags,
                dir_fd=current_descriptor,
            )
            descriptors.append(current_descriptor)
        parent_descriptor = current_descriptor
        pinned_parent = os.fstat(parent_descriptor)
        lexical_parent = os.stat(path.parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(lexical_parent.st_mode)
            or lexical_parent.st_dev != pinned_parent.st_dev
            or lexical_parent.st_ino != pinned_parent.st_ino
        ):
            raise AssetPromotionError(
                "promotion destination directory changed during write"
            )

        file_descriptor = os.open(
            relative.name,
            file_flags,
            0o755 if git_mode == "100755" else 0o644,
            dir_fd=parent_descriptor,
        )
        identity = os.fstat(file_descriptor)
        with os.fdopen(file_descriptor, "wb", closefd=False) as handle:
            os.fchmod(
                handle.fileno(),
                0o755 if git_mode == "100755" else 0o644,
            )
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(file_descriptor)
        file_descriptor = None

        lexical_parent = os.stat(path.parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(lexical_parent.st_mode)
            or lexical_parent.st_dev != pinned_parent.st_dev
            or lexical_parent.st_ino != pinned_parent.st_ino
        ):
            os.unlink(relative.name, dir_fd=parent_descriptor)
            identity = None
            raise AssetPromotionError(
                "promotion destination directory changed during write"
            )
    except (AssetPromotionError, OSError) as exc:
        if identity is not None and parent_descriptor is not None:
            try:
                current = os.stat(
                    relative.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    current.st_dev == identity.st_dev
                    and current.st_ino == identity.st_ino
                ):
                    os.unlink(relative.name, dir_fd=parent_descriptor)
            except OSError:
                pass
        if isinstance(exc, AssetPromotionError):
            raise
        raise AssetPromotionError(
            "unable to create promotion destination safely"
        ) from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        for descriptor in reversed(descriptors):
            os.close(descriptor)
