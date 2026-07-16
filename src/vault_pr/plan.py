"""Auditable semantic change plans with reviewer and note-count gates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .files import atomic_replace_bytes
import yaml

from .capture import CaptureError, load_validated_capture, source_note_has_id
from .config import (
    load_vault_path,
    max_touched_notes,
    note_path_allowed,
    review_policy,
    source_archive_dir,
    vault_relative_path,
)
from .git import require_clean_git_root, run_git, vault_write_lock
from .recovery import (
    RecoveryError,
    mark_active_applied,
    prepare_active_transaction,
    rollback_active_transaction,
    worktree_file_mode,
)


class PlanError(RuntimeError):
    """Raised when a semantic change plan is invalid or unsafe."""


ALLOWED_ACTIONS = {"create", "update", "delete", "rename"}
ALLOWED_INTENTS = {"integrate", "merge", "split", "rename"}
SOURCE_ID_PATTERN = re.compile(r"src_[0-9a-f]{20}")
TRANSACTION_ID_PATTERN = re.compile(r"txn_[A-Za-z0-9_-]{1,80}")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PlanError(f"{label} not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"invalid {label}: {path}") from exc
    if not isinstance(value, dict):
        raise PlanError(f"{label} must be a JSON object")
    return value


def _git_dir(vault: Path) -> Path:
    result = run_git(vault, "rev-parse", "--absolute-git-dir")
    if result.returncode != 0:
        raise PlanError("unable to resolve vault Git directory")
    return Path(result.stdout.strip())


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _plan_hash(plan_path: Path) -> str:
    return _sha256_file(plan_path)


def _proposed_content_paths(
    plan_path: Path, plan: dict[str, Any] | None = None
) -> list[Path]:
    plan = plan if plan is not None else _read_json(plan_path, "plan")
    operations = plan.get("operations")
    if not isinstance(operations, list):
        raise PlanError("plan operations must be an array")
    content_paths: set[Path] = set()
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise PlanError(f"operation {index} must be an object")
        if operation.get("content_file") is not None:
            content_paths.add(_content_file(plan_path, operation))
    return sorted(content_paths)


def _transaction_digest_snapshot(
    plan_path: Path, plan_bytes: bytes, content: dict[Path, bytes]
) -> str:
    """Bind review to the exact plan and every proposed content file."""
    digest = hashlib.sha256()
    digest.update(b"plan\0")
    digest.update(plan_bytes)
    for content_path in sorted(content):
        relative = str(content_path.relative_to(plan_path.parent.resolve()))
        digest.update(b"file\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content[content_path])
    return digest.hexdigest()


def _transaction_digest(plan_path: Path, content_paths: list[Path]) -> str:
    plan_bytes = plan_path.read_bytes()
    content = {path: path.read_bytes() for path in sorted(set(content_paths))}
    return _transaction_digest_snapshot(plan_path, plan_bytes, content)


def _approval_bytes_valid(
    content: bytes | None,
    transaction_sha256: str,
    history: list[dict[str, Any]],
) -> bool:
    if content is None:
        return False
    try:
        approval = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(approval, dict)
        and approval.get("decision") == "approve"
        and approval.get("transaction_sha256") == transaction_sha256
        and isinstance(approval.get("reviewer"), str)
        and approval["reviewer"].strip()
        and history
        and history[-1] == approval
    )


def _validated_source_manifest(
    config: dict[str, Any], vault: Path, source_id: str
) -> dict[str, Any]:
    capture = source_archive_dir(config, vault) / source_id
    try:
        return load_validated_capture(capture, source_id=source_id, vault=vault)
    except CaptureError as exc:
        raise PlanError(f"source capture integrity failed: {exc}") from exc


def init_plan(config: dict[str, Any], source_id: str, summary: str) -> dict[str, Any]:
    """Create a plan template outside the worktree in the vault Git directory."""
    vault = load_vault_path(config)
    require_clean_git_root(vault)
    if not SOURCE_ID_PATTERN.fullmatch(source_id):
        raise PlanError("invalid source id")
    _validated_source_manifest(config, vault, source_id)
    summary = " ".join(summary.split()).strip()
    if not summary:
        raise PlanError("plan summary must not be empty")
    transaction_id = (
        "txn_"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_"
        + uuid.uuid4().hex[:8]
    )
    plan_dir = _git_dir(vault) / "vault-pr-staging" / transaction_id
    plan_dir.mkdir(parents=True)
    (plan_dir / "files").mkdir()
    plan = {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "source_id": source_id,
        "summary": summary,
        "intent": "integrate",
        "contradiction": False,
        "operations": [],
    }
    plan_path = plan_dir / "plan.json"
    plan_path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"transaction_id": transaction_id, "plan": str(plan_path)}


def _frontmatter_ok(content: bytes) -> bool:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    lines = text.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        return False
    try:
        closing = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
        frontmatter = yaml.safe_load("\n".join(lines[1:closing]))
    except (StopIteration, yaml.YAMLError):
        return False
    return isinstance(frontmatter, dict)


def _content_file(plan_path: Path, operation: dict[str, Any]) -> Path:
    raw_content_file = operation.get("content_file")
    if not isinstance(raw_content_file, str) or not raw_content_file.strip():
        raise PlanError("create/update operation requires content_file")
    content_path = (plan_path.parent / raw_content_file).resolve()
    if not content_path.is_relative_to(plan_path.parent.resolve()):
        raise PlanError("content_file escapes the plan directory")
    if not content_path.is_file():
        raise PlanError(f"content_file does not exist: {raw_content_file}")
    return content_path


def _approval_status(
    plan_path: Path,
    content_paths: list[Path],
    history: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    approval_path = plan_path.parent / "approval.json"
    if not approval_path.is_file():
        return False, None
    valid = _approval_bytes_valid(
        approval_path.read_bytes(),
        _transaction_digest(plan_path, content_paths),
        history,
    )
    return valid, str(approval_path)


def validate_plan(config: dict[str, Any], plan_path: Path) -> dict[str, Any]:
    """Validate paths, hashes, note count, risk, and reviewer approval."""
    vault = load_vault_path(config)
    plan_path = plan_path.expanduser().resolve()
    plan = _read_json(plan_path, "plan")
    if plan.get("schema_version") != 1:
        raise PlanError("unsupported plan schema_version")
    transaction_id = plan.get("transaction_id")
    if not isinstance(transaction_id, str) or not TRANSACTION_ID_PATTERN.fullmatch(
        transaction_id
    ):
        raise PlanError("invalid transaction_id")
    source_id = plan.get("source_id")
    if not isinstance(source_id, str) or not SOURCE_ID_PATTERN.fullmatch(source_id):
        raise PlanError("plan source_id does not resolve to a capture")
    manifest = _validated_source_manifest(config, vault, source_id)
    artifacts = manifest.get("artifacts")
    source_note_raw = (
        artifacts.get("source_note") if isinstance(artifacts, dict) else None
    )
    source_note = vault_relative_path(
        vault, source_note_raw, "source manifest artifacts.source_note"
    )
    if not source_note.is_file():
        raise PlanError("source manifest source note does not exist")
    intent = plan.get("intent", "integrate")
    if intent not in ALLOWED_INTENTS:
        raise PlanError(f"unsupported plan intent: {intent}")
    contradiction = plan.get("contradiction", False)
    if not isinstance(contradiction, bool):
        raise PlanError("plan contradiction must be boolean")
    operations = plan.get("operations")
    if not isinstance(operations, list) or not operations:
        raise PlanError("plan operations must be a non-empty array")

    touched: set[Path] = set()
    normalized: list[dict[str, Any]] = []
    actions: set[str] = set()
    for index, raw_operation in enumerate(operations):
        if not isinstance(raw_operation, dict):
            raise PlanError(f"operation {index} must be an object")
        action = raw_operation.get("action")
        if action not in ALLOWED_ACTIONS:
            raise PlanError(f"operation {index} has unsupported action: {action}")
        raw_path = raw_operation.get("path")
        target = vault_relative_path(vault, raw_path, f"operations[{index}].path")
        if target.suffix.lower() != ".md" or not note_path_allowed(
            config, vault, target
        ):
            raise PlanError(
                f"operation {index} must target an allowed visible Markdown note"
            )
        actions.add(action)
        if target in touched:
            raise PlanError(f"operation {index} duplicates an affected note path")
        touched.add(target)
        operation = {"action": action, "path": target}

        source = target
        if action == "rename":
            source = vault_relative_path(
                vault, raw_operation.get("from"), f"operations[{index}].from"
            )
            if (
                source.suffix.lower() != ".md"
                or not source.is_file()
                or not note_path_allowed(config, vault, source)
            ):
                raise PlanError(f"operation {index} rename source is invalid")
            if source in touched:
                raise PlanError(f"operation {index} duplicates an affected note path")
            touched.add(source)
            operation["from"] = source
            if target.exists():
                raise PlanError(f"operation {index} rename target already exists")
        elif action == "create":
            if target.exists():
                raise PlanError(f"operation {index} create target already exists")
        elif not target.is_file():
            raise PlanError(f"operation {index} target does not exist")

        if action in {"update", "delete", "rename"}:
            expected = raw_operation.get("expected_sha256")
            if not isinstance(expected, str) or expected != _sha256_file(source):
                raise PlanError(f"operation {index} expected_sha256 does not match")
            operation["expected_sha256"] = expected

        if action in {"create", "update"} or (
            action == "rename" and raw_operation.get("content_file")
        ):
            content_path = _content_file(plan_path, raw_operation)
            content = content_path.read_bytes()
            if not _frontmatter_ok(content):
                raise PlanError(f"operation {index} content has invalid frontmatter")
            operation["content_file"] = content_path
        normalized.append(operation)

    source_updates = [
        operation
        for operation in normalized
        if operation["action"] == "update" and operation["path"] == source_note
    ]
    if not source_updates:
        raise PlanError("plan must update the captured source note")
    try:
        preserves_source_id = source_note_has_id(
            source_updates[0]["content_file"], source_id
        )
    except CaptureError as exc:
        raise PlanError(f"proposed source note is invalid: {exc}") from exc
    if not preserves_source_id:
        raise PlanError("proposed source note must preserve its source_id property")

    maximum = max_touched_notes(config)
    if len(touched) > maximum:
        raise PlanError(f"plan touches {len(touched)} notes; maximum is {maximum}")

    policy = review_policy(config)
    risks: list[str] = []
    if intent == "merge":
        risks.append("merge")
    if "delete" in actions:
        risks.append("delete")
    if contradiction:
        risks.append("contradiction")
    if policy.review_all_semantic_transactions and not risks:
        risks.append("vault_policy")
    content_paths = [
        operation["content_file"]
        for operation in normalized
        if operation.get("content_file")
    ]
    transaction_sha256 = _transaction_digest(plan_path, content_paths)
    history = _review_history(plan_path.parent / "review-history.json")
    _, rejection_count = _validate_review_history(history)
    if history and not risks:
        risks.append("review_history")
    revision_limit_exhausted = rejection_count > policy.max_revision_rounds
    approval_valid, approval_path = _approval_status(plan_path, content_paths, history)
    return {
        "status": "valid",
        "transaction_id": transaction_id,
        "source_id": source_id,
        "plan_sha256": _plan_hash(plan_path),
        "transaction_sha256": transaction_sha256,
        "touched_notes": len(touched),
        "maximum_touched_notes": maximum,
        "risks": risks,
        "approval_required": bool(risks),
        "approval_valid": approval_valid,
        "approval_path": approval_path,
        "review_rejections": rejection_count,
        "revision_limit_exhausted": revision_limit_exhausted,
        "operations": normalized,
        "plan": plan,
        "plan_path": plan_path,
    }


def _review_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"invalid review history: {path}") from exc
    if not isinstance(history, list) or not all(
        isinstance(entry, dict) for entry in history
    ):
        raise PlanError(f"review history must be an array: {path}")
    return history


def _validate_review_history(
    history: list[dict[str, Any]],
) -> tuple[set[str], int]:
    """Validate reviewer decisions and return rejected digests and their count."""
    rejected_digests: set[str] = set()
    rejection_count = 0
    for index, entry in enumerate(history):
        decision = entry.get("decision")
        digest = entry.get("transaction_sha256")
        policy = entry.get("review_policy")
        if (
            entry.get("schema_version") != 1
            or decision not in {"approve", "reject"}
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(entry.get("plan_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["plan_sha256"])
            or not isinstance(entry.get("reviewer"), str)
            or not entry["reviewer"].strip()
            or not isinstance(entry.get("reviewed_at"), str)
            or not entry["reviewed_at"].strip()
            or not isinstance(policy, dict)
            or not isinstance(policy.get("review_all_semantic_transactions"), bool)
            or isinstance(policy.get("max_revision_rounds"), bool)
            or not isinstance(policy.get("max_revision_rounds"), int)
            or not 0 <= policy["max_revision_rounds"] <= 5
        ):
            raise PlanError(f"review history entry {index} is invalid")
        if digest in rejected_digests:
            raise PlanError(
                "review history reuses plan content that was already rejected"
            )
        if decision == "reject":
            rejected_digests.add(digest)
            rejection_count += 1
    return rejected_digests, rejection_count


def record_review(
    config: dict[str, Any],
    plan_path: Path,
    reviewer: str,
    decision: str,
    notes: str = "",
) -> dict[str, Any]:
    """Bind a reviewer decision to the exact plan and proposed content bytes."""
    plan_path = plan_path.expanduser().resolve()
    validate_plan(config, plan_path)
    try:
        plan_bytes = plan_path.read_bytes()
        plan = json.loads(plan_bytes)
    except FileNotFoundError as exc:
        raise PlanError(f"plan not found: {plan_path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"invalid plan: {plan_path}") from exc
    if not isinstance(plan, dict):
        raise PlanError("plan must be a JSON object")
    reviewer = " ".join(reviewer.split()).strip()
    if not reviewer:
        raise PlanError("reviewer must not be empty")
    if decision not in {"approve", "reject"}:
        raise PlanError("review decision must be approve or reject")
    content = {
        path: path.read_bytes() for path in _proposed_content_paths(plan_path, plan)
    }
    transaction_sha256 = _transaction_digest_snapshot(plan_path, plan_bytes, content)
    policy = review_policy(config)
    history_path = plan_path.parent / "review-history.json"
    history = _review_history(history_path)
    rejected_digests, rejection_count = _validate_review_history(history)
    if rejection_count > policy.max_revision_rounds:
        raise PlanError(
            "review revision limit exhausted; preserve the unapplied plan for inspection"
        )
    if transaction_sha256 in rejected_digests:
        raise PlanError("plan and proposed content must change after rejection")
    approval = {
        "schema_version": 1,
        "plan_sha256": _sha256_bytes(plan_bytes),
        "transaction_sha256": transaction_sha256,
        "reviewer": reviewer,
        "decision": decision,
        "notes": notes.strip(),
        "review_policy": {
            "review_all_semantic_transactions": policy.review_all_semantic_transactions,
            "max_revision_rounds": policy.max_revision_rounds,
        },
        "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    history.append(approval)
    _atomic_write(
        history_path,
        (json.dumps(history, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    approval_path = plan_path.parent / "approval.json"
    _atomic_write(
        approval_path,
        (json.dumps(approval, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return {
        "approval": str(approval_path),
        "review_history": str(history_path),
        "remaining_revision_rounds": max(
            0, policy.max_revision_rounds - rejection_count
        ),
        **approval,
    }


def _atomic_write(path: Path, content: bytes) -> None:
    atomic_replace_bytes(path, content)


def _install_staged(
    path: Path, content: bytes, staging_path: Path, git_mode: str
) -> None:
    """Install bytes atomically from the private active transaction directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        staging_path.write_bytes(content)
        staging_path.chmod(0o755 if git_mode == "100755" else 0o644)
        os.replace(staging_path, path)
    finally:
        staging_path.unlink(missing_ok=True)


