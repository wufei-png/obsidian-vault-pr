"""One-time, journaled migrations for legacy vault-pr captures."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .capture import (
    CaptureError,
    SOURCE_ID_PATTERN,
    load_validated_capture,
    source_identity_digest,
    source_identity_payload,
    source_note_has_id,
)
from .config import (
    load_vault_path,
    note_path_allowed,
    source_archive_dir,
)
from .git import require_clean_git_root, run_git, vault_write_lock
from .recovery import (
    RecoveryError,
    mark_active_applied,
    prepare_active_transaction,
    rollback_active_transaction,
    worktree_file_mode,
)


class MigrationError(RuntimeError):
    """Raised when a legacy capture cannot be migrated safely."""


_DOMAIN_PROPERTY = re.compile(r"^domain\s*:", re.IGNORECASE)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_artifact(raw: Any, label: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise MigrationError(f"legacy capture has invalid {label} path")
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        raise MigrationError(f"legacy capture {label} path escapes its directory")
    return path


def _vault_file(vault: Path, raw: Any, label: str) -> Path:
    relative = _relative_artifact(raw, label)
    current = vault
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise MigrationError(f"legacy capture {label} traverses a symbolic link")
    return current


def _legacy_files(capture_dir: Path, manifest: dict[str, Any]) -> dict[Path, str]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise MigrationError("legacy capture artifact metadata is invalid")
    raw = _relative_artifact(artifacts.get("raw"), "raw artifact")
    extracted = _relative_artifact(
        artifacts.get("extracted"), "extracted artifact"
    )
    asset_hashes = manifest.get("asset_sha256", {})
    if not isinstance(asset_hashes, dict):
        raise MigrationError("legacy capture asset hashes are invalid")
    expected: dict[Path, str] = {
        raw: manifest.get("raw_sha256"),
        extracted: manifest.get("extracted_sha256"),
    }
    for raw_path, digest in asset_hashes.items():
        path = _relative_artifact(raw_path, "asset")
        if path in expected:
            raise MigrationError("legacy capture declares a duplicate artifact path")
        expected[path] = digest
    if not all(
        isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
        for digest in expected.values()
    ):
        raise MigrationError("legacy capture contains invalid artifact hashes")

    expected_paths = {Path("manifest.json"), *expected}
    expected_directories = {Path(".")}
    for relative in expected_paths:
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
        elif relative not in expected_paths:
            unexpected.append(str(relative))
    if unexpected:
        raise MigrationError(
            "legacy capture contains undeclared content: "
            + ", ".join(sorted(unexpected))
        )
    for relative, digest in expected.items():
        path = capture_dir / relative
        if path.is_symlink() or not path.is_file() or _sha256(path) != digest:
            raise MigrationError(f"legacy capture artifact integrity failed: {relative}")
    return expected


def _read_legacy_capture(
    config: dict[str, Any],
    vault: Path,
    capture_dir: Path,
    *,
    missing_adapter: str | None,
) -> tuple[dict[str, Any], dict[Path, str], Path]:
    if capture_dir.is_symlink() or not capture_dir.is_dir():
        raise MigrationError("legacy capture path is not a regular directory")
    try:
        manifest = json.loads(
            (capture_dir / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError("legacy capture manifest is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise MigrationError("capture is not schema v1")
    old_source_id = manifest.get("source_id")
    if (
        not isinstance(old_source_id, str)
        or not SOURCE_ID_PATTERN.fullmatch(old_source_id)
        or capture_dir.name != old_source_id
    ):
        raise MigrationError("legacy capture source id is invalid")
    adapter = manifest.get("adapter")
    if adapter is None:
        adapter = missing_adapter
    if adapter not in {"http", "wechat", "authenticated"}:
        raise MigrationError(
            f"legacy capture {old_source_id} has no supported adapter; "
            "pass --missing-adapter"
        )
    artifacts = _legacy_files(capture_dir, manifest)
    source_note = _vault_file(
        vault, manifest.get("artifacts", {}).get("source_note"), "source note"
    )
    try:
        linked = source_note_has_id(source_note, old_source_id)
    except CaptureError as exc:
        raise MigrationError("legacy capture source note linkage is invalid") from exc
    if not note_path_allowed(config, vault, source_note) or not linked:
        raise MigrationError("legacy capture source note linkage is invalid")
    migrated = dict(manifest)
    migrated["schema_version"] = 2
    migrated["adapter"] = adapter
    migrated["asset_sha256"] = dict(manifest.get("asset_sha256", {}))
    migrated.pop("domain", None)
    identity = source_identity_payload(migrated)
    new_source_id = f"src_{source_identity_digest(identity)[:20]}"
    migrated["source_id"] = new_source_id
    return migrated, artifacts, source_note


def _tracked_markdown(config: dict[str, Any], vault: Path) -> list[Path]:
    result = run_git(vault, "ls-files", "-z", "--", "*.md")
    if result.returncode != 0:
        raise MigrationError("unable to enumerate tracked Markdown notes")
    paths: list[Path] = []
    for raw in result.stdout.split("\0"):
        if not raw:
            continue
        path = vault / raw
        if note_path_allowed(config, vault, path):
            paths.append(path)
    return paths


def _remove_frontmatter_domain(content: str) -> str:
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return content
    end = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.rstrip("\r\n") == "---"
        ),
        None,
    )
    if end is None:
        return content
    return "".join(
        line
        for index, line in enumerate(lines)
        if not (0 < index < end and _DOMAIN_PROPERTY.match(line))
    )


def _git_dir(vault: Path) -> Path:
    result = run_git(vault, "rev-parse", "--absolute-git-dir")
    if result.returncode != 0:
        raise MigrationError("unable to resolve vault Git directory")
    return Path(result.stdout.strip()).resolve()


def migrate_captures_v2(
    config: dict[str, Any],
    *,
    apply: bool = False,
    missing_adapter: str | None = None,
) -> dict[str, Any]:
    """Plan or atomically apply every schema-v1 capture migration."""
    vault = load_vault_path(config)
    archive_root = source_archive_dir(config, vault)
    legacy_dirs: list[Path] = []
    if archive_root.is_dir():
        for path in sorted(archive_root.iterdir()):
            manifest_path = path / "manifest.json"
            if path.is_symlink() or manifest_path.is_symlink():
                raise MigrationError("capture archive contains a symbolic link")
            if not manifest_path.is_file():
                continue
            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MigrationError("capture manifest is unreadable") from exc
            if isinstance(raw, dict) and raw.get("schema_version") == 1:
                legacy_dirs.append(path)
    if not legacy_dirs:
        return {"status": "aligned", "migrated_captures": 0, "updated_notes": 0}

    migrations: list[tuple[Path, dict[str, Any], dict[Path, str], Path]] = []
    old_to_new: dict[str, str] = {}
    source_notes: set[Path] = set()
    for capture_dir in legacy_dirs:
        manifest, artifacts, source_note = _read_legacy_capture(
            config,
            vault,
            capture_dir,
            missing_adapter=missing_adapter,
        )
        old_source_id = capture_dir.name
        new_source_id = manifest["source_id"]
        if new_source_id in old_to_new.values():
            raise MigrationError("legacy captures collide after schema migration")
        target = archive_root / new_source_id
        if target.exists():
            raise MigrationError(f"migrated capture already exists: {new_source_id}")
        old_to_new[old_source_id] = new_source_id
        source_notes.add(source_note)
        migrations.append((capture_dir, manifest, artifacts, source_note))

    rewritten_notes: dict[Path, bytes] = {}
    for path in _tracked_markdown(config, vault):
        if path.is_symlink() or not path.is_file():
            raise MigrationError("tracked Markdown path is not a regular file")
        try:
            original = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationError("tracked Markdown note is not UTF-8") from exc
        updated = original
        for old_source_id, new_source_id in old_to_new.items():
            updated = updated.replace(old_source_id, new_source_id)
        if path in source_notes:
            updated = _remove_frontmatter_domain(updated)
        if updated != original:
            rewritten_notes[path] = updated.encode("utf-8")
    for source_note in source_notes:
        if source_note not in rewritten_notes:
            raise MigrationError("legacy source note was not selected for migration")

    result: dict[str, Any] = {
        "status": "planned",
        "migrated_captures": len(migrations),
        "updated_notes": len(rewritten_notes),
        "source_ids": old_to_new,
    }
    if not apply:
        return result

    affected: set[Path] = set(rewritten_notes)
    originals: dict[Path, str | None] = {}
    original_modes: dict[Path, str | None] = {}
    finals: dict[Path, str | None] = {}
    final_modes: dict[Path, str | None] = {}
    staged_files: dict[Path, bytes] = {}
    for path, content in rewritten_notes.items():
        affected.add(path)
        originals[path] = _sha256(path)
        original_modes[path] = worktree_file_mode(path)
        finals[path] = hashlib.sha256(content).hexdigest()
        final_modes[path] = original_modes[path]
        staged_files[path] = content
    for old_dir, manifest, artifacts, _source_note in migrations:
        new_dir = archive_root / manifest["source_id"]
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        old_files = {Path("manifest.json"), *artifacts}
        for relative in old_files:
            old_path = old_dir / relative
            affected.add(old_path)
            originals[old_path] = _sha256(old_path)
            original_modes[old_path] = worktree_file_mode(old_path)
            finals[old_path] = None
            final_modes[old_path] = None
            new_path = new_dir / relative
            content = (
                manifest_bytes
                if relative == Path("manifest.json")
                else old_path.read_bytes()
            )
            affected.add(new_path)
            originals[new_path] = None
            original_modes[new_path] = None
            finals[new_path] = hashlib.sha256(content).hexdigest()
            final_modes[new_path] = (
                "100644" if relative == Path("manifest.json") else original_modes[old_path]
            )
            staged_files[new_path] = content

    migration_digest = source_identity_digest(
        {"migration": "captures-v2", "source_ids": old_to_new}
    )
    with vault_write_lock(vault):
        require_clean_git_root(vault)
        try:
            active_dir = prepare_active_transaction(
                vault=vault,
                git_dir=_git_dir(vault),
                transaction_id=f"txn_migrate_v2_{migration_digest[:16]}",
                transaction_sha256=migration_digest,
                affected=affected,
                expected_original_sha256=originals,
                expected_original_mode=original_modes,
                final_sha256=finals,
                final_mode=final_modes,
            )
        except RecoveryError as exc:
            raise MigrationError(str(exc)) from exc
        try:
            staging = active_dir / "write-staging"
            for index, (destination, content) in enumerate(sorted(staged_files.items())):
                temporary = staging / str(index)
                temporary.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_bytes(content)
                mode = final_modes[destination]
                temporary.chmod(0o755 if mode == "100755" else 0o644)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporary, destination)
            for old_dir, _manifest, artifacts, _source_note in migrations:
                for relative in {Path("manifest.json"), *artifacts}:
                    (old_dir / relative).unlink()
                for directory in sorted(
                    (path for path in old_dir.rglob("*") if path.is_dir()),
                    key=lambda path: len(path.parts),
                    reverse=True,
                ):
                    directory.rmdir()
                old_dir.rmdir()
            for _old_dir, manifest, _artifacts, _source_note in migrations:
                load_validated_capture(
                    archive_root / manifest["source_id"],
                    source_id=manifest["source_id"],
                    vault=vault,
                    expected_manifest=manifest,
                )
            mark_active_applied(active_dir)
        except Exception as exc:
            try:
                rollback_active_transaction(vault, active_dir)
            except RecoveryError as recovery_exc:
                raise MigrationError(
                    f"capture migration failed and requires recovery: {recovery_exc}"
                ) from recovery_exc
            if isinstance(exc, MigrationError):
                raise
            raise MigrationError(f"capture migration failed: {exc}") from exc

    result["status"] = "migrated"
    result["transaction_id"] = active_dir.name
    result["requires_commit"] = True
    return result
