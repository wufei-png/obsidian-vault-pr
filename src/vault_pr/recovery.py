"""Durable journals for recovering interrupted semantic transactions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .git import run_git, vault_write_lock


class RecoveryError(RuntimeError):
    """Raised when an interrupted transaction cannot be recovered safely."""


TRANSACTION_ID_PATTERN = re.compile(r"txn_[A-Za-z0-9_-]{1,80}")
GIT_FILE_MODES = {"100644", "100755"}


def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def worktree_file_mode(path: Path) -> str | None:
    """Return the normalized Git mode for a regular worktree file."""
    if path.is_symlink():
        raise RecoveryError(f"transaction path is a symbolic link: {path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise RecoveryError(f"transaction path is not a regular file: {path}")
    return "100755" if path.stat().st_mode & 0o111 else "100644"


def _chmod_git_mode(path: Path, mode: str) -> None:
    if mode not in GIT_FILE_MODES:
        raise RecoveryError(f"unsupported regular-file Git mode: {mode}")
    path.chmod(0o755 if mode == "100755" else 0o644)


def _lexical_child(root: Path, raw: str, label: str) -> Path:
    """Resolve a relative child lexically and reject every symlink component."""
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
        raise RecoveryError(f"{label} escapes its root")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise RecoveryError(f"{label} traverses a symbolic link: {raw}")
    return current


def _normalize_transaction_path(vault: Path, path: Path, lexical_vault: Path) -> Path:
    lexical = Path(os.path.abspath(path.expanduser()))
    root = (
        lexical_vault
        if lexical.is_relative_to(lexical_vault)
        else vault
        if lexical.is_relative_to(vault)
        else None
    )
    if root is None or lexical == root:
        raise RecoveryError("affected path escapes the vault")
    relative = lexical.relative_to(root)
    _lexical_child(root, str(relative), "affected path")
    return _lexical_child(vault, str(relative), "affected path")


def _head_commit(vault: Path) -> str | None:
    result = run_git(vault, "rev-parse", "--verify", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else None


def _git_worktree_blob_oid(vault: Path, relative: str) -> str | None:
    """Return the blob OID Git would stage after applying configured filters."""
    path = vault / relative
    if not path.exists():
        return None
    result = subprocess.run(
        ["git", "-C", str(vault), "hash-object", "--", relative],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RecoveryError(f"unable to hash filtered worktree path: {relative}")
    oid = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", oid):
        raise RecoveryError(f"unexpected filtered blob metadata: {relative}")
    return oid


def _git_head_file_oid(vault: Path, relative: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(vault), "ls-tree", "-z", "HEAD", "--", relative],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RecoveryError(f"unable to inspect committed blob: {relative}")
    if not result.stdout:
        return None
    records = [record for record in result.stdout.split(b"\0") if record]
    if len(records) != 1 or b"\t" not in records[0]:
        raise RecoveryError(f"unexpected committed blob metadata: {relative}")
    fields = records[0].split(b"\t", 1)[0].split()
    if len(fields) != 3:
        raise RecoveryError(f"unexpected committed blob metadata: {relative}")
    return fields[2].decode("ascii")


def _git_head_file_mode(vault: Path, relative: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(vault), "ls-tree", "-z", "HEAD", "--", relative],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RecoveryError(f"unable to inspect committed mode: {relative}")
    if not result.stdout:
        return None
    records = [record for record in result.stdout.split(b"\0") if record]
    if len(records) != 1 or b"\t" not in records[0]:
        raise RecoveryError(f"unexpected committed mode metadata: {relative}")
    return records[0].split(b" ", 1)[0].decode("ascii")


def _git_is_clean(vault: Path) -> bool:
    status = run_git(vault, "status", "--porcelain=v1", "--untracked-files=all")
    if status.returncode != 0:
        raise RecoveryError("unable to inspect vault Git status")
    return not status.stdout


def _git_status_paths(vault: Path) -> tuple[set[str], set[str]]:
    status = run_git(vault, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status.returncode != 0:
        raise RecoveryError("unable to inspect vault Git status")
    dirty: set[str] = set()
    staged: set[str] = set()
    fields = [field for field in status.stdout.split("\0") if field]
    index = 0
    while index < len(fields):
        field = fields[index]
        code = field[:2]
        path = field[3:]
        is_staged = code[0] not in {" ", "?"}
        dirty.add(path)
        if is_staged:
            staged.add(path)
        if "R" in code or "C" in code:
            index += 1
            if index >= len(fields):
                raise RecoveryError("invalid Git rename status")
            other_path = fields[index]
            dirty.add(other_path)
            if is_staged:
                staged.add(other_path)
        index += 1
    return dirty, staged


def _git_index_file_oid(vault: Path, relative: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(vault), "ls-files", "--stage", "-z", "--", relative],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RecoveryError(f"unable to inspect staged blob: {relative}")
    if not result.stdout:
        return None
    records = [record for record in result.stdout.split(b"\0") if record]
    if len(records) != 1 or b"\t" not in records[0]:
        raise RecoveryError(f"unexpected staged blob metadata: {relative}")
    fields = records[0].split(b"\t", 1)[0].split()
    if len(fields) != 3:
        raise RecoveryError(f"unexpected staged blob metadata: {relative}")
    return fields[1].decode("ascii")


def _git_index_file_mode(vault: Path, relative: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(vault), "ls-files", "--stage", "-z", "--", relative],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RecoveryError(f"unable to inspect staged mode: {relative}")
    if not result.stdout:
        return None
    records = [record for record in result.stdout.split(b"\0") if record]
    if len(records) != 1 or b"\t" not in records[0]:
        raise RecoveryError(f"unexpected staged mode metadata: {relative}")
    return records[0].split(b" ", 1)[0].decode("ascii")


def _committed_paths(vault: Path, base_commit: str | None, head: str) -> set[str]:
    if base_commit is not None:
        ancestor = run_git(vault, "merge-base", "--is-ancestor", base_commit, head)
        if ancestor.returncode != 0:
            raise RecoveryError(
                "transaction HEAD is not descended from its base commit"
            )
        revision_range = f"{base_commit}..{head}"
    else:
        revision_range = head
    revisions = run_git(vault, "rev-list", "--reverse", revision_range)
    if revisions.returncode != 0:
        raise RecoveryError("unable to inspect transaction commit range")
    paths: set[str] = set()
    for commit in revisions.stdout.splitlines():
        changed = subprocess.run(
            [
                "git",
                "-C",
                str(vault),
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "--no-renames",
                "-r",
                "-m",
                "-z",
                commit,
            ],
            check=False,
            capture_output=True,
        )
        if changed.returncode != 0:
            raise RecoveryError("unable to inspect transaction commit paths")
        paths.update(
            value.decode("utf-8", errors="surrogateescape")
            for value in changed.stdout.split(b"\0")
            if value
        )
    return paths


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
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise RecoveryError("active transaction root is not a regular directory")
    if not root.exists():
        return []
    active: list[Path] = []
    for path in root.iterdir():
        if path.is_symlink() or not path.is_dir():
            raise RecoveryError(
                f"active transaction root contains an invalid entry: {path.name}"
            )
        active.append(path)
    return sorted(active)


def prepare_active_transaction(
    *,
    vault: Path,
    git_dir: Path,
    transaction_id: str,
    transaction_sha256: str,
    affected: set[Path],
    expected_original_sha256: dict[Path, str | None],
    expected_original_mode: dict[Path, str | None],
    final_sha256: dict[Path, str | None],
    final_mode: dict[Path, str | None],
) -> Path:
    """Persist originals and allowed states before the first vault mutation."""
    lexical_vault = Path(os.path.abspath(vault.expanduser()))
    vault = vault.resolve()
    git_dir = git_dir.resolve()
    affected = {
        _normalize_transaction_path(vault, path, lexical_vault) for path in affected
    }
    expected_original_sha256 = {
        _normalize_transaction_path(vault, path, lexical_vault): value
        for path, value in expected_original_sha256.items()
    }
    expected_original_mode = {
        _normalize_transaction_path(vault, path, lexical_vault): value
        for path, value in expected_original_mode.items()
    }
    final_sha256 = {
        _normalize_transaction_path(vault, path, lexical_vault): value
        for path, value in final_sha256.items()
    }
    final_mode = {
        _normalize_transaction_path(vault, path, lexical_vault): value
        for path, value in final_mode.items()
    }
    if not TRANSACTION_ID_PATTERN.fullmatch(transaction_id):
        raise RecoveryError("invalid transaction id")
    if not affected:
        raise RecoveryError("a recovery transaction must affect at least one path")
    if (
        set(expected_original_sha256) != affected
        or set(expected_original_mode) != affected
        or set(final_sha256) != affected
        or set(final_mode) != affected
    ):
        raise RecoveryError(
            "original and final states must cover every affected path exactly"
        )
    active_root = active_transaction_root(git_dir)
    active = list_active_transactions(git_dir)
    if active:
        raise RecoveryError(
            "an interrupted vault-pr transaction requires recovery: "
            + ", ".join(path.name for path in active)
        )
    try:
        active_root.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise RecoveryError("unable to create active transaction root") from exc
    if active_root.is_symlink() or not active_root.is_dir():
        raise RecoveryError("active transaction root is not a regular directory")
    active_dir = active_root / transaction_id
    try:
        active_dir.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise RecoveryError(
            f"active transaction path is already occupied: {transaction_id}"
        ) from exc
    backups = active_dir / "backups"
    backups.mkdir()
    entries: list[dict[str, Any]] = []
    try:
        for index, path in enumerate(sorted(affected)):
            relative = str(path.relative_to(vault))
            expected_original = expected_original_sha256[path]
            expected_mode = expected_original_mode[path]
            requested_final = final_sha256[path]
            requested_final_mode = final_mode[path]
            if (expected_original is None) != (expected_mode is None) or (
                requested_final is None
            ) != (requested_final_mode is None):
                raise RecoveryError(
                    f"hash and mode disagree about path presence: {relative}"
                )
            if expected_mode not in GIT_FILE_MODES | {None} or (
                requested_final_mode not in GIT_FILE_MODES | {None}
            ):
                raise RecoveryError(f"unsupported Git file mode: {relative}")
            original_bytes = path.read_bytes() if path.is_file() else None
            original = (
                hashlib.sha256(original_bytes).hexdigest()
                if original_bytes is not None
                else None
            )
            original_mode = worktree_file_mode(path)
            if original != expected_original or original_mode != expected_mode:
                raise RecoveryError(
                    f"affected path changed before transaction prepare: {relative}"
                )
            backup_name: str | None = None
            if original is not None:
                backup_name = f"backups/{index}"
                backup_path = active_dir / backup_name
                backup_path.write_bytes(original_bytes)
                _chmod_git_mode(backup_path, original_mode)
                if (
                    _sha256(backup_path) != original
                    or worktree_file_mode(backup_path) != original_mode
                ):
                    raise RecoveryError(f"failed to verify backup for {relative}")
            allowed = {original, final_sha256.get(path)}
            entries.append(
                {
                    "path": relative,
                    "original_sha256": original,
                    "original_mode": original_mode,
                    "final_sha256": requested_final,
                    "final_mode": requested_final_mode,
                    "allowed_sha256": sorted(value for value in allowed if value),
                    "allow_missing": None in allowed,
                    "backup": backup_name,
                }
            )
        created_dirs: set[Path] = set()
        for path in affected:
            parent = path.parent
            while parent != vault:
                if not parent.exists():
                    created_dirs.add(parent)
                parent = parent.parent
        journal = {
            "schema_version": 3,
            "transaction_id": transaction_id,
            "transaction_sha256": transaction_sha256,
            "vault": str(vault),
            "phase": "prepared",
            "base_commit": _head_commit(vault),
            "created_dirs": [
                str(path.relative_to(vault))
                for path in sorted(
                    created_dirs, key=lambda item: (len(item.parts), str(item))
                )
            ],
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
    if journal.get("schema_version") not in {1, 2, 3}:
        raise RecoveryError("recovery journal has an unsupported schema version")
    phase = journal.get("phase", "prepared")
    if phase not in {"prepared", "applied"}:
        raise RecoveryError("recovery journal has an invalid phase")
    return journal


def mark_active_applied(active_dir: Path, completion_path: str | None = None) -> None:
    """Durably record that vault writes finished and now require a Git commit."""
    active_dir = active_dir.resolve()
    journal_path = active_dir / "journal.json"
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"invalid recovery journal: {active_dir}") from exc
    if journal.get("phase") != "prepared":
        raise RecoveryError("only a prepared transaction can be marked applied")
    if completion_path is not None:
        completion = Path(completion_path)
        if completion.is_absolute() or ".." in completion.parts:
            raise RecoveryError("transaction completion path must be vault-relative")
    journal["phase"] = "applied"
    journal["applied_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    journal["completion_path"] = completion_path
    _atomic_json(journal_path, journal)


def _validated_entries(
    vault: Path, active_dir: Path, journal: dict[str, Any]
) -> list[tuple[dict[str, Any], Path, Path | None]]:
    resolved_entries: list[tuple[dict[str, Any], Path, Path | None]] = []
    seen_paths: set[Path] = set()
    mode_aware = journal.get("schema_version") == 3
    for entry in journal["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise RecoveryError("recovery journal contains an invalid path entry")
        path = _lexical_child(vault, entry["path"], "recovery journal path")
        if path in seen_paths:
            raise RecoveryError("recovery journal contains duplicate paths")
        seen_paths.add(path)
        original = entry.get("original_sha256")
        final = entry.get("final_sha256")
        backup_name = entry.get("backup")
        if final is not None and not isinstance(final, str):
            raise RecoveryError("recovery journal contains an invalid final hash")
        if mode_aware:
            original_mode = entry.get("original_mode")
            final_mode = entry.get("final_mode")
            if original_mode not in GIT_FILE_MODES | {None} or (
                final_mode not in GIT_FILE_MODES | {None}
            ):
                raise RecoveryError("recovery journal contains an invalid Git mode")
            if (original is None) != (original_mode is None) or (final is None) != (
                final_mode is None
            ):
                raise RecoveryError(
                    "recovery journal hash and mode disagree about path presence"
                )
        backup: Path | None = None
        if original is None:
            if backup_name is not None:
                raise RecoveryError("recovery journal has an unexpected backup")
        elif not isinstance(original, str) or not isinstance(backup_name, str):
            raise RecoveryError("recovery journal has invalid original backup metadata")
        else:
            backup = _lexical_child(
                active_dir, backup_name, "recovery journal backup path"
            )
            if (
                not backup.is_file()
                or _sha256(backup) != original
                or (
                    mode_aware
                    and worktree_file_mode(backup) != entry.get("original_mode")
                )
            ):
                raise RecoveryError(
                    f"recovery backup integrity failed: {entry['path']}"
                )
        resolved_entries.append((entry, path, backup))
    return resolved_entries


def _created_directories(vault: Path, journal: dict[str, Any]) -> list[Path]:
    raw_directories = journal.get("created_dirs", [])
    if not isinstance(raw_directories, list) or not all(
        isinstance(value, str) for value in raw_directories
    ):
        raise RecoveryError("recovery journal contains invalid created directories")
    directories: list[Path] = []
    for raw in raw_directories:
        path = _lexical_child(vault, raw, "recovery journal directory")
        directories.append(path)
    if len(set(directories)) != len(directories):
        raise RecoveryError("recovery journal contains duplicate created directories")
    return sorted(directories, key=lambda item: len(item.parts), reverse=True)


def _validate_archive_contents(
    archive: Path,
    journal: dict[str, Any],
    resolved_entries: list[tuple[dict[str, Any], Path, Path | None]],
) -> None:
    """Refuse undeclared archive content before rollback mutates any path."""
    if not archive.exists():
        return
    if archive.is_symlink() or not archive.is_dir():
        raise RecoveryError("transaction archive is not a regular directory")
    if journal.get("schema_version") == 1:
        raise RecoveryError(
            "legacy recovery journal cannot safely remove a transaction archive"
        )
    expected_files: set[Path] = set()
    expected_directories = {archive}
    for _entry, path, _backup in resolved_entries:
        if not path.is_relative_to(archive):
            continue
        expected_files.add(path)
        parent = path.parent
        while parent.is_relative_to(archive):
            expected_directories.add(parent)
            if parent == archive:
                break
            parent = parent.parent
    unexpected: list[str] = []
    for path in archive.rglob("*"):
        if path.is_symlink():
            unexpected.append(str(path.relative_to(archive)))
        elif path.is_dir():
            if path not in expected_directories:
                unexpected.append(str(path.relative_to(archive)) + "/")
        elif path not in expected_files:
            unexpected.append(str(path.relative_to(archive)))
    if unexpected:
        raise RecoveryError(
            "undeclared transaction archive content blocks recovery: "
            + ", ".join(sorted(unexpected))
        )


def rollback_active_transaction(vault: Path, active_dir: Path) -> dict[str, Any]:
    """Restore a journal only when all affected paths remain in known states."""
    vault = vault.resolve()
    active_dir = active_dir.resolve()
    journal = _read_journal(vault, active_dir)
    legacy_journal = journal.get("schema_version") == 1
    mode_aware = journal.get("schema_version") == 3
    expected_dirty: set[str] = set()
    resolved_entries = _validated_entries(vault, active_dir, journal)
    for entry, path, _backup in resolved_entries:
        current = _sha256(path)
        if mode_aware:
            current_state = (current, worktree_file_mode(path))
            known = current_state in {
                (entry.get("original_sha256"), entry.get("original_mode")),
                (entry.get("final_sha256"), entry.get("final_mode")),
            }
        else:
            allowed_raw = entry.get("allowed_sha256")
            if not isinstance(allowed_raw, list) or not all(
                isinstance(value, str) for value in allowed_raw
            ):
                raise RecoveryError("recovery journal contains invalid allowed hashes")
            allowed = set(allowed_raw)
            known = (
                bool(entry.get("allow_missing"))
                if current is None
                else current in allowed
            )
        if not known:
            raise RecoveryError(
                f"affected note has an unexpected state: {entry['path']}"
            )
        expected_dirty.add(entry["path"])
        if legacy_journal:
            expected_dirty.add(entry["path"] + ".vault-pr-tmp")

    transaction_id = journal.get("transaction_id")
    archive_prefix = f".vault-pr/transactions/{transaction_id}"
    archive = _lexical_child(vault, archive_prefix, "transaction archive path")
    _validate_archive_contents(archive, journal, resolved_entries)
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
    unexpected = sorted(path for path in dirty_paths if path not in expected_dirty)
    if unexpected:
        raise RecoveryError(
            "unrelated vault changes block recovery: " + ", ".join(unexpected)
        )

    for entry, path, backup in resolved_entries:
        path = _lexical_child(vault, entry["path"], "recovery journal path")
        if legacy_journal:
            path.with_name(path.name + ".vault-pr-tmp").unlink(missing_ok=True)
        if backup is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = active_dir / "restore-staging" / entry["path"]
            temporary.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, temporary)
            if mode_aware:
                _chmod_git_mode(temporary, entry["original_mode"])
            os.replace(temporary, path)

    for directory in _created_directories(vault, journal):
        if directory.is_dir():
            try:
                directory.rmdir()
            except OSError as exc:
                raise RecoveryError(
                    f"new transaction directory is not empty: {directory.relative_to(vault)}"
                ) from exc
    for entry, path, _backup in resolved_entries:
        path = _lexical_child(vault, entry["path"], "recovery journal path")
        if _sha256(path) != entry.get("original_sha256"):
            raise RecoveryError(f"recovery verification failed: {entry['path']}")
        if mode_aware and worktree_file_mode(path) != entry.get("original_mode"):
            raise RecoveryError(f"recovery mode verification failed: {entry['path']}")
        if legacy_journal and path.with_name(path.name + ".vault-pr-tmp").exists():
            raise RecoveryError(f"recovery temporary file remains: {entry['path']}")
    shutil.rmtree(active_dir)
    return {"status": "recovered", "transaction_id": transaction_id}


def _finalize_active_transaction_unlocked(
    vault: Path, active_dir: Path
) -> dict[str, Any]:
    journal = _read_journal(vault, active_dir)
    if journal.get("phase") != "applied":
        raise RecoveryError("transaction has not reached the applied phase")
    if not _git_is_clean(vault):
        raise RecoveryError("applied transaction must be committed before finalization")
    head = _head_commit(vault)
    base_commit = journal.get("base_commit")
    if base_commit is not None and not isinstance(base_commit, str):
        raise RecoveryError("recovery journal contains an invalid base commit")
    if head is None or head == base_commit:
        raise RecoveryError("applied transaction has no new Git commit")
    expected_committed_paths: set[str] = set()
    for entry, path, _backup in _validated_entries(vault, active_dir, journal):
        final = entry.get("final_sha256")
        final_mode = (
            entry.get("final_mode") if journal.get("schema_version") == 3 else None
        )
        if _sha256(path) != final or (
            journal.get("schema_version") == 3
            and worktree_file_mode(path) != final_mode
        ):
            raise RecoveryError(
                f"applied path does not match final state: {entry['path']}"
            )
        if _git_head_file_oid(vault, entry["path"]) != _git_worktree_blob_oid(
            vault, entry["path"]
        ) or (
            journal.get("schema_version") == 3
            and _git_head_file_mode(vault, entry["path"]) != final_mode
        ):
            raise RecoveryError(
                f"Git commit does not contain final path state: {entry['path']}"
            )
        if entry.get("original_sha256") != final or (
            journal.get("schema_version") == 3
            and entry.get("original_mode") != final_mode
        ):
            expected_committed_paths.add(entry["path"])
    committed_paths = _committed_paths(vault, base_commit, head)
    if committed_paths != expected_committed_paths:
        extra = sorted(committed_paths - expected_committed_paths)
        missing = sorted(expected_committed_paths - committed_paths)
        details = []
        if extra:
            details.append("extra=" + ", ".join(extra))
        if missing:
            details.append("missing=" + ", ".join(missing))
        raise RecoveryError(
            "transaction commit range does not match journal paths: "
            + "; ".join(details)
        )
    completion_path = journal.get("completion_path")
    if completion_path is not None:
        if not isinstance(completion_path, str):
            raise RecoveryError("recovery journal has an invalid completion path")
        completion = _lexical_child(
            vault, completion_path, "transaction completion path"
        )
        if not completion.is_file() or _git_head_file_oid(
            vault, completion_path
        ) != _git_worktree_blob_oid(vault, completion_path):
            raise RecoveryError(
                "Git commit does not contain the transaction audit record"
            )
    transaction_id = journal["transaction_id"]
    shutil.rmtree(active_dir)
    return {"status": "finalized", "transaction_id": transaction_id, "commit": head}


def finalize_applied_transaction(
    vault: Path, git_dir: Path, transaction_id: str
) -> dict[str, Any]:
    """Finalize one applied transaction only after its exact result is committed."""
    vault = vault.resolve()
    git_dir = git_dir.resolve()
    if not TRANSACTION_ID_PATTERN.fullmatch(transaction_id):
        raise RecoveryError("invalid transaction id")
    with vault_write_lock(vault):
        active_dir = active_transaction_root(git_dir) / transaction_id
        if not active_dir.is_dir() or active_dir.is_symlink():
            raise RecoveryError(f"active transaction not found: {transaction_id}")
        return _finalize_active_transaction_unlocked(vault, active_dir.resolve())


def commit_applied_transaction(
    vault: Path,
    git_dir: Path,
    transaction_id: str,
    message: str,
) -> dict[str, Any]:
    """Stage exact journal paths, commit them under lock, and finalize."""
    vault = vault.resolve()
    git_dir = git_dir.resolve()
    if not TRANSACTION_ID_PATTERN.fullmatch(transaction_id):
        raise RecoveryError("invalid transaction id")
    message = " ".join(message.split()).strip()
    if not message:
        raise RecoveryError("commit message must not be empty")
    with vault_write_lock(vault):
        active_dir = active_transaction_root(git_dir) / transaction_id
        if not active_dir.is_dir() or active_dir.is_symlink():
            raise RecoveryError(f"active transaction not found: {transaction_id}")
        active_dir = active_dir.resolve()
        journal = _read_journal(vault, active_dir)
        if journal.get("phase") != "applied":
            raise RecoveryError("transaction has not reached the applied phase")
        entries = _validated_entries(vault, active_dir, journal)
        expected_paths: set[str] = set()
        final_by_path: dict[str, tuple[str | None, str | None]] = {}
        for entry, path, _backup in entries:
            final = entry.get("final_sha256")
            final_mode = (
                entry.get("final_mode") if journal.get("schema_version") == 3 else None
            )
            if _sha256(path) != final or (
                journal.get("schema_version") == 3
                and worktree_file_mode(path) != final_mode
            ):
                raise RecoveryError(
                    f"applied path does not match final state: {entry['path']}"
                )
            if entry.get("original_sha256") != final or (
                journal.get("schema_version") == 3
                and entry.get("original_mode") != final_mode
            ):
                expected_paths.add(entry["path"])
                final_by_path[entry["path"]] = (final, final_mode)
        if not expected_paths:
            raise RecoveryError("applied transaction has no paths to commit")

        dirty, staged = _git_status_paths(vault)
        unexpected_dirty = sorted(dirty - expected_paths)
        unexpected_staged = sorted(staged - expected_paths)
        if unexpected_dirty:
            raise RecoveryError(
                "unrelated vault changes block commit: " + ", ".join(unexpected_dirty)
            )
        if unexpected_staged:
            raise RecoveryError(
                "unrelated staged changes block commit: " + ", ".join(unexpected_staged)
            )

        added = run_git(vault, "add", "--all", "--", *sorted(expected_paths))
        if added.returncode != 0:
            detail = (added.stderr or added.stdout).strip()
            raise RecoveryError(f"unable to stage transaction paths: {detail}")
        _dirty_after, staged_after = _git_status_paths(vault)
        if staged_after != expected_paths:
            extra = sorted(staged_after - expected_paths)
            missing = sorted(expected_paths - staged_after)
            details = []
            if extra:
                details.append("extra=" + ", ".join(extra))
            if missing:
                details.append("missing=" + ", ".join(missing))
            raise RecoveryError(
                "staged paths do not match transaction journal: " + "; ".join(details)
            )
        for relative, (final, final_mode) in final_by_path.items():
            if _git_index_file_oid(vault, relative) != _git_worktree_blob_oid(
                vault, relative
            ) or (
                journal.get("schema_version") == 3
                and _git_index_file_mode(vault, relative) != final_mode
            ):
                raise RecoveryError(
                    f"staged state does not match transaction journal: {relative}"
                )

        committed = run_git(vault, "commit", "-m", message)
        if committed.returncode != 0:
            detail = (committed.stderr or committed.stdout).strip()
            raise RecoveryError(f"unable to commit transaction: {detail}")
        finalized = _finalize_active_transaction_unlocked(vault, active_dir)
        return {
            **finalized,
            "status": "committed_and_finalized",
            "message": message,
        }


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
        journal = _read_journal(vault, active_dir)
        if journal.get("phase") == "applied" and _git_is_clean(vault):
            if _head_commit(vault) != journal.get("base_commit"):
                return _finalize_active_transaction_unlocked(vault, active_dir)
        return rollback_active_transaction(vault, active_dir)
