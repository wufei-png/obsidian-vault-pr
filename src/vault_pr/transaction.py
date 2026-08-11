"""Read-only transaction lifecycle observability."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import load_vault_path
from .git import run_git, vault_write_lock
from .mutation import MutationError, list_committed_mutation_receipts
from .recovery import (
    RecoveryError,
    TRANSACTION_ID_PATTERN,
    inspect_active_transaction,
    list_active_transactions,
    verify_committed_transaction,
)


class TransactionError(RuntimeError):
    """Raised when transaction state cannot be observed safely."""


def _git_dir(vault: Path) -> Path:
    result = run_git(vault, "rev-parse", "--absolute-git-dir")
    if result.returncode != 0:
        raise TransactionError("unable to resolve vault Git directory")
    return Path(result.stdout.strip()).resolve()


def _active_view(vault: Path, active_dir: Path) -> dict[str, Any]:
    snapshot = inspect_active_transaction(vault, active_dir)
    phase = snapshot["phase"]
    if snapshot["unrelated_changes"]:
        state = "RECOVERY_REQUIRED"
        allowed_actions = []
        blocking_reasons = ["unrelated_vault_changes"]
    elif phase == "prepared":
        state = "RECOVERY_REQUIRED"
        allowed_actions = ["recover"]
        blocking_reasons = ["transaction_not_applied"]
    elif not snapshot["head_identity_matches"]:
        state = "RECOVERY_REQUIRED"
        allowed_actions = ["recover"]
        blocking_reasons = ["head_identity_changed"]
    elif snapshot["head_commit"] != snapshot["base_commit"] and not snapshot[
        "git_clean"
    ]:
        state = "RECOVERY_REQUIRED"
        allowed_actions = ["recover"]
        blocking_reasons = ["vault_not_clean_after_head_change"]
    elif snapshot["commit_observed"]:
        try:
            verify_committed_transaction(vault, active_dir)
        except RecoveryError:
            state = "RECOVERY_REQUIRED"
            allowed_actions = ["recover"]
            blocking_reasons = ["commit_does_not_match_transaction"]
        else:
            state = "COMMITTED"
            allowed_actions = ["finalize", "recover"]
            blocking_reasons = []
    else:
        state = "APPLIED"
        allowed_actions = ["commit", "recover"]
        blocking_reasons = []
    return {
        "transaction_id": snapshot["transaction_id"],
        "operation": snapshot["operation"],
        "state": state,
        "allowed_actions": allowed_actions,
        "blocking_reasons": blocking_reasons,
        "transaction_sha256": snapshot["transaction_sha256"],
        "request_sha256": snapshot["request_sha256"],
        "phase": phase,
        "commit_observed": snapshot["commit_observed"],
        "base_commit": snapshot["base_commit"],
        "head_commit": snapshot["head_commit"],
        "head_identity_matches": snapshot["head_identity_matches"],
        "git_clean": snapshot["git_clean"],
        "unrelated_changes": snapshot["unrelated_changes"],
        "completion_path": snapshot["completion_path"],
        "files": [
            {
                "path": item["path"],
                "original": {
                    "sha256": item["original_sha256"],
                    "mode": item["original_mode"],
                },
                "final": {
                    "sha256": item["final_sha256"],
                    "mode": item["final_mode"],
                },
                "original_state_available": True,
            }
            for item in snapshot["files"]
        ],
        "basis": "active_journal",
    }


def _finalized_view(record: dict[str, Any]) -> dict[str, Any]:
    receipt = record["receipt"]
    files = [
        {
            "path": item["path"],
            "original": None,
            "final": {"sha256": item["sha256"], "mode": item["mode"]},
            "original_state_available": False,
        }
        for item in receipt["files"]
    ]
    files.append(
        {
            "path": record["receipt_path"],
            "original": {"sha256": None, "mode": None},
            "final": {
                "sha256": record["receipt_sha256"],
                "mode": "100644",
            },
            "original_state_available": True,
        }
    )
    return {
        "transaction_id": receipt["transaction_id"],
        "operation": receipt["operation"],
        "state": "FINALIZED",
        "allowed_actions": [],
        "blocking_reasons": [],
        "transaction_sha256": receipt["transaction_sha256"],
        "request_sha256": receipt["request_sha256"],
        "completion_path": receipt.get("completion_path"),
        "files": files,
        "result": receipt["result"],
        "basis": "committed_receipt",
    }


def _transaction_views(config: dict[str, Any]) -> list[dict[str, Any]]:
    vault = load_vault_path(config)
    git_dir = _git_dir(vault)
    try:
        with vault_write_lock(vault):
            active = [
                _active_view(vault, path)
                for path in list_active_transactions(git_dir)
            ]
            active_ids = {item["transaction_id"] for item in active}
            finalized = [
                _finalized_view(record)
                for record in list_committed_mutation_receipts(
                    vault,
                    lock=False,
                    exclude_transaction_ids=active_ids,
                )
            ]
    except (MutationError, RecoveryError) as exc:
        raise TransactionError(str(exc)) from exc
    return sorted([*active, *finalized], key=lambda item: item["transaction_id"])


def list_transactions(config: dict[str, Any]) -> dict[str, Any]:
    """List compact lifecycle states for active and finalized transactions."""
    compact = [
        {
            key: item[key]
            for key in (
                "transaction_id",
                "operation",
                "state",
                "allowed_actions",
                "blocking_reasons",
            )
        }
        for item in _transaction_views(config)
    ]
    return {"status": "listed", "transactions": compact}


def show_transaction(config: dict[str, Any], transaction_id: str) -> dict[str, Any]:
    """Show one validated active journal or committed receipt."""
    if not TRANSACTION_ID_PATTERN.fullmatch(transaction_id):
        raise TransactionError("invalid transaction id")
    matches = [
        item
        for item in _transaction_views(config)
        if item["transaction_id"] == transaction_id
    ]
    if len(matches) != 1:
        raise TransactionError(f"transaction not found: {transaction_id}")
    return {"status": "shown", **matches[0]}


def diff_transaction(config: dict[str, Any], transaction_id: str) -> dict[str, Any]:
    """Show hash-and-mode transitions without emitting private note content."""
    shown = show_transaction(config, transaction_id)
    return {
        "status": "shown",
        "transaction_id": transaction_id,
        "state": shown["state"],
        "basis": shown["basis"],
        "files": shown["files"],
    }
