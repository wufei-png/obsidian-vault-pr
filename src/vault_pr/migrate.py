"""One-time, journaled migrations from Capture schema v2 to v3."""

from __future__ import annotations

import hashlib
import json
import mimetypes
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
)
from .config import (
    load_vault_path,
    note_path_allowed,
    source_archive_dir,
)
from .git import run_git
from .mutation import (
    MutationError,
    execute_journaled_mutation,
    inspect_active_operation,
    latest_committed_operation,
    mutation_response,
)
from .recovery import (
    worktree_file_mode,
)
from .snapshot_bundle import ASSET_MIMES, PRIMARY_MIMES
from .source_note import source_note_has_id


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


def _v2_files(capture_dir: Path, manifest: dict[str, Any]) -> dict[Path, str]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise MigrationError("schema-v2 capture artifact metadata is invalid")
    raw = _relative_artifact(artifacts.get("raw"), "raw artifact")
    extracted = _relative_artifact(
        artifacts.get("extracted"), "extracted artifact"
    )
    asset_hashes = manifest.get("asset_sha256", {})
    if not isinstance(asset_hashes, dict):
        raise MigrationError("schema-v2 capture asset hashes are invalid")
    raw_digest = manifest.get("raw_sha256")
    extracted_digest = manifest.get("extracted_sha256")
    if not all(
        isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
        for digest in (raw_digest, extracted_digest)
    ):
        raise MigrationError("schema-v2 capture contains invalid artifact hashes")
    assert isinstance(raw_digest, str) and isinstance(extracted_digest, str)
    expected: dict[Path, str] = {raw: raw_digest, extracted: extracted_digest}
    for raw_path, digest in asset_hashes.items():
        path = _relative_artifact(raw_path, "asset")
        if path in expected:
            raise MigrationError("schema-v2 capture declares a duplicate artifact path")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise MigrationError("schema-v2 capture contains invalid artifact hashes")
        expected[path] = digest

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
            "schema-v2 capture contains undeclared content: "
            + ", ".join(sorted(unexpected))
        )
    for relative, digest in expected.items():
        path = capture_dir / relative
        if path.is_symlink() or not path.is_file() or _sha256(path) != digest:
            raise MigrationError(
                f"schema-v2 capture artifact integrity failed: {relative}"
            )
    return expected