def apply_plan(config: dict[str, Any], plan_path: Path) -> dict[str, Any]:
    """Apply a validated plan atomically and archive its audit record."""
    vault = load_vault_path(config)
    require_clean_git_root(vault)
    with vault_write_lock(vault):
        require_clean_git_root(vault)
        validation = validate_plan(config, plan_path)
        frozen_plan_path = validation["plan_path"]
        plan_bytes = frozen_plan_path.read_bytes()
        content_snapshots = {
            operation["content_file"]: operation["content_file"].read_bytes()
            for operation in validation["operations"]
            if operation.get("content_file")
        }
        transaction_sha256 = _transaction_digest_snapshot(
            frozen_plan_path, plan_bytes, content_snapshots
        )
        if transaction_sha256 != validation["transaction_sha256"]:
            raise PlanError("plan or proposed content changed during validation")
        approval_path = frozen_plan_path.parent / "approval.json"
        approval_bytes = approval_path.read_bytes() if approval_path.is_file() else None
        review_history_path = frozen_plan_path.parent / "review-history.json"
        review_history_bytes = (
            review_history_path.read_bytes() if review_history_path.is_file() else None
        )
        if validation["revision_limit_exhausted"]:
            raise PlanError(
                "review revision limit exceeded under the configured vault policy"
            )
        if validation["approval_required"] and not _approval_bytes_valid(
            approval_bytes,
            transaction_sha256,
            _review_history(review_history_path),
        ):
            raise PlanError(
                "a hash-bound reviewer approval record is required for: "
                + ", ".join(validation["risks"])
            )

        operations = validation["operations"]
        validated_original_mode: dict[Path, str | None] = {}
        for operation in operations:
            action = operation["action"]
            target = operation["path"]
            try:
                if action == "create":
                    unchanged = not target.exists()
                    validated_original_mode[target] = None
                elif action == "rename":
                    source = operation["from"]
                    unchanged = (
                        not target.exists()
                        and source.is_file()
                        and _sha256_file(source) == operation["expected_sha256"]
                    )
                    validated_original_mode[source] = worktree_file_mode(source)
                    validated_original_mode[target] = None
                else:
                    unchanged = (
                        target.is_file()
                        and _sha256_file(target) == operation["expected_sha256"]
                    )
                    validated_original_mode[target] = worktree_file_mode(target)
            except (OSError, RecoveryError):
                unchanged = False
            if not unchanged:
                raise PlanError(
                    f"operation target changed during validation: {target.relative_to(vault)}"
                )
        require_clean_git_root(vault)

        affected: set[Path] = set()
        expected_original_sha256: dict[Path, str | None] = {}
        expected_original_mode: dict[Path, str | None] = {}
        for operation in operations:
            target = operation["path"]
            affected.add(target)
            if operation["action"] == "rename":
                source = operation["from"]
                affected.add(source)
                expected_original_sha256[source] = operation["expected_sha256"]
                expected_original_sha256[target] = None
                expected_original_mode[source] = validated_original_mode[source]
                expected_original_mode[target] = None
            elif operation["action"] == "create":
                expected_original_sha256[target] = None
                expected_original_mode[target] = None
            else:
                expected_original_sha256[target] = operation["expected_sha256"]
                expected_original_mode[target] = validated_original_mode[target]
        transaction_root = (vault / ".vault-pr" / "transactions").resolve()
        if not transaction_root.is_relative_to(vault):
            raise PlanError("transaction archive directory escapes the vault")
        archive = transaction_root / validation["transaction_id"]
        if archive.exists():
            raise PlanError(f"transaction archive already exists: {archive}")

        final_sha256 = {
            path: _sha256_file(path) if path.is_file() else None for path in affected
        }
        final_mode = dict(expected_original_mode)
        for operation in operations:
            action = operation["action"]
            target = operation["path"]
            if action in {"create", "update"}:
                final_sha256[target] = _sha256_bytes(
                    content_snapshots[operation["content_file"]]
                )
                if action == "create":
                    final_mode[target] = "100644"
            elif action == "delete":
                final_sha256[target] = None
                final_mode[target] = None
            elif action == "rename":
                source = operation["from"]
                final_sha256[source] = None
                final_mode[source] = None
                final_sha256[target] = (
                    _sha256_bytes(content_snapshots[operation["content_file"]])
                    if operation.get("content_file")
                    else _sha256_file(source)
                )
                final_mode[target] = validated_original_mode[source]

        result = {
            "schema_version": 1,
            "transaction_id": validation["transaction_id"],
            "source_id": validation["source_id"],
            "plan_sha256": _sha256_bytes(plan_bytes),
            "transaction_sha256": transaction_sha256,
            "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "touched_notes": validation["touched_notes"],
            "risks": validation["risks"],
        }
        result_bytes = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
        archive_payloads = {
            archive / "plan.json": plan_bytes,
            archive / "result.json": result_bytes,
        }
        reserved_audit_paths = {
            archive / "plan.json",
            archive / "result.json",
            archive / "approval.json",
            archive / "review-history.json",
        }
        for content_path, content in content_snapshots.items():
            relative = content_path.relative_to(frozen_plan_path.parent)
            archived_content = archive / relative
            if archived_content in reserved_audit_paths:
                raise PlanError(
                    f"proposed content uses reserved audit path: {relative}"
                )
            archive_payloads[archived_content] = content
        if approval_bytes is not None:
            archive_payloads[archive / "approval.json"] = approval_bytes
        if review_history_bytes is not None:
            archive_payloads[archive / "review-history.json"] = review_history_bytes
        affected.update(archive_payloads)
        expected_original_sha256.update({path: None for path in archive_payloads})
        expected_original_mode.update({path: None for path in archive_payloads})
        final_sha256.update(
            {path: _sha256_bytes(content) for path, content in archive_payloads.items()}
        )
        final_mode.update({path: "100644" for path in archive_payloads})

        try:
            active_dir = prepare_active_transaction(
                vault=vault,
                git_dir=_git_dir(vault),
                transaction_id=validation["transaction_id"],
                transaction_sha256=validation["transaction_sha256"],
                affected=affected,
                expected_original_sha256=expected_original_sha256,
                expected_original_mode=expected_original_mode,
                final_sha256=final_sha256,
                final_mode=final_mode,
            )
        except RecoveryError as exc:
            raise PlanError(str(exc)) from exc
        try:
            write_index = 0
            for operation in operations:
                action = operation["action"]
                target = operation["path"]
                if action in {"create", "update"}:
                    _install_staged(
                        target,
                        content_snapshots[operation["content_file"]],
                        active_dir / "write-staging" / str(write_index),
                        final_mode[target],
                    )
                    write_index += 1
                elif action == "delete":
                    target.unlink()
                elif action == "rename":
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if operation.get("content_file"):
                        _install_staged(
                            target,
                            content_snapshots[operation["content_file"]],
                            active_dir / "write-staging" / str(write_index),
                            final_mode[target],
                        )
                        write_index += 1
                        operation["from"].unlink()
                    else:
                        os.replace(operation["from"], target)
            for archive_path, content in archive_payloads.items():
                _install_staged(
                    archive_path,
                    content,
                    active_dir / "write-staging" / str(write_index),
                    final_mode[archive_path],
                )
                write_index += 1
            mark_active_applied(
                active_dir,
                str((archive / "result.json").relative_to(vault)),
            )
        except Exception:
            try:
                rollback_active_transaction(vault, active_dir)
            except RecoveryError as recovery_exc:
                raise PlanError(
                    f"apply failed; durable recovery is required from {active_dir}: "
                    f"{recovery_exc}"
                ) from recovery_exc
            raise
        return {"status": "applied", "requires_commit": True, **result}
