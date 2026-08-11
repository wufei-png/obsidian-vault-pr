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

from .files import atomic_replace_text
from .git import run_git, vault_write_lock


class RecoveryError(RuntimeError):
    """Raised when an interrupted transaction cannot be recovered safely."""


TRANSACTION_ID_PATTERN = re.compile(r"txn_[A-Za-z0-9_-]{1,80}")
OPERATION_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
GIT_FILE_MODES = {"100644", "100755"}


def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _vault_id(vault: Path) -> str:
    """Bind a journal to one configured vault without storing its path."""
    return hashlib.sha256(str(vault.resolve()).encode("utf-8")).hexdigest()


def worktree_file_mode(path: Path) -> str | None:
    """Return the normalized Git mode for a regular worktree file."""
    if path.is_symlink():
        raise RecoveryError("transaction path is a symbolic link")
    if not path.exists():
        return None
    if not path.is_file():
        raise RecoveryError("transaction path is not a regular file")
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


def _git_revision_file_metadata(
    vault: Path, revision: str, relative: str
) -> tuple[str | None, str | None]:
    result = subprocess.run(
        ["git", "-C", str(vault), "ls-tree", "-z", revision, "--", relative],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RecoveryError(f"unable to inspect committed path: {relative}")
    if not result.stdout:
        return None, None
    records = [record for record in result.stdout.split(b"\0") if record]
    if len(records) != 1 or b"\t" not in records[0]:
        raise RecoveryError(f"unexpected committed path metadata: {relative}")
    fields = records[0].split(b"\t", 1)[0].split()
    if len(fields) != 3:
        raise RecoveryError(f"unexpected committed path metadata: {relative}")
    return fields[2].decode("ascii"), fields[0].decode("ascii")


def _git_revision_file_oid(vault: Path, revision: str, relative: str) -> str | None:
    return _git_revision_file_metadata(vault, revision, relative)[0]


def _git_revision_file_mode(vault: Path, revision: str, relative: str) -> str | None:
    return _git_revision_file_metadata(vault, revision, relative)[1]


def _git_head_file_oid(vault: Path, relative: str) -> str | None:
    return _git_revision_file_oid(vault, "HEAD", relative)


def _git_head_file_mode(vault: Path, relative: str) -> str | None:
    return _git_revision_file_mode(vault, "HEAD", relative)


def _git_is_clean(vault: Path) -> bool:
    status = run_git(vault, "status", "--porcelain=v1", "--untracked-files=all")
    if status.returncode != 0:
        raise RecoveryError("unable to inspect vault Git status")
    return not status.stdout


def _parse_git_status_paths(output: str) -> tuple[set[str], set[str]]:
    """Return dirty and staged paths, including both sides of renames and copies."""
    dirty: set[str] = set()
    staged: set[str] = set()
    fields = [field for field in output.split("\0") if field]
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


def _git_status_paths(vault: Path) -> tuple[set[str], set[str]]:
    status = run_git(vault, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status.returncode != 0:
        raise RecoveryError("unable to inspect vault Git status")
    return _parse_git_status_paths(status.stdout)


def _run_git_with_index(
    vault: Path,
    index_path: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["GIT_INDEX_FILE"] = str(index_path)
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _git_index_file_oid(
    vault: Path, relative: str, *, index_path: Path | None = None
) -> str | None:
    command = ["ls-files", "--stage", "-z", "--", relative]
    result = subprocess.run(
        ["git", "-C", str(vault), *command],
        check=False,
        capture_output=True,
        env=(
            {**os.environ, "GIT_INDEX_FILE": str(index_path)}
            if index_path is not None
            else None
        ),
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


def _git_index_file_mode(
    vault: Path, relative: str, *, index_path: Path | None = None
) -> str | None:
    command = ["ls-files", "--stage", "-z", "--", relative]
    result = subprocess.run(
        ["git", "-C", str(vault), *command],
        check=False,
        capture_output=True,
        env=(
            {**os.environ, "GIT_INDEX_FILE": str(index_path)}
            if index_path is not None
            else None
        ),
    )
    if result.returncode != 0:
        raise RecoveryError(f"unable to inspect staged mode: {relative}")
    if not result.stdout:
        return None
    records = [record for record in result.stdout.split(b"\0") if record]
    if len(records) != 1 or b"\t" not in records[0]:
        raise RecoveryError(f"unexpected staged mode metadata: {relative}")
    return records[0].split(b" ", 1)[0].decode("ascii")


def _changed_journal_paths(journal: dict[str, Any]) -> set[str]:
    return {
        entry["path"]
        for entry in journal["entries"]
        if entry.get("original_sha256") != entry.get("final_sha256")
        or entry.get("original_mode") != entry.get("final_mode")
    }


def _unstage_paths(vault: Path, paths: set[str]) -> None:
    """Restore exact verified transaction paths in the index without touching files."""
    if not paths:
        return
    if _head_commit(vault) is None:
        result = run_git(
            vault,
            "rm",
            "--cached",
            "--quiet",
            "--force",
            "--ignore-unmatch",
            "--",
            *sorted(paths),
        )
    else:
        result = run_git(vault, "reset", "--quiet", "HEAD", "--", *sorted(paths))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RecoveryError(f"unable to unstage transaction paths: {detail}")


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
    atomic_replace_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        mode=0o600,
    )


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
    operation: str = "unspecified",
    request_sha256: str | None = None,
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
    request_sha256 = request_sha256 or transaction_sha256
    if not SHA256_PATTERN.fullmatch(transaction_sha256) or not SHA256_PATTERN.fullmatch(
        request_sha256
    ):
        raise RecoveryError("transaction digests must be lowercase SHA-256 values")
    if not OPERATION_PATTERN.fullmatch(operation):
        raise RecoveryError("invalid transaction operation")
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
            entries.append(
                {
                    "path": relative,
                    "original_sha256": original,
                    "original_mode": original_mode,
                    "final_sha256": requested_final,
                    "final_mode": requested_final_mode,
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
            "request_sha256": request_sha256,
            "operation": operation,
            "vault_id": _vault_id(vault),
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
        raise RecoveryError(
            f"invalid recovery journal for transaction {active_dir.name}"
        ) from exc
    if not isinstance(journal, dict) or journal.get("vault_id") != _vault_id(vault):
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
    if journal.get("schema_version") != 3:
        raise RecoveryError("recovery journal has an unsupported schema version")
    if not isinstance(journal.get("transaction_sha256"), str) or not SHA256_PATTERN.fullmatch(
        journal["transaction_sha256"]
    ):
        raise RecoveryError("recovery journal has an invalid transaction digest")
    request_sha256 = journal.get(
        "request_sha256", journal.get("transaction_sha256")
    )
    if not isinstance(request_sha256, str) or not SHA256_PATTERN.fullmatch(
        request_sha256
    ):
        raise RecoveryError("recovery journal has an invalid request digest")
    operation = journal.get("operation", "unspecified")
    if not isinstance(operation, str) or not OPERATION_PATTERN.fullmatch(operation):
        raise RecoveryError("recovery journal has an invalid operation")
    journal["request_sha256"] = request_sha256
    journal["operation"] = operation
    phase = journal.get("phase")
    if phase not in {"prepared", "applied"}:
        raise RecoveryError("recovery journal has an invalid phase")
    return journal


def inspect_active_transaction(vault: Path, active_dir: Path) -> dict[str, Any]:
    """Validate one active journal and return its mutation identity and next step."""
    vault = vault.resolve()
    active_dir = active_dir.resolve()
    journal = _read_journal(vault, active_dir)
    entries = _validated_entries(vault, active_dir, journal)
    for entry, path, _backup in entries:
        current = (_sha256(path), worktree_file_mode(path))
        original = (entry.get("original_sha256"), entry.get("original_mode"))
        final = (entry.get("final_sha256"), entry.get("final_mode"))
        if journal["phase"] == "applied":
            if current != final:
                raise RecoveryError(
                    f"applied transaction path changed: {entry['path']}"
                )
        elif current not in {original, final}:
            raise RecoveryError(
                f"prepared transaction path has an unexpected state: {entry['path']}"
            )
    git_clean = _git_is_clean(vault)
    head_commit = _head_commit(vault)
    commit_observed = (
        journal["phase"] == "applied"
        and git_clean
        and head_commit != journal.get("base_commit")
    )
    return {
        "transaction_id": journal["transaction_id"],
        "transaction_sha256": journal["transaction_sha256"],
        "request_sha256": journal["request_sha256"],
        "operation": journal["operation"],
        "phase": journal["phase"],
        "commit_observed": commit_observed,
        "base_commit": journal.get("base_commit"),
        "head_commit": head_commit,
        "git_clean": git_clean,
        "completion_path": journal.get("completion_path"),
        "files": [
            {
                "path": entry["path"],
                "original_sha256": entry.get("original_sha256"),
                "original_mode": entry.get("original_mode"),
                "final_sha256": entry.get("final_sha256"),
                "final_mode": entry.get("final_mode"),
            }
            for entry in journal["entries"]
        ],
    }


def committed_path_matches_state(
    vault: Path,
    path: Path,
    expected_sha256: str | None,
    expected_mode: str | None,
) -> bool:
    """Return whether one safe path has the expected state in worktree and HEAD."""
    vault = vault.resolve()
    lexical = Path(os.path.abspath(path.expanduser()))
    if not lexical.is_relative_to(vault) or lexical == vault:
        raise RecoveryError("committed path escapes the vault")
    relative = lexical.relative_to(vault).as_posix()
    _lexical_child(vault, relative, "committed path")
    if (expected_sha256 is None) != (expected_mode is None):
        raise RecoveryError("committed path hash and mode disagree")
    if expected_mode not in GIT_FILE_MODES | {None}:
        raise RecoveryError("committed path has an invalid mode")
    if lexical.is_symlink():
        return False
    if (_sha256(lexical), worktree_file_mode(lexical)) != (
        expected_sha256,
        expected_mode,
    ):
        return False
    if expected_sha256 is None:
        return (
            _git_head_file_oid(vault, relative) is None
            and _git_head_file_mode(vault, relative) is None
        )
    return (
        _git_head_file_oid(vault, relative) == _git_worktree_blob_oid(vault, relative)
        and _git_head_file_mode(vault, relative) == expected_mode
    )


def committed_file_matches_worktree(vault: Path, path: Path) -> bool:
    """Return whether one safe regular worktree file is identical to HEAD."""
    if path.is_symlink() or not path.is_file():
        return False
    try:
        mode = worktree_file_mode(path)
    except RecoveryError:
        return False
    return committed_path_matches_state(vault, path, _sha256(path), mode)


def mark_active_applied(active_dir: Path, completion_path: str | None = None) -> None:
    """Durably record that vault writes finished and now require a Git commit."""
    active_dir = active_dir.resolve()
    journal_path = active_dir / "journal.json"
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError(
            f"invalid recovery journal for transaction {active_dir.name}"
        ) from exc
    if journal.get("schema_version") != 3:
        raise RecoveryError("recovery journal has an unsupported schema version")
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
                or worktree_file_mode(backup) != entry.get("original_mode")
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
    expected_dirty: set[str] = set()
    resolved_entries = _validated_entries(vault, active_dir, journal)
    for entry, path, _backup in resolved_entries:
        current = _sha256(path)
        current_state = (current, worktree_file_mode(path))
        known = current_state in {
            (entry.get("original_sha256"), entry.get("original_mode")),
            (entry.get("final_sha256"), entry.get("final_mode")),
        }
        if not known:
            raise RecoveryError(
                f"affected note has an unexpected state: {entry['path']}"
            )
        expected_dirty.add(entry["path"])

    transaction_id = journal.get("transaction_id")
    archive_prefix = f".vault-pr/transactions/{transaction_id}"
    archive = _lexical_child(vault, archive_prefix, "transaction archive path")
    _validate_archive_contents(archive, journal, resolved_entries)
    status = run_git(vault, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status.returncode != 0:
        raise RecoveryError("unable to inspect vault status before recovery")
    dirty_paths, staged_paths = _parse_git_status_paths(status.stdout)
    unexpected = sorted(path for path in dirty_paths if path not in expected_dirty)
    if unexpected:
        raise RecoveryError(
            "unrelated vault changes block recovery: " + ", ".join(unexpected)
        )
    if staged_paths:
        changed_paths = _changed_journal_paths(journal)
        if _head_commit(vault) != journal.get("base_commit"):
            raise RecoveryError(
                "staged transaction recovery requires the original base commit"
            )
        if staged_paths != changed_paths:
            extra = sorted(staged_paths - changed_paths)
            missing = sorted(changed_paths - staged_paths)
            details = []
            if extra:
                details.append("extra=" + ", ".join(extra))
            if missing:
                details.append("missing=" + ", ".join(missing))
            raise RecoveryError(
                "staged paths do not match the recoverable transaction: "
                + "; ".join(details)
            )
        entry_by_path = {entry["path"]: entry for entry in journal["entries"]}
        for relative in sorted(staged_paths):
            entry = entry_by_path[relative]
            if _git_index_file_oid(vault, relative) != _git_worktree_blob_oid(
                vault, relative
            ) or _git_index_file_mode(vault, relative) != entry.get("final_mode"):
                raise RecoveryError(
                    f"staged state does not match transaction journal: {relative}"
                )
        _unstage_paths(vault, staged_paths)
        _dirty_after_unstage, staged_after_unstage = _git_status_paths(vault)
        if staged_after_unstage:
            raise RecoveryError(
                "transaction paths remain staged after recovery preparation: "
                + ", ".join(sorted(staged_after_unstage))
            )
    for entry, path, backup in resolved_entries:
        path = _lexical_child(vault, entry["path"], "recovery journal path")
        if backup is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = active_dir / "restore-staging" / entry["path"]
            temporary.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, temporary)
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
        if worktree_file_mode(path) != entry.get("original_mode"):
            raise RecoveryError(f"recovery mode verification failed: {entry['path']}")
    shutil.rmtree(active_dir)
    return {"status": "recovered", "transaction_id": transaction_id}


def _verify_committed_transaction_unlocked(
    vault: Path,
    active_dir: Path,
    *,
    require_clean: bool,
) -> tuple[dict[str, Any], str, set[str]]:
    """Verify that HEAD and the worktree contain exactly one journal result."""
    journal = _read_journal(vault, active_dir)
    if journal.get("phase") != "applied":
        raise RecoveryError("transaction has not reached the applied phase")
    if require_clean and not _git_is_clean(vault):
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
        final_mode = entry.get("final_mode")
        if _sha256(path) != final or worktree_file_mode(path) != final_mode:
            raise RecoveryError(
                f"applied path does not match final state: {entry['path']}"
            )
        if _git_head_file_oid(vault, entry["path"]) != _git_worktree_blob_oid(
            vault, entry["path"]
        ) or _git_head_file_mode(vault, entry["path"]) != final_mode:
            raise RecoveryError(
                f"Git commit does not contain final path state: {entry['path']}"
            )
        if (
            entry.get("original_sha256") != final
            or entry.get("original_mode") != final_mode
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
    return journal, head, expected_committed_paths


def _finalize_active_transaction_unlocked(
    vault: Path, active_dir: Path
) -> dict[str, Any]:
    journal, head, _expected_paths = _verify_committed_transaction_unlocked(
        vault,
        active_dir,
        require_clean=True,
    )
    transaction_id = journal["transaction_id"]
    shutil.rmtree(active_dir)
    return {"status": "finalized", "transaction_id": transaction_id, "commit": head}


def _synchronize_committed_transaction_index(
    vault: Path,
    active_dir: Path,
) -> dict[str, Any]:
    """Finish index synchronization after a crash immediately following ref CAS."""
    journal, head, expected_paths = _verify_committed_transaction_unlocked(
        vault,
        active_dir,
        require_clean=False,
    )
    dirty_paths, staged_paths = _git_status_paths(vault)
    if dirty_paths != expected_paths or staged_paths != expected_paths:
        raise RecoveryError(
            "committed transaction index is not in the recoverable pre-sync state"
        )

    base_commit = journal.get("base_commit")
    for relative in sorted(expected_paths):
        if base_commit is None:
            base_oid = None
            base_mode = None
        else:
            base_oid = _git_revision_file_oid(vault, base_commit, relative)
            base_mode = _git_revision_file_mode(vault, base_commit, relative)
        if (
            _git_index_file_oid(vault, relative) != base_oid
            or _git_index_file_mode(vault, relative) != base_mode
        ):
            raise RecoveryError(
                f"committed transaction index does not match its base: {relative}"
            )

    synchronized = run_git(
        vault,
        "reset",
        "--quiet",
        head,
        "--",
        *sorted(expected_paths),
    )
    if synchronized.returncode != 0 or not _git_is_clean(vault):
        raise RecoveryError(
            "unable to synchronize the committed transaction index during recovery"
        )
    return _finalize_active_transaction_unlocked(vault, active_dir)


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


def _temporary_index_paths(
    vault: Path, index_path: Path, base_commit: str | None
) -> set[str]:
    if base_commit is None:
        result = _run_git_with_index(vault, index_path, "ls-files", "-z")
    else:
        result = _run_git_with_index(
            vault,
            index_path,
            "diff",
            "--cached",
            "--name-only",
            "--no-renames",
            "-z",
            base_commit,
        )
    if result.returncode != 0:
        raise RecoveryError("unable to inspect temporary transaction index")
    return {path for path in result.stdout.split("\0") if path}


def _rollback_committed_ref(
    vault: Path, base_commit: str | None, commit_oid: str
) -> bool:
    if base_commit is None:
        rolled_back = run_git(vault, "update-ref", "-d", "HEAD", commit_oid)
    else:
        rolled_back = run_git(
            vault,
            "update-ref",
            "HEAD",
            base_commit,
            commit_oid,
        )
    return rolled_back.returncode == 0


def _run_post_commit_hook(vault: Path) -> str:
    hook_path_result = run_git(vault, "rev-parse", "--git-path", "hooks/post-commit")
    if hook_path_result.returncode != 0:
        return "lookup_failed"
    hook_path = Path(hook_path_result.stdout.strip())
    if not hook_path.is_absolute():
        hook_path = vault / hook_path
    if not hook_path.is_file() or not os.access(hook_path, os.X_OK):
        return "not_configured"
    hook = run_git(vault, "hook", "run", "post-commit")
    return "passed" if hook.returncode == 0 else "failed"


def commit_applied_transaction(
    vault: Path,
    git_dir: Path,
    transaction_id: str,
    message: str,
) -> dict[str, Any]:
    """Commit an exact temporary-index tree with an expected-old ref CAS."""
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
        base_commit = journal.get("base_commit")
        if base_commit is not None and not isinstance(base_commit, str):
            raise RecoveryError("recovery journal contains an invalid base commit")
        if _head_commit(vault) != base_commit:
            raise RecoveryError("vault HEAD changed since transaction prepare")
        entries = _validated_entries(vault, active_dir, journal)
        expected_paths: set[str] = set()
        final_by_path: dict[str, tuple[str | None, str | None]] = {}
        for entry, path, _backup in entries:
            final = entry.get("final_sha256")
            final_mode = entry.get("final_mode")
            if _sha256(path) != final or worktree_file_mode(path) != final_mode:
                raise RecoveryError(
                    f"applied path does not match final state: {entry['path']}"
                )
            if (
                entry.get("original_sha256") != final
                or entry.get("original_mode") != final_mode
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

        index_path = active_dir / "commit-index"
        index_lock_path = active_dir / "commit-index.lock"
        for temporary_path in (index_path, index_lock_path):
            if temporary_path.is_symlink() or (
                temporary_path.exists() and not temporary_path.is_file()
            ):
                raise RecoveryError("temporary transaction index is unsafe")
            temporary_path.unlink(missing_ok=True)
        try:
            initialized = _run_git_with_index(
                vault,
                index_path,
                "read-tree",
                base_commit if base_commit is not None else "--empty",
            )
            if initialized.returncode != 0:
                raise RecoveryError("unable to initialize temporary transaction index")
            added = _run_git_with_index(
                vault,
                index_path,
                "add",
                "--all",
                "--",
                *sorted(expected_paths),
            )
            if added.returncode != 0:
                detail = (added.stderr or added.stdout).strip()
                raise RecoveryError(f"unable to stage transaction paths: {detail}")
            staged_after = _temporary_index_paths(vault, index_path, base_commit)
            if staged_after != expected_paths:
                extra = sorted(staged_after - expected_paths)
                missing = sorted(expected_paths - staged_after)
                details = []
                if extra:
                    details.append("extra=" + ", ".join(extra))
                if missing:
                    details.append("missing=" + ", ".join(missing))
                raise RecoveryError(
                    "temporary index paths do not match transaction journal: "
                    + "; ".join(details)
                )
            for relative, (final, final_mode) in final_by_path.items():
                if _git_index_file_oid(
                    vault,
                    relative,
                    index_path=index_path,
                ) != _git_worktree_blob_oid(
                    vault, relative
                ) or _git_index_file_mode(
                    vault,
                    relative,
                    index_path=index_path,
                ) != final_mode:
                    raise RecoveryError(
                        f"temporary index state does not match journal: {relative}"
                    )
            tree = _run_git_with_index(vault, index_path, "write-tree")
            tree_oid = tree.stdout.strip()
            if tree.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", tree_oid):
                raise RecoveryError("unable to write transaction tree")
            commit_args = ["commit-tree", tree_oid]
            if base_commit is not None:
                commit_args.extend(["-p", base_commit])
            commit_args.extend(["-m", message])
            committed = run_git(vault, *commit_args)
            commit_oid = committed.stdout.strip()
            if committed.returncode != 0 or not re.fullmatch(
                r"[0-9a-f]{40,64}", commit_oid
            ):
                detail = (committed.stderr or committed.stdout).strip()
                raise RecoveryError(f"unable to create transaction commit: {detail}")
            expected_old = base_commit or ("0" * len(commit_oid))
            updated = run_git(
                vault,
                "update-ref",
                "HEAD",
                commit_oid,
                expected_old,
            )
            if updated.returncode != 0:
                raise RecoveryError(
                    "vault HEAD changed before transaction commit CAS"
                )
            synchronized = run_git(
                vault,
                "reset",
                "--quiet",
                "HEAD",
                "--",
                *sorted(expected_paths),
            )
            if synchronized.returncode != 0 or not _git_is_clean(vault):
                rolled_back = _rollback_committed_ref(vault, base_commit, commit_oid)
                restored = False
                if rolled_back:
                    if base_commit is None:
                        restored_index = run_git(
                            vault,
                            "rm",
                            "--cached",
                            "--quiet",
                            "--ignore-unmatch",
                            "--",
                            *sorted(expected_paths),
                        )
                    else:
                        restored_index = run_git(
                            vault,
                            "reset",
                            "--quiet",
                            "HEAD",
                            "--",
                            *sorted(expected_paths),
                        )
                    restored = restored_index.returncode == 0
                details = []
                if not rolled_back:
                    details.append("ref rollback also failed")
                elif not restored:
                    details.append("index restoration also failed")
                suffix = "; " + "; ".join(details) if details else ""
                raise RecoveryError(
                    "transaction commit succeeded but index synchronization failed"
                    + suffix
                )
        finally:
            index_path.unlink(missing_ok=True)
            index_lock_path.unlink(missing_ok=True)
        finalized = _finalize_active_transaction_unlocked(vault, active_dir)
        hook_status = _run_post_commit_hook(vault)
        return {
            **finalized,
            "status": "committed_and_finalized",
            "message": message,
            "post_commit_hook": hook_status,
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
        if journal.get("phase") == "applied":
            if _head_commit(vault) != journal.get("base_commit"):
                if not _git_is_clean(vault):
                    return _synchronize_committed_transaction_index(
                        vault,
                        active_dir,
                    )
                return _finalize_active_transaction_unlocked(vault, active_dir)
        return rollback_active_transaction(vault, active_dir)
