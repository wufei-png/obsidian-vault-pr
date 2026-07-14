"""Auditable semantic change plans with reviewer and note-count gates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .config import (
    ConfigError,
    domain_specs,
    load_vault_path,
    max_touched_notes,
    source_archive_dir,
    vault_relative_path,
)
from .git import require_clean_git_root, run_git, vault_write_lock
from .recovery import (
    RecoveryError,
    prepare_active_transaction,
    rollback_active_transaction,
)


class PlanError(RuntimeError):
    """Raised when a semantic change plan is invalid or unsafe."""


ALLOWED_ACTIONS = {"create", "update", "delete", "rename"}
ALLOWED_INTENTS = {"integrate", "merge", "split", "rename"}
MANDATORY_REVIEW_RISKS = {"merge", "delete", "contradiction", "work"}
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


def _proposed_content_paths(plan_path: Path) -> list[Path]:
    plan = _read_json(plan_path, "plan")
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


def _transaction_digest(plan_path: Path, content_paths: list[Path]) -> str:
    """Bind review to the exact plan and every proposed content file."""
    digest = hashlib.sha256()
    digest.update(b"plan\0")
    digest.update(plan_path.read_bytes())
    for content_path in sorted(set(content_paths)):
        relative = str(content_path.relative_to(plan_path.parent.resolve()))
        digest.update(b"file\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content_path.read_bytes())
    return digest.hexdigest()


def init_plan(config: dict[str, Any], source_id: str, summary: str) -> dict[str, Any]:
    """Create a plan template outside the worktree in the vault Git directory."""
    vault = load_vault_path(config)
    require_clean_git_root(vault)
    if not SOURCE_ID_PATTERN.fullmatch(source_id):
        raise PlanError("invalid source id")
    if not (source_archive_dir(config, vault) / source_id / "manifest.json").is_file():
        raise PlanError(f"unknown source id: {source_id}")
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


def _domain_for_path(config: dict[str, Any], vault: Path, path: Path) -> str:
    matches = [
        name
        for name, spec in domain_specs(config, vault).items()
        if path.is_relative_to(spec.root_dir)
    ]
    if len(matches) != 1:
        raise PlanError(f"note path must belong to exactly one domain: {path}")
    return matches[0]


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
    plan_path: Path, content_paths: list[Path]
) -> tuple[bool, str | None]:
    approval_path = plan_path.parent / "approval.json"
    if not approval_path.is_file():
        return False, None
    approval = _read_json(approval_path, "approval")
    valid = (
        approval.get("decision") == "approve"
        and approval.get("transaction_sha256")
        == _transaction_digest(plan_path, content_paths)
        and isinstance(approval.get("reviewer"), str)
        and bool(approval["reviewer"].strip())
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
    manifest_path = source_archive_dir(config, vault) / str(source_id) / "manifest.json"
    if (
        not isinstance(source_id, str)
        or not SOURCE_ID_PATTERN.fullmatch(source_id)
        or not manifest_path.is_file()
    ):
        raise PlanError("plan source_id does not resolve to a capture")
    manifest = _read_json(manifest_path, "source manifest")
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
    domains: set[str] = set()
    actions: set[str] = set()
    for index, raw_operation in enumerate(operations):
        if not isinstance(raw_operation, dict):
            raise PlanError(f"operation {index} must be an object")
        action = raw_operation.get("action")
        if action not in ALLOWED_ACTIONS:
            raise PlanError(f"operation {index} has unsupported action: {action}")
        raw_path = raw_operation.get("path")
        target = vault_relative_path(vault, raw_path, f"operations[{index}].path")
        if target.suffix.lower() != ".md" or ".vault-pr" in target.parts:
            raise PlanError(f"operation {index} must target a visible Markdown note")
        domain = _domain_for_path(config, vault, target)
        domains.add(domain)
        actions.add(action)
        if target in touched:
            raise PlanError(f"operation {index} duplicates an affected note path")
        touched.add(target)
        operation = {"action": action, "path": target, "domain": domain}

        source = target
        if action == "rename":
            source = vault_relative_path(
                vault, raw_operation.get("from"), f"operations[{index}].from"
            )
            if source.suffix.lower() != ".md" or not source.is_file():
                raise PlanError(f"operation {index} rename source is invalid")
            source_domain = _domain_for_path(config, vault, source)
            domains.add(source_domain)
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

        if action in {"create", "update"} or (
            action == "rename" and raw_operation.get("content_file")
        ):
            content_path = _content_file(plan_path, raw_operation)
            content = content_path.read_bytes()
            if not _frontmatter_ok(content):
                raise PlanError(f"operation {index} content has invalid frontmatter")
            operation["content_file"] = content_path
        normalized.append(operation)

    if not any(
        operation["action"] == "update" and operation["path"] == source_note
        for operation in normalized
    ):
        raise PlanError("plan must update the captured source note")

    maximum = max_touched_notes(config)
    if len(touched) > maximum:
        raise PlanError(f"plan touches {len(touched)} notes; maximum is {maximum}")

    configured_risks = config.get("review", {}).get(
        "independent_for", ["merge", "delete", "contradiction", "work"]
    )
    if not isinstance(configured_risks, list) or not all(
        isinstance(item, str) for item in configured_risks
    ):
        raise ConfigError("review.independent_for must be an array of strings")
    configured_risk_set = set(configured_risks)
    missing_risks = MANDATORY_REVIEW_RISKS - configured_risk_set
    unknown_risks = configured_risk_set - MANDATORY_REVIEW_RISKS
    if missing_risks:
        raise ConfigError(
            "review.independent_for cannot omit mandatory risks: "
            + ", ".join(sorted(missing_risks))
        )
    if unknown_risks:
        raise ConfigError(
            "review.independent_for contains unsupported risks: "
            + ", ".join(sorted(unknown_risks))
        )
    risks: list[str] = []
    if "merge" in configured_risks and intent == "merge":
        risks.append("merge")
    if "delete" in configured_risks and "delete" in actions:
        risks.append("delete")
    if "contradiction" in configured_risks and contradiction:
        risks.append("contradiction")
    if "work" in configured_risks and "work" in domains:
        risks.append("work")
    content_paths = [
        operation["content_file"]
        for operation in normalized
        if operation.get("content_file")
    ]
    transaction_sha256 = _transaction_digest(plan_path, content_paths)
    approval_valid, approval_path = _approval_status(plan_path, content_paths)
    return {
        "status": "valid",
        "transaction_id": transaction_id,
        "source_id": source_id,
        "plan_sha256": _plan_hash(plan_path),
        "transaction_sha256": transaction_sha256,
        "touched_notes": len(touched),
        "maximum_touched_notes": maximum,
        "domains": sorted(domains),
        "risks": risks,
        "approval_required": bool(risks),
        "approval_valid": approval_valid,
        "approval_path": approval_path,
        "operations": normalized,
        "plan": plan,
        "plan_path": plan_path,
    }


def record_review(
    plan_path: Path, reviewer: str, decision: str, notes: str = ""
) -> dict[str, Any]:
    """Bind an independent review decision to the exact plan bytes."""
    plan_path = plan_path.expanduser().resolve()
    _read_json(plan_path, "plan")
    reviewer = " ".join(reviewer.split()).strip()
    if not reviewer:
        raise PlanError("reviewer must not be empty")
    if decision not in {"approve", "reject"}:
        raise PlanError("review decision must be approve or reject")
    transaction_sha256 = _transaction_digest(
        plan_path, _proposed_content_paths(plan_path)
    )
    approval = {
        "schema_version": 1,
        "plan_sha256": _plan_hash(plan_path),
        "transaction_sha256": transaction_sha256,
        "reviewer": reviewer,
        "decision": decision,
        "notes": notes.strip(),
        "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    approval_path = plan_path.parent / "approval.json"
    approval_path.write_text(
        json.dumps(approval, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"approval": str(approval_path), **approval}


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".vault-pr-tmp")
    if temporary.exists():
        raise PlanError(f"stale transaction temporary file exists: {temporary}")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_plan(config: dict[str, Any], plan_path: Path) -> dict[str, Any]:
    """Apply a validated plan atomically and archive its audit record."""
    vault = load_vault_path(config)
    require_clean_git_root(vault)
    with vault_write_lock(vault):
        require_clean_git_root(vault)
        validation = validate_plan(config, plan_path)
        if validation["approval_required"] and not validation["approval_valid"]:
            raise PlanError(
                "independent reviewer approval is required for: "
                + ", ".join(validation["risks"])
            )

        operations = validation["operations"]
        affected: set[Path] = set()
        for operation in operations:
            affected.add(operation["path"])
            if operation["action"] == "rename":
                affected.add(operation["from"])
        transaction_root = (vault / ".vault-pr" / "transactions").resolve()
        if not transaction_root.is_relative_to(vault):
            raise PlanError("transaction archive directory escapes the vault")
        archive = transaction_root / validation["transaction_id"]
        if archive.exists():
            raise PlanError(f"transaction archive already exists: {archive}")

        final_sha256 = {
            path: _sha256_file(path) if path.is_file() else None for path in affected
        }
        for operation in operations:
            action = operation["action"]
            target = operation["path"]
            if action in {"create", "update"}:
                final_sha256[target] = _sha256_file(operation["content_file"])
            elif action == "delete":
                final_sha256[target] = None
            elif action == "rename":
                source = operation["from"]
                final_sha256[source] = None
                final_sha256[target] = (
                    _sha256_file(operation["content_file"])
                    if operation.get("content_file")
                    else _sha256_file(source)
                )

        active_dir = prepare_active_transaction(
            vault=vault,
            git_dir=_git_dir(vault),
            transaction_id=validation["transaction_id"],
            transaction_sha256=validation["transaction_sha256"],
            affected=affected,
            final_sha256=final_sha256,
        )
        try:
            for operation in operations:
                action = operation["action"]
                target = operation["path"]
                if action in {"create", "update"}:
                    _atomic_write(target, operation["content_file"].read_bytes())
                elif action == "delete":
                    target.unlink()
                elif action == "rename":
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(operation["from"], target)
                    if operation.get("content_file"):
                        _atomic_write(target, operation["content_file"].read_bytes())

            archive.mkdir(parents=True)
            shutil.copy2(validation["plan_path"], archive / "plan.json")
            for operation in operations:
                content_path = operation.get("content_file")
                if not content_path:
                    continue
                relative = content_path.relative_to(validation["plan_path"].parent)
                archived_content = archive / relative
                archived_content.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(content_path, archived_content)
            approval_path = validation["plan_path"].parent / "approval.json"
            if approval_path.is_file():
                shutil.copy2(approval_path, archive / "approval.json")
            result = {
                "schema_version": 1,
                "transaction_id": validation["transaction_id"],
                "source_id": validation["source_id"],
                "plan_sha256": validation["plan_sha256"],
                "transaction_sha256": validation["transaction_sha256"],
                "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "touched_notes": validation["touched_notes"],
                "risks": validation["risks"],
            }
            (archive / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
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
        shutil.rmtree(active_dir)
        return {"status": "applied", **result}