def _mime_for_path(path: Path) -> str:
    explicit = {
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".html": "text/html",
        ".htm": "text/html",
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    if path.suffix.lower() in explicit:
        return explicit[path.suffix.lower()]
    mime, _encoding = mimetypes.guess_type(path.name)
    if mime == "text/x-markdown":
        return "text/markdown"
    return mime or "application/octet-stream"


def _v3_file_record(path: Path, digest: str, capture_dir: Path) -> dict[str, Any]:
    return {
        "path": path.as_posix(),
        "sha256": digest,
        "size": (capture_dir / path).stat().st_size,
        "mime": _mime_for_path(path),
    }


def _read_v2_capture(
    config: dict[str, Any],
    vault: Path,
    capture_dir: Path,
) -> tuple[dict[str, Any], dict[Path, str], Path]:
    if capture_dir.is_symlink() or not capture_dir.is_dir():
        raise MigrationError("schema-v2 capture path is not a regular directory")
    try:
        manifest = json.loads(
            (capture_dir / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError("schema-v2 capture manifest is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
        raise MigrationError("capture is not schema v2")
    old_source_id = manifest.get("source_id")
    if (
        not isinstance(old_source_id, str)
        or not SOURCE_ID_PATTERN.fullmatch(old_source_id)
        or capture_dir.name != old_source_id
    ):
        raise MigrationError("schema-v2 capture source id is invalid")
    adapter = manifest.get("adapter")
    if not isinstance(adapter, str) or adapter not in {
        "http",
        "wechat",
        "authenticated",
    }:
        raise MigrationError(f"schema-v2 capture {old_source_id} has invalid adapter")
    artifacts = _v2_files(capture_dir, manifest)
    source_note = _vault_file(
        vault, manifest.get("artifacts", {}).get("source_note"), "source note"
    )
    try:
        linked = source_note_has_id(source_note, old_source_id)
    except CaptureError as exc:
        raise MigrationError("schema-v2 capture source note linkage is invalid") from exc
    if not note_path_allowed(config, vault, source_note) or not linked:
        raise MigrationError("schema-v2 capture source note linkage is invalid")
    raw_relative = _relative_artifact(
        manifest.get("artifacts", {}).get("raw"), "raw artifact"
    )
    extracted_relative = _relative_artifact(
        manifest.get("artifacts", {}).get("extracted"), "extracted artifact"
    )
    source = {
        "kind": "snapshot_bundle" if adapter in {"wechat", "authenticated"} else "url",
        "access_kind": "authenticated" if adapter == "authenticated" else "public",
        "requested_url": manifest.get("requested_url"),
        "final_url": manifest.get("final_url"),
    }
    if not all(
        isinstance(source[field], str) and source[field]
        for field in ("requested_url", "final_url")
    ):
        raise MigrationError("schema-v2 capture URL metadata is invalid")
    primary_record = _v3_file_record(
        raw_relative, str(manifest.get("raw_sha256")), capture_dir
    )
    extracted_record = _v3_file_record(
        extracted_relative,
        str(manifest.get("extracted_sha256")),
        capture_dir,
    )
    if primary_record["mime"] not in PRIMARY_MIMES:
        raise MigrationError("schema-v2 primary MIME type cannot migrate to v3")
    if extracted_record["mime"] != "text/markdown":
        raise MigrationError("schema-v2 extracted artifact is not Markdown")
    asset_records = [
        {
            **_v3_file_record(path, digest, capture_dir),
            "role": "asset",
        }
        for path, digest in sorted(
            (
                (path, digest)
                for path, digest in artifacts.items()
                if path not in {raw_relative, extracted_relative}
            ),
            key=lambda item: item[0].as_posix(),
        )
    ]
    if any(record["mime"] not in ASSET_MIMES for record in asset_records):
        raise MigrationError("schema-v2 asset MIME type cannot migrate to v3")
    migrated = {
        "schema_version": 3,
        "source_id": "",
        "source": source,
        "captured_at": manifest.get("captured_at"),
        "title": manifest.get("title"),
        "primary": primary_record,
        "files": asset_records,
        "derived": {
            "extracted": extracted_record
        },
        "acquisition": {
            "adapter": adapter,
        },
    }
    if not isinstance(migrated["captured_at"], str) or not isinstance(
        migrated["title"], str
    ):
        raise MigrationError("schema-v2 capture metadata is incomplete")
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


def _replace_source_id_tokens(
    content: str, replacements: dict[str, str]
) -> str:
    updated = content
    for old_source_id, new_source_id in replacements.items():
        updated = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(old_source_id)}(?![A-Za-z0-9_])",
            new_source_id,
            updated,
        )
    return updated


def _git_dir(vault: Path) -> Path:
    result = run_git(vault, "rev-parse", "--absolute-git-dir")
    if result.returncode != 0:
        raise MigrationError("unable to resolve vault Git directory")
    return Path(result.stdout.strip()).resolve()


def migrate_captures_v3(
    config: dict[str, Any],
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Plan or atomically apply every schema-v2 capture migration."""
    vault = load_vault_path(config)
    git_dir = _git_dir(vault)
    if apply:
        try:
            active = inspect_active_operation(
                vault=vault,
                git_dir=git_dir,
                operation="capture_migration_v3",
            )
        except MutationError as exc:
            raise MigrationError(str(exc)) from exc
        if active is not None:
            return mutation_response(active, applied_status="migrated")
    archive_root = source_archive_dir(config, vault)
    v2_dirs: list[Path] = []
    if archive_root.is_dir():
        for path in sorted(archive_root.iterdir()):
            if path.is_symlink() or not path.is_dir():
                raise MigrationError(
                    "capture archive contains a non-directory entry"
                )
            manifest_path = path / "manifest.json"
            if manifest_path.is_symlink():
                raise MigrationError("capture archive contains a symbolic link")
            if not manifest_path.is_file():
                raise MigrationError("capture directory has no manifest")
            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MigrationError("capture manifest is unreadable") from exc
            if not isinstance(raw, dict):
                raise MigrationError("capture manifest must be a JSON object")
            schema_version = raw.get("schema_version")
            if schema_version == 2:
                v2_dirs.append(path)
            elif schema_version == 3:
                try:
                    load_validated_capture(
                        path,
                        source_id=path.name,
                        vault=vault,
                    )
                except CaptureError as exc:
                    raise MigrationError(
                        f"schema-v3 capture is invalid: {path.name}: {exc}"
                    ) from exc
            else:
                raise MigrationError(
                    f"unsupported capture schema version: {schema_version}"
                )
    if not v2_dirs:
        if apply:
            try:
                committed = latest_committed_operation(
                    vault=vault,
                    git_dir=git_dir,
                    operation="capture_migration_v3",
                )
            except MutationError as exc:
                raise MigrationError(str(exc)) from exc
            if committed is not None:
                return mutation_response(committed, applied_status="migrated")
        return {"status": "aligned", "migrated_captures": 0, "updated_notes": 0}

    migrations: list[tuple[Path, dict[str, Any], dict[Path, str], Path]] = []
    old_to_new: dict[str, str] = {}
    new_to_old: dict[str, str] = {}
    source_notes: set[Path] = set()
    collisions: dict[str, list[str]] = {}
    for capture_dir in v2_dirs:
        manifest, artifacts, source_note = _read_v2_capture(
            config, vault, capture_dir
        )
        old_source_id = capture_dir.name
        new_source_id = manifest["source_id"]
        if new_source_id in new_to_old:
            collisions.setdefault(
                new_source_id, [new_to_old[new_source_id]]
            ).append(old_source_id)
            continue
        target = archive_root / new_source_id
        if target.exists():
            raise MigrationError(f"migrated capture already exists: {new_source_id}")
        old_to_new[old_source_id] = new_source_id
        new_to_old[new_source_id] = old_source_id
        source_notes.add(source_note)
        migrations.append((capture_dir, manifest, artifacts, source_note))
    if collisions:
        return {
            "status": "dedupe_conflict",
            "migrated_captures": 0,
            "updated_notes": 0,
            "source_ids": old_to_new,
            "conflicts": collisions,
        }

    rewritten_notes: dict[Path, bytes] = {}
    for path in _tracked_markdown(config, vault):
        if path.is_symlink() or not path.is_file():
            raise MigrationError("tracked Markdown path is not a regular file")
        try:
            original = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationError("tracked Markdown note is not UTF-8") from exc
        updated = _replace_source_id_tokens(original, old_to_new)
        if path in source_notes:
            updated = _remove_frontmatter_domain(updated)
        if updated != original:
            rewritten_notes[path] = updated.encode("utf-8")
    for source_note in source_notes:
        if source_note not in rewritten_notes:
            raise MigrationError("schema-v2 source note was not selected for migration")

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
        {"migration": "captures-v3", "source_ids": old_to_new}
    )
    request_sha256 = source_identity_digest(
        {
            "migration": "captures-v3",
            "files": [
                {
                    "path": path.relative_to(vault).as_posix(),
                    "original_sha256": originals[path],
                    "original_mode": original_modes[path],
                    "final_sha256": finals[path],
                    "final_mode": final_modes[path],
                }
                for path in sorted(affected)
            ],
        }
    )
    transaction_id = f"txn_migrate_v3_{migration_digest[:16]}"

    def write(active_dir: Path) -> None:
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

    def postcondition(_active_dir: Path) -> None:
        for _old_dir, manifest, _artifacts, _source_note in migrations:
            load_validated_capture(
                archive_root / manifest["source_id"],
                source_id=manifest["source_id"],
                vault=vault,
                expected_manifest=manifest,
            )

    try:
        outcome = execute_journaled_mutation(
            vault=vault,
            git_dir=git_dir,
            transaction_id=transaction_id,
            transaction_sha256=migration_digest,
            request_sha256=request_sha256,
            operation="capture_migration_v3",
            receipt_data=result,
            affected=affected,
            expected_original_sha256=originals,
            expected_original_mode=original_modes,
            final_sha256=finals,
            final_mode=final_modes,
            write=write,
            postcondition=postcondition,
        )
    except MutationError as exc:
        raise MigrationError(str(exc)) from exc
    return mutation_response(outcome, applied_status="migrated")
