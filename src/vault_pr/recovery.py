"""Durable journals for recovering interrupted semantic transactions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from .git import run_git, vault_write_lock


class RecoveryError(RuntimeError):
    """Raised when an interrupted transaction cannot be recovered safely."""


TRANSACTION_ID_PATTERN = re.compile(r"txn_[A-Za-z0-9_-]{1,80}")


def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def active_transaction_root(git_dir: Path) -> Path:
    """Return the private Git-directory location for active journals."""
    return git_dir / "vault-pr-active"


def list_active_transactions(git_dir: Path) -> list[Path]:
    """List durable active transaction directories."""
    root = active_transaction_root(git_dir)
    return (
        sorted(
            path for path in root.iterdir() if path.is_dir() and not path.is_symlink()
        )
        if root.is_dir()
        else []
    )


def prepare_active_transaction(
    *,
    vault: Path,
    git_dir: Path,
    transaction_id: str,
    transaction_sha256: str,
    affected: set[Path],
    final_sha256: dict[Path, str | None],
) -> Path:
    """Persist originals and allowed states before the first vault mutation."""
    vault = vault.resolve()
    git_dir = git_dir.resolve()
    affected = {path.resolve() for path in affected}
    final_sha256 = {path.resolve(): value for path, value in final_sha256.items()}
    active = list_active_transactions(git_dir)
    if active:
        raise RecoveryError(
            "an interrupted vault-pr transaction requires recovery: "
            + ", ".join(path.name for path in active)
        )
    active_dir = active_transaction_root(git_dir) / transaction_id
    backups = active_dir / "backups"
    backups.mkdir(parents=True)
    entries: list[dict[str, Any]] = []
    try:
        for index, path in enumerate(sorted(affected)):
            relative = str(path.relative_to(vault))
            original = _sha256(path)
            backup_name: str | None = None
            if original is not None:
                backup_name = f"backups/{index}"
                backup_path = active_dir / backup_name
                shutil.copy2(path, backup_path)
                if _sha256(backup_path) != original:
                    raise RecoveryError(f"failed to verify backup for {relative}")
            allowed = {original, final_sha256.get(path)}
            entries.append(
                {
                    "path": relative,
                    "original_sha256": original,
                    "allowed_sha256": sorted(value for value in allowed if value),
                    "allow_missing": None in allowed,
                    "backup": backup_name,
                }
            )
        journal = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "transaction_sha256": transaction_sha256,
            "vault": str(vault),
            "entries": entries,
        }
        _atomic_json(active_dir / "journal.json", journal)
    except Exception:
        shutil.rmtree(active_dir, ignore_errors=True)
        raise
    return active_dir


def _read_journal(vault: Path, active_dir: Path) -> dict[str, Any]:
    try:
        journal = json.loads((active_dir / "journal.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"invalid recovery journal: {active_dir}") from exc
    if not isinstance(journal, dict) or journal.get("vault") != str(vault):
        raise RecoveryError("recovery journal does not match the configured vault")
    transaction_id = journal.get("transaction_id")
    if (
        not isinstance(transaction_id, str)
        or not TRANSACTION_ID_PATTERN.fullmatch(transaction_id)
        or transaction_id != active_dir.name
    ):
        raise RecoveryError("recovery journal has an invalid transaction id")
    entries = journal.get("entries")
    if not isinstance(entries, list) or not entries:
        raise RecoveryError("recovery journal has no affected paths")
    return journal


def rollback_active_transaction(vault: Path, active_dir: Path) -> dict[str, Any]:
    """Restore a journal only when all affected paths remain in known states."""
    vault = vault.resolve()
    active_dir = active_dir.resolve()
    journal = _read_journal(vault, active_dir)
    entries = journal["entries"]
    expected_dirty: set[str] = set()
    resolved_entries: list[tuple[dict[str, Any], Path, Path | None]] = []
    seen_paths: set[Path] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise RecoveryError("recovery journal contains an invalid path entry")
        path = (vault / entry["path"]).resolve()
        if not path.is_relative_to(vault):
            raise RecoveryError("recovery journal path escapes the vault")
        if path in seen_paths:
            raise RecoveryError("recovery journal contains duplicate paths")
        seen_paths.add(path)
        current = _sha256(path)
        allowed_raw = entry.get("allowed_sha256")
        if not isinstance(allowed_raw, list) or not all(
            isinstance(value, str) for value in allowed_raw
        ):
            raise RecoveryError("recovery journal contains invalid allowed hashes")
        allowed = set(allowed_raw)
        if current is None:
            known = bool(entry.get("allow_missing"))
        else:
            known = current in allowed
        if not known:
            raise RecoveryError(
                f"affected note has an unexpected state: {entry['path']}"
            )
        expected_dirty.add(entry["path"])
        expected_dirty.add(entry["path"] + ".vault-pr-tmp")
        original = entry.get("original_sha256")
        backup_name = entry.get("backup")
        backup: Path | None = None
        if original is None:
            if backup_name is not None:
                raise RecoveryError("recovery journal has an unexpected backup")
        elif not isinstance(original, str) or not isinstance(backup_name, str):
            raise RecoveryError("recovery journal has invalid original backup metadata")
        else:
            backup = (active_dir / backup_name).resolve()
            if (
                not backup.is_relative_to(active_dir)
                or not backup.is_file()
                or _sha256(backup) != original
            ):
                raise RecoveryError(
                    f"recovery backup integrity failed: {entry['path']}"
                )
        resolved_entries.append((entry, path, backup))

    transaction_id = journal.get("transaction_id")
    archive_prefix = f".vault-pr/transactions/{transaction_id}"
    archive = (vault / archive_prefix).resolve()
    if archive.exists() and not archive.is_relative_to(vault):
        raise RecoveryError("transaction archive path escapes the vault")
    status = run_git(vault, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status.returncode != 0:
        raise RecoveryError("unable to inspect vault status before recovery")
    dirty_paths: set[str] = set()
    staged_paths: list[str] = []
    fields = [field for field in status.stdout.split("\0") if field]
    index = 0
    while index < len(fields):
        field = fields[index]
        code = field[:2]
        path = field[3:]
        if code[0] not in {" ", "?"}:
            staged_paths.append(path)
        dirty_paths.add(path)
        if "R" in code or "C" in code:
            index += 1
            if index >= len(fields):
                raise RecoveryError("invalid Git rename status during recovery")
            dirty_paths.add(fields[index])
        index += 1
    if staged_paths:
        raise RecoveryError(
            "staged vault changes block recovery: " + ", ".join(sorted(staged_paths))
        )
    unexpected = sorted(
        path
        for path in dirty_paths
        if path not in expected_dirty and not path.startswith(archive_prefix + "/")
    )
    if unexpected:
        raise RecoveryError(
            "unrelated vault changes block recovery: " + ", ".join(unexpected)
        )

    for entry, path, backup in resolved_entries:
        temporary = path.with_name(path.name + ".vault-pr-tmp")
        temporary.unlink(missing_ok=True)
        if backup is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".vault-pr-tmp")
            shutil.copy2(backup, temporary)
            os.replace(temporary, path)

    if archive.is_relative_to(vault) and archive.is_dir():
        shutil.rmtree(archive)
    for entry, path, _backup in resolved_entries:
        if _sha256(path) != entry.get("original_sha256"):
            raise RecoveryError(f"recovery verification failed: {entry['path']}")
        if path.with_name(path.name + ".vault-pr-tmp").exists():
            raise RecoveryError(f"recovery temporary file remains: {entry['path']}")
    shutil.rmtree(active_dir)
    return {"status": "recovered", "transaction_id": transaction_id}


def recover_interrupted_transaction(vault: Path, git_dir: Path) -> dict[str, Any]:
    """Recover the single active transaction under the vault write lock."""
    vault = vault.resolve()
    git_dir = git_dir.resolve()
    with vault_write_lock(vault):
        active = list_active_transactions(git_dir)
        if len(active) != 1:
            raise RecoveryError(
                f"expected exactly one active transaction, found {len(active)}"
            )
        active_dir = active[0]
        if not (active_dir / "journal.json").is_file():
            if not TRANSACTION_ID_PATTERN.fullmatch(active_dir.name):
                raise RecoveryError("incomplete recovery directory has an invalid id")
            status = run_git(vault, "status", "--porcelain=v1")
            if status.returncode != 0 or status.stdout:
                raise RecoveryError(
                    "incomplete prepare directory can only be removed from a clean vault"
                )
            shutil.rmtree(active_dir)
            return {
                "status": "abandoned_prepare_removed",
                "transaction_id": active_dir.name,
            }
        return rollback_active_transaction(vault, active_dir)
