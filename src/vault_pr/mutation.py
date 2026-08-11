"""Thin orchestration for journaled vault mutations."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from .git import require_clean_git_root, vault_write_lock
from .recovery import (
    RecoveryError,
    committed_file_matches_worktree,
    inspect_active_transaction,
    list_active_transactions,
    mark_active_applied,
    prepare_active_transaction,
    rollback_active_transaction,
)


class MutationError(RuntimeError):
    """Raised when a journaled mutation fails or requires durable recovery."""


MutationCallback = Callable[[Path], None]
_OPERATION_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_TRANSACTION_ID_PATTERN = re.compile(r"txn_[A-Za-z0-9_-]{1,80}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_RECEIPT_SCHEMA_VERSION = 1


def _validate_request_fields(
    *,
    operation: str,
    transaction_id: str,
    transaction_sha256: str | None,
    request_sha256: str,
) -> None:
    if not _TRANSACTION_ID_PATTERN.fullmatch(transaction_id):
        raise MutationError("invalid mutation transaction id")
    if not _OPERATION_PATTERN.fullmatch(operation):
        raise MutationError("invalid mutation operation")
    if (
        transaction_sha256 is not None
        and not _SHA256_PATTERN.fullmatch(transaction_sha256)
    ) or not _SHA256_PATTERN.fullmatch(request_sha256):
        raise MutationError("mutation digests must be lowercase SHA-256 values")


def _receipt_path(vault: Path, transaction_id: str) -> Path:
    return vault / ".vault-pr" / "transaction-receipts" / f"{transaction_id}.json"


def _receipt_bytes(
    *,
    operation: str,
    transaction_id: str,
    transaction_sha256: str,
    request_sha256: str,
    receipt_data: dict[str, Any],
    completion_path: str | None,
    vault: Path,
    final_sha256: dict[Path, str | None],
    final_mode: dict[Path, str | None],
) -> bytes:
    receipt = {
        "schema_version": _RECEIPT_SCHEMA_VERSION,
        "record_type": "vault_pr_transaction_receipt",
        "operation": operation,
        "transaction_id": transaction_id,
        "transaction_sha256": transaction_sha256,
        "request_sha256": request_sha256,
        "completion_path": completion_path,
        "result": receipt_data,
        "files": [
            {
                "path": path.relative_to(vault).as_posix(),
                "sha256": final_sha256[path],
                "mode": final_mode[path],
            }
            for path in sorted(final_sha256)
        ],
    }
    return json.dumps(
        receipt,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8") + b"\n"


def _read_receipt(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
        raise MutationError("transaction receipt is missing, unsafe, or too large")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MutationError("transaction receipt is invalid") from exc
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != _RECEIPT_SCHEMA_VERSION
        or receipt.get("record_type") != "vault_pr_transaction_receipt"
        or not isinstance(receipt.get("result"), dict)
        or not isinstance(receipt.get("files"), list)
    ):
        raise MutationError("transaction receipt schema is invalid")
    return receipt


def _validate_committed_receipt(
    vault: Path,
    receipt_path: Path,
    receipt: dict[str, Any],
) -> None:
    try:
        if not committed_file_matches_worktree(vault, receipt_path):
            raise MutationError(
                "transaction receipt is not committed exactly in HEAD"
            )
        seen: set[str] = set()
        for item in receipt["files"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"path", "sha256", "mode"}
                or not isinstance(item.get("path"), str)
                or item["path"] in seen
            ):
                raise MutationError("transaction receipt file states are invalid")
            seen.add(item["path"])
            relative = Path(item["path"])
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or relative == Path(".")
            ):
                raise MutationError("transaction receipt path is unsafe")
            sha256 = item.get("sha256")
            mode = item.get("mode")
            if (sha256 is None) != (mode is None) or (
                sha256 is not None and not _SHA256_PATTERN.fullmatch(sha256)
            ) or mode not in {None, "100644", "100755"}:
                raise MutationError("transaction receipt file states are invalid")
    except RecoveryError as exc:
        raise MutationError(str(exc)) from exc


def _validate_identity(
    value: dict[str, Any],
    *,
    operation: str,
    transaction_id: str,
    transaction_sha256: str | None,
    request_sha256: str,
) -> None:
    if value.get("transaction_id") != transaction_id:
        raise MutationError("active transaction id does not match the request")
    if value.get("operation") != operation:
        raise MutationError("transaction operation conflict")
    if value.get("request_sha256") != request_sha256 or (
        transaction_sha256 is not None
        and value.get("transaction_sha256") != transaction_sha256
    ):
        raise MutationError("transaction digest conflict")


def _active_outcome(vault: Path, active_dir: Path) -> dict[str, Any]:
    try:
        identity = inspect_active_transaction(vault, active_dir)
    except RecoveryError as exc:
        raise MutationError(str(exc)) from exc
    phase = identity["phase"]
    commit_observed = identity["commit_observed"]
    outcome: dict[str, Any] = {
        **identity,
        "status": "already_active",
        "requires_commit": phase == "applied" and not commit_observed,
        "next_action": (
            "commit" if phase == "applied" and not commit_observed else "recover"
        ),
    }
    if phase == "applied":
        receipt = _read_receipt(_receipt_path(vault, identity["transaction_id"]))
        outcome["receipt_data"] = receipt["result"]
    return outcome


def _inspect_journaled_mutation_unlocked(
    *,
    vault: Path,
    git_dir: Path,
    operation: str,
    transaction_id: str,
    transaction_sha256: str | None,
    request_sha256: str,
) -> dict[str, Any] | None:
    try:
        active = list_active_transactions(git_dir)
    except RecoveryError as exc:
        raise MutationError(str(exc)) from exc
    if active:
        if len(active) != 1:
            raise MutationError(
                f"expected at most one active transaction, found {len(active)}"
            )
        outcome = _active_outcome(vault, active[0])
        _validate_identity(
            outcome,
            operation=operation,
            transaction_id=transaction_id,
            transaction_sha256=transaction_sha256,
            request_sha256=request_sha256,
        )
        return outcome

    receipt_path = _receipt_path(vault, transaction_id)
    if not receipt_path.exists() and not receipt_path.is_symlink():
        return None
    receipt = _read_receipt(receipt_path)
    _validate_identity(
        receipt,
        operation=operation,
        transaction_id=transaction_id,
        transaction_sha256=transaction_sha256,
        request_sha256=request_sha256,
    )
    _validate_committed_receipt(vault, receipt_path, receipt)
    return {
        "status": "already_committed",
        "transaction_id": transaction_id,
        "transaction_sha256": transaction_sha256,
        "request_sha256": request_sha256,
        "operation": operation,
        "requires_commit": False,
        "next_action": None,
        "receipt_data": receipt["result"],
    }


def inspect_journaled_mutation(
    *,
    vault: Path,
    git_dir: Path,
    operation: str,
    transaction_id: str,
    transaction_sha256: str | None,
    request_sha256: str | None = None,
    lock: bool = True,
) -> dict[str, Any] | None:
    """Recognize a mutation; callers disabling locking must hold the vault lock."""
    request_sha256 = request_sha256 or transaction_sha256
    if request_sha256 is None:
        raise MutationError("request digest is required")
    _validate_request_fields(
        operation=operation,
        transaction_id=transaction_id,
        transaction_sha256=transaction_sha256,
        request_sha256=request_sha256,
    )
    if lock:
        with vault_write_lock(vault):
            return _inspect_journaled_mutation_unlocked(
                vault=vault,
                git_dir=git_dir,
                operation=operation,
                transaction_id=transaction_id,
                transaction_sha256=transaction_sha256,
                request_sha256=request_sha256,
            )
    return _inspect_journaled_mutation_unlocked(
        vault=vault,
        git_dir=git_dir,
        operation=operation,
        transaction_id=transaction_id,
        transaction_sha256=transaction_sha256,
        request_sha256=request_sha256,
    )


def inspect_active_operation(
    *,
    vault: Path,
    git_dir: Path,
    operation: str,
) -> dict[str, Any] | None:
    """Return the active mutation for one operation when its request is no longer derivable."""
    with vault_write_lock(vault):
        try:
            active = list_active_transactions(git_dir)
        except RecoveryError as exc:
            raise MutationError(str(exc)) from exc
        if not active:
            return None
        if len(active) != 1:
            raise MutationError(
                f"expected at most one active transaction, found {len(active)}"
            )
        outcome = _active_outcome(vault, active[0])
        if outcome["operation"] != operation:
            raise MutationError(
                "an interrupted vault-pr transaction requires recovery: "
                + outcome["transaction_id"]
            )
        return outcome


def latest_committed_operation(
    *,
    vault: Path,
    git_dir: Path,
    operation: str,
) -> dict[str, Any] | None:
    """Return a committed receipt for an operation, if one exists exactly in HEAD."""
    if not _OPERATION_PATTERN.fullmatch(operation):
        raise MutationError("invalid mutation operation")
    with vault_write_lock(vault):
        try:
            active = list_active_transactions(git_dir)
        except RecoveryError as exc:
            raise MutationError(str(exc)) from exc
        if active:
            raise MutationError(
                "an interrupted vault-pr transaction requires recovery: "
                + ", ".join(path.name for path in active)
            )
        root = vault / ".vault-pr" / "transaction-receipts"
        if not root.exists():
            return None
        if root.is_symlink() or not root.is_dir():
            raise MutationError("transaction receipt directory is unsafe")
        matches: list[dict[str, Any]] = []
        for path in sorted(root.glob("txn_*.json")):
            receipt = _read_receipt(path)
            if receipt.get("operation") != operation:
                continue
            _validate_committed_receipt(vault, path, receipt)
            matches.append(receipt)
    if not matches:
        return None
    receipt = matches[-1]
    return {
        "status": "already_committed",
        "transaction_id": receipt["transaction_id"],
        "transaction_sha256": receipt["transaction_sha256"],
        "request_sha256": receipt["request_sha256"],
        "operation": operation,
        "requires_commit": False,
        "next_action": None,
        "receipt_data": receipt["result"],
    }


def validate_committed_mutation_receipt(
    *,
    vault: Path,
    operation: str,
    transaction_id: str,
    transaction_sha256: str,
    request_sha256: str | None = None,
) -> dict[str, Any]:
    """Load one exact committed mutation receipt without acquiring the vault lock."""
    request_sha256 = request_sha256 or transaction_sha256
    _validate_request_fields(
        operation=operation,
        transaction_id=transaction_id,
        transaction_sha256=transaction_sha256,
        request_sha256=request_sha256,
    )
    path = _receipt_path(vault, transaction_id)
    receipt = _read_receipt(path)
    _validate_identity(
        receipt,
        operation=operation,
        transaction_id=transaction_id,
        transaction_sha256=transaction_sha256,
        request_sha256=request_sha256,
    )
    _validate_committed_receipt(vault, path, receipt)
    return receipt


def mutation_response(
    outcome: dict[str, Any],
    *,
    applied_status: str,
) -> dict[str, Any]:
    """Merge durable receipt data with the shared mutation lifecycle status."""
    status = applied_status if outcome["status"] == "applied" else outcome["status"]
    return {
        **outcome.get("receipt_data", {}),
        "status": status,
        "transaction_id": outcome["transaction_id"],
        "requires_commit": outcome["requires_commit"],
        "next_action": outcome["next_action"],
    }


def execute_journaled_mutation(
    *,
    vault: Path,
    git_dir: Path,
    transaction_id: str,
    transaction_sha256: str,
    operation: str,
    receipt_data: dict[str, Any],
    request_sha256: str | None = None,
    affected: set[Path],
    expected_original_sha256: dict[Path, str | None],
    expected_original_mode: dict[Path, str | None],
    final_sha256: dict[Path, str | None],
    final_mode: dict[Path, str | None],
    write: MutationCallback,
    postcondition: MutationCallback | None = None,
    completion_path: str | None = None,
    precondition: Callable[[], None] | None = None,
    lock: bool = True,
) -> dict[str, Any]:
    """Lock, journal, write, verify, and rollback one domain mutation."""
    request_sha256 = request_sha256 or transaction_sha256
    _validate_request_fields(
        operation=operation,
        transaction_id=transaction_id,
        transaction_sha256=transaction_sha256,
        request_sha256=request_sha256,
    )
    receipt_path = _receipt_path(vault, transaction_id)
    receipt_content = _receipt_bytes(
        operation=operation,
        transaction_id=transaction_id,
        transaction_sha256=transaction_sha256,
        request_sha256=request_sha256,
        receipt_data=receipt_data,
        completion_path=completion_path,
        vault=vault,
        final_sha256=final_sha256,
        final_mode=final_mode,
    )
    receipt_sha256 = hashlib.sha256(receipt_content).hexdigest()
    lock_context = vault_write_lock(vault) if lock else nullcontext()
    with lock_context:
        existing = _inspect_journaled_mutation_unlocked(
            vault=vault,
            git_dir=git_dir,
            operation=operation,
            transaction_id=transaction_id,
            transaction_sha256=transaction_sha256,
            request_sha256=request_sha256,
        )
        if existing is not None:
            return existing
        require_clean_git_root(vault)
        if precondition is not None:
            precondition()
        affected = {*affected, receipt_path}
        expected_original_sha256 = {
            **expected_original_sha256,
            receipt_path: None,
        }
        expected_original_mode = {
            **expected_original_mode,
            receipt_path: None,
        }
        final_sha256 = {**final_sha256, receipt_path: receipt_sha256}
        final_mode = {**final_mode, receipt_path: "100644"}
        try:
            active_dir = prepare_active_transaction(
                vault=vault,
                git_dir=git_dir,
                transaction_id=transaction_id,
                transaction_sha256=transaction_sha256,
                operation=operation,
                request_sha256=request_sha256,
                affected=affected,
                expected_original_sha256=expected_original_sha256,
                expected_original_mode=expected_original_mode,
                final_sha256=final_sha256,
                final_mode=final_mode,
            )
        except RecoveryError as exc:
            raise MutationError(str(exc)) from exc
        try:
            write(active_dir)
            if postcondition is not None:
                postcondition(active_dir)
            receipt_staging = active_dir / "write-staging" / "transaction-receipt"
            receipt_staging.parent.mkdir(parents=True, exist_ok=True)
            receipt_staging.write_bytes(receipt_content)
            receipt_staging.chmod(0o644)
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(receipt_staging, receipt_path)
            mark_active_applied(active_dir, str(receipt_path.relative_to(vault)))
        except Exception as exc:
            try:
                rollback_active_transaction(vault, active_dir)
            except RecoveryError as recovery_exc:
                raise MutationError(
                    "mutation failed and durable recovery is required from "
                    f"transaction {active_dir.name}: {recovery_exc}"
                ) from recovery_exc
            raise MutationError(f"mutation failed: {exc}") from exc
        return {
            "status": "applied",
            "transaction_id": transaction_id,
            "transaction_sha256": transaction_sha256,
            "request_sha256": request_sha256,
            "operation": operation,
            "requires_commit": True,
            "next_action": "commit",
            "receipt_data": receipt_data,
        }
