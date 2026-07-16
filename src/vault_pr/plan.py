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

from .capture import CaptureError, load_validated_capture
from .config import (
    load_vault_path,
    max_touched_notes,
    note_path_allowed,
    review_policy,
    source_archive_dir,
    vault_relative_path,
)
from .errors import IngestError
from .git import require_clean_git_root, run_git, vault_write_lock
from .index import IndexUpdateError, validate_qmd_index_freshness
from .obsidian_validation import (
    ObsidianValidationError,
    validate_proposed_obsidian_state,
)
from .review import (
    REQUIRED_JUDGMENTS,
    ReviewError,
    build_review_packet,
    canonical_json,
    packet_sha256,
    validate_assessment,
    validate_digest,
)
from .source_note import resolve_source_note, source_note_has_id
from .quality_record import (
    QualityRecordError,
    load_quality_record,
    revalidate_quality_record,
)
from .quality import (
    AUTONOMOUS_CAPABILITY_ACTIONS,
    note_role_for_type,
    note_role_sets,
)
from .mutation import (
    MutationError,
    execute_journaled_mutation,
    inspect_journaled_mutation,
    mutation_response,
)
from .recovery import (
    RecoveryError,
    worktree_file_mode,
)


class PlanError(RuntimeError):
    """Raised when a semantic change plan is invalid or unsafe."""


ALLOWED_ACTIONS = {"create", "update", "delete", "rename", "purge_capture"}
ALLOWED_INTENTS = {"integrate", "maintenance", "merge", "split", "rename"}
ALLOWED_AUTHORIZATION_KINDS = {
    "maintenance_request",
    "autonomous_repair_policy",
}
ALLOWED_EVIDENCE_KINDS = {
    "source_capture",
    "quality_record",
}
SOURCE_ID_PATTERN = re.compile(r"src_[0-9a-f]{20}")
QUALITY_RECORD_ID_PATTERN = re.compile(r"quality_[0-9a-f]{20}")
TRANSACTION_ID_PATTERN = re.compile(r"txn_[A-Za-z0-9_-]{1,80}")
CHAIN_ID_PATTERN = re.compile(r"chain_[A-Za-z0-9_-]{1,80}")
HEX64_PATTERN = re.compile(r"[0-9a-f]{64}")
MAX_EVIDENCE_EXCERPT_CHARACTERS = 12_000


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PlanError(f"{label} not found") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"invalid {label}") from exc
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


def _governance_digest_snapshot(
    plan_path: Path,
    plan_bytes: bytes,
    content: dict[Path, bytes],
    authorization: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> str:
    """Bind plan bytes, proposed content, authorization, and evidence."""
    digest = hashlib.sha256()
    digest.update(b"plan\0")
    digest.update(plan_bytes)
    for content_path in sorted(content):
        relative = str(content_path.relative_to(plan_path.parent.resolve()))
        digest.update(b"file\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content[content_path])
    digest.update(b"authorization\0")
    digest.update(canonical_json(authorization))
    digest.update(b"evidence\0")
    digest.update(canonical_json(evidence))
    return digest.hexdigest()


def _transaction_digest(
    governance_sha256: str,
    review_packet: dict[str, Any],
) -> str:
    digest = hashlib.sha256()
    digest.update(b"governance\0")
    digest.update(governance_sha256.encode("ascii"))
    digest.update(b"review-packet\0")
    digest.update(canonical_json(review_packet))
    return digest.hexdigest()


def _validated_source_manifest(
    config: dict[str, Any], vault: Path, source_id: str
) -> dict[str, Any]:
    capture = source_archive_dir(config, vault) / source_id
    try:
        return load_validated_capture(capture, source_id=source_id, vault=vault)
    except CaptureError as exc:
        raise PlanError(f"source capture integrity failed: {exc}") from exc


def init_plan(
    config: dict[str, Any],
    summary: str,
    authorization_kind: str,
    authorization_summary: str,
    *,
    source_ids: list[str] | None = None,
    request_ref: str | None = None,
    request_sha256: str | None = None,
    chain_id: str | None = None,
) -> dict[str, Any]:
    """Create a plan template outside the worktree in the vault Git directory."""
    vault = load_vault_path(config)
    require_clean_git_root(vault)
    summary = " ".join(summary.split()).strip()
    if not summary:
        raise PlanError("plan summary must not be empty")
    authorization_summary = " ".join(authorization_summary.split()).strip()
    if not authorization_summary:
        raise PlanError("authorization summary must not be empty")
    if authorization_kind not in ALLOWED_AUTHORIZATION_KINDS:
        raise PlanError("unsupported authorization kind")
    authorization: dict[str, Any] = {
        "kind": authorization_kind,
        "summary": authorization_summary,
    }
    if request_ref is not None or request_sha256 is not None:
        authorization["request_ref"] = request_ref
        authorization["request_sha256"] = request_sha256
    evidence: list[dict[str, Any]] = []
    for source_id in source_ids or []:
        if not SOURCE_ID_PATTERN.fullmatch(source_id):
            raise PlanError("invalid source id")
        _validated_source_manifest(config, vault, source_id)
        evidence.append({"kind": "source_capture", "source_id": source_id})
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
        "schema_version": 2,
        "transaction_id": transaction_id,
        "summary": summary,
        "authorization": authorization,
        "evidence": evidence,
        "intent": "integrate" if evidence else "maintenance",
        "retrieval": (
            {"required": True}
            if evidence
            else {
                "required": False,
                "reason": "maintenance request does not depend on retrieval",
            }
        ),
        "contradiction": False,
        "operations": [],
    }
    if chain_id is not None:
        plan["chain_id"] = chain_id
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


def _normalized_summary(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise PlanError(f"{field} must be a string")
    normalized = " ".join(value.split()).strip()
    if not normalized:
        raise PlanError(f"{field} must not be empty")
    return normalized


def _normalize_authorization(
    config: dict[str, Any],
    raw: object,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PlanError("plan authorization must be an object")
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in ALLOWED_AUTHORIZATION_KINDS:
        raise PlanError("plan authorization kind is unsupported")
    summary = _normalized_summary(raw.get("summary"), "authorization.summary")
    if kind == "maintenance_request":
        allowed = {"kind", "summary", "request_ref", "request_sha256"}
        if set(raw) - allowed:
            raise PlanError("maintenance request authorization has unknown fields")
        request_ref = raw.get("request_ref")
        request_sha256 = raw.get("request_sha256")
        if (request_ref is None) != (request_sha256 is None):
            raise PlanError(
                "authorization request_ref and request_sha256 must be provided together"
            )
        normalized: dict[str, Any] = {"kind": kind, "summary": summary}
        if request_ref is not None:
            if not isinstance(request_ref, str) or not request_ref.strip():
                raise PlanError("authorization request_ref must be non-empty")
            if (
                not isinstance(request_sha256, str)
                or not HEX64_PATTERN.fullmatch(request_sha256)
            ):
                raise PlanError("authorization request_sha256 must be a SHA-256 digest")
            normalized["request_ref"] = request_ref.strip()
            normalized["request_sha256"] = request_sha256
        return normalized

    if set(raw) != {"kind", "summary"}:
        raise PlanError(
            "autonomous repair authorization policy fields are Kernel-generated"
        )
    autonomous = config.get("autonomous_repair", {})
    if not isinstance(autonomous, dict) or autonomous.get("enabled", False) is not True:
        raise PlanError(
            "maintenance_request_required: autonomous repair policy is not enabled"
        )
    policy = {
        "enabled": True,
        "capabilities_version": 1,
        "allowed_actions": ["create", "update", "rename"],
        "maximum_touched_notes": 3,
        "independent_review_required": True,
    }
    return {
        "kind": kind,
        "summary": summary,
        "policy": policy,
        "policy_sha256": _sha256_bytes(
            canonical_json({"autonomous_repair": autonomous, "policy": policy})
        ),
    }


def _bounded_excerpt(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PlanError("evidence is not readable UTF-8 text") from exc
    return text[:MAX_EVIDENCE_EXCERPT_CHARACTERS]


def _normalize_source_evidence(
    config: dict[str, Any],
    vault: Path,
    raw: dict[str, Any],
) -> dict[str, Any]:
    if set(raw) != {"kind", "source_id"}:
        raise PlanError("source_capture evidence has unknown or missing fields")
    source_id = raw.get("source_id")
    if not isinstance(source_id, str) or not SOURCE_ID_PATTERN.fullmatch(source_id):
        raise PlanError("source_capture evidence has an invalid source_id")
    manifest = _validated_source_manifest(config, vault, source_id)
    capture = source_archive_dir(config, vault) / source_id
    manifest_path = capture / "manifest.json"
    derived = manifest.get("derived")
    if not isinstance(derived, dict) or not isinstance(
        derived.get("extracted"), dict
    ):
        raise PlanError("source capture manifest is missing derived.extracted")
    extracted = vault_relative_path(
        vault,
        str(
            (
                capture / str(derived["extracted"].get("path"))
            ).relative_to(vault)
        ),
        "source capture extracted artifact",
    )
    try:
        source_note = resolve_source_note(config, vault, source_id)
    except Exception as exc:
        raise PlanError(str(exc)) from exc
    return {
        "kind": "source_capture",
        "evidence_id": f"source_capture:{source_id}",
        "source_id": source_id,
        "manifest_sha256": _sha256_file(manifest_path),
        "primary_sha256": manifest.get("primary", {}).get("sha256"),
        "extracted_sha256": derived["extracted"].get("sha256"),
        "source_note": str(source_note.relative_to(vault)),
        "source_note_sha256": _sha256_file(source_note),
        "title": str(manifest.get("title", "")),
        "excerpt": _bounded_excerpt(extracted),
    }


def _normalize_quality_evidence(
    config: dict[str, Any],
    vault: Path,
    raw: dict[str, Any],
) -> dict[str, Any]:
    if set(raw) != {"kind", "record_id", "finding_ids"}:
        raise PlanError("quality_record evidence has unknown or missing fields")
    record_id = raw.get("record_id")
    finding_ids = raw.get("finding_ids")
    if (
        not isinstance(record_id, str)
        or not QUALITY_RECORD_ID_PATTERN.fullmatch(record_id)
        or not isinstance(finding_ids, list)
        or not finding_ids
        or not all(isinstance(value, str) and value for value in finding_ids)
        or len(finding_ids) != len(set(finding_ids))
    ):
        raise PlanError("quality_record evidence identity is invalid")
    try:
        record, path = load_quality_record(vault, record_id)
        selected = revalidate_quality_record(config, record, finding_ids)
    except QualityRecordError as exc:
        raise PlanError(str(exc)) from exc
    return {
        "kind": "quality_record",
        "evidence_id": f"quality_record:{record_id}",
        "record_id": record_id,
        "finding_ids": sorted(finding_ids),
        "record_sha256": _sha256_file(path),
        "findings": selected,
    }


def _normalize_evidence(
    config: dict[str, Any],
    vault: Path,
    raw_evidence: object,
) -> list[dict[str, Any]]:
    if not isinstance(raw_evidence, list):
        raise PlanError("plan evidence must be an array")
    normalized: list[dict[str, Any]] = []
    identities: set[str] = set()
    for index, raw in enumerate(raw_evidence):
        if not isinstance(raw, dict):
            raise PlanError(f"evidence {index} has an unsupported kind")
        kind = raw.get("kind")
        if not isinstance(kind, str) or kind not in ALLOWED_EVIDENCE_KINDS:
            raise PlanError(f"evidence {index} has an unsupported kind")
        if kind == "source_capture":
            item = _normalize_source_evidence(config, vault, raw)
        else:
            item = _normalize_quality_evidence(config, vault, raw)
        if item["evidence_id"] in identities:
            raise PlanError(f"evidence {index} duplicates an evidence identity")
        identities.add(item["evidence_id"])
        normalized.append(item)
    return normalized


def _assessment_status(
    plan_dir: Path,
    transaction_sha256: str,
    packet: dict[str, Any],
    history: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    assessment_path = plan_dir / "assessment.json"
    if not assessment_path.is_file() or not history:
        return False, None
    assessment = _read_json(assessment_path, "semantic review assessment")
    last = history[-1]
    try:
        validate_assessment(
            assessment,
            packet,
            prior_instance_ids={
                entry["assessment"]["instance_id"] for entry in history[:-1]
            },
        )
    except (KeyError, ReviewError):
        return False, str(assessment_path)
    valid = bool(
        last.get("transaction_sha256") == transaction_sha256
        and last.get("review_packet_sha256") == packet_sha256(packet)
        and last.get("assessment") == assessment
        and assessment.get("status") == "approve"
    )
    return valid, str(assessment_path)


def validate_plan(config: dict[str, Any], plan_path: Path) -> dict[str, Any]:
    """Validate paths, hashes, note count, risk, and reviewer approval."""
    vault = load_vault_path(config)
    plan_path = plan_path.expanduser().resolve()
    plan = _read_json(plan_path, "plan")
    if plan.get("schema_version") != 2:
        raise PlanError("unsupported plan schema_version")
    allowed_plan_fields = {
        "schema_version",
        "transaction_id",
        "chain_id",
        "summary",
        "authorization",
        "evidence",
        "intent",
        "retrieval",
        "contradiction",
        "operations",
    }
    if set(plan) - allowed_plan_fields:
        raise PlanError("plan has unknown fields")
    transaction_id = plan.get("transaction_id")
    if not isinstance(transaction_id, str) or not TRANSACTION_ID_PATTERN.fullmatch(
        transaction_id
    ):
        raise PlanError("invalid transaction_id")
    chain_id = plan.get("chain_id")
    if chain_id is not None and (
        not isinstance(chain_id, str) or not CHAIN_ID_PATTERN.fullmatch(chain_id)
    ):
        raise PlanError("invalid chain_id")
    summary = _normalized_summary(plan.get("summary"), "plan summary")
    authorization = _normalize_authorization(config, plan.get("authorization"))
    evidence = _normalize_evidence(config, vault, plan.get("evidence"))
    intent = plan.get("intent", "integrate")
    if not isinstance(intent, str) or intent not in ALLOWED_INTENTS:
        raise PlanError(f"unsupported plan intent: {intent}")
    retrieval = plan.get("retrieval")
    if not isinstance(retrieval, dict):
        raise PlanError("plan retrieval declaration is required")
    if retrieval.get("required") is True:
        if set(retrieval) != {"required"}:
            raise PlanError("required retrieval declaration has unknown fields")
        try:
            index_freshness = validate_qmd_index_freshness(config)
        except IndexUpdateError as exc:
            raise PlanError(f"retrieval index validation failed: {exc}") from exc
        if not index_freshness.get("eligible"):
            raise PlanError(
                "retrieval index is stale: "
                + ", ".join(index_freshness.get("reasons", ["unknown"]))
            )
    elif retrieval.get("required") is False:
        if set(retrieval) != {"required", "reason"}:
            raise PlanError(
                "retrieval_not_required declaration has unknown fields"
            )
        reason = _normalized_summary(
            retrieval.get("reason"), "retrieval_not_required reason"
        )
        retrieval = {"required": False, "reason": reason}
        index_freshness = None
        if intent == "integrate":
            raise PlanError("integration plan cannot skip retrieval")
    else:
        raise PlanError("plan retrieval.required must be boolean")
    contradiction = plan.get("contradiction", False)
    if not isinstance(contradiction, bool):
        raise PlanError("plan contradiction must be boolean")
    source_evidence = [
        item for item in evidence if item["kind"] == "source_capture"
    ]
    quality_evidence = [
        item for item in evidence if item["kind"] == "quality_record"
    ]
    if intent == "integrate" and not source_evidence:
        raise PlanError("integration plan requires source_capture evidence")
    if (
        authorization["kind"] == "autonomous_repair_policy"
        and not quality_evidence
    ):
        raise PlanError("autonomous repair requires quality_record evidence")
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
        if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
            raise PlanError(f"operation {index} has unsupported action: {action}")
        if action == "purge_capture":
            if set(raw_operation) != {"action", "source_id"}:
                raise PlanError(
                    f"operation {index} purge_capture has unknown fields"
                )
            if authorization["kind"] != "maintenance_request":
                raise PlanError(
                    "maintenance_request_required: purge_capture requires an "
                    "explicit maintenance request"
                )
            source_id = raw_operation.get("source_id")
            if (
                not isinstance(source_id, str)
                or not SOURCE_ID_PATTERN.fullmatch(source_id)
            ):
                raise PlanError(f"operation {index} has invalid source_id")
            capture_dir = source_archive_dir(config, vault) / source_id
            try:
                manifest = load_validated_capture(
                    capture_dir,
                    source_id=source_id,
                    vault=vault,
                )
                source_note = resolve_source_note(config, vault, source_id)
            except (CaptureError, IngestError) as exc:
                raise PlanError(f"purge_capture source is invalid: {exc}") from exc
            declared = {
                Path("manifest.json"),
                Path(str(manifest["primary"]["path"])),
                Path(str(manifest["derived"]["extracted"]["path"])),
                *[
                    Path(str(item["path"]))
                    for item in manifest.get("files", [])
                ],
            }
            purge_paths = [capture_dir / relative for relative in sorted(declared)]
            if source_note in touched:
                raise PlanError(
                    f"operation {index} duplicates an affected note path"
                )
            touched.add(source_note)
            actions.add(action)
            normalized.append(
                {
                    "action": "delete",
                    "path": source_note,
                    "expected_sha256": _sha256_file(source_note),
                    "purge_source_id": source_id,
                }
            )
            for path in purge_paths:
                normalized.append(
                    {
                        "action": "delete",
                        "path": path,
                        "expected_sha256": _sha256_file(path),
                        "capture_artifact": True,
                        "purge_source_id": source_id,
                    }
                )
            continue
        operation_fields = {
            "create": {"action", "path", "content_file"},
            "update": {"action", "path", "expected_sha256", "content_file"},
            "delete": {"action", "path", "expected_sha256"},
            "rename": {"action", "path", "from", "expected_sha256"},
        }[action]
        if action == "rename" and "content_file" in raw_operation:
            operation_fields.add("content_file")
        if set(raw_operation) != operation_fields:
            raise PlanError(
                f"operation {index} {action} has unknown or missing fields"
            )
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

    if intent == "integrate":
        for source in source_evidence:
            source_note = vault / source["source_note"]
            source_updates = [
                operation
                for operation in normalized
                if operation["action"] == "update"
                and operation["path"] == source_note
            ]
            if not source_updates:
                raise PlanError(
                    f"plan must update source note for {source['source_id']}"
                )
            try:
                preserves_source_id = source_note_has_id(
                    source_updates[0]["content_file"], source["source_id"]
                )
            except IngestError as exc:
                raise PlanError(f"proposed source note is invalid: {exc}") from exc
            if not preserves_source_id:
                raise PlanError(
                    "proposed source note must preserve source_id "
                    f"{source['source_id']}"
                )

    maximum = max_touched_notes(config)
    if authorization["kind"] == "autonomous_repair_policy":
        maximum = min(maximum, 3)
        if intent in {"merge", "split"} or contradiction:
            raise PlanError(
                "maintenance_request_required: autonomous repair cannot perform "
                f"{'contradiction' if contradiction else intent}"
            )
        disallowed = sorted(actions - {"create", "update", "rename"})
        if disallowed:
            raise PlanError(
                "maintenance_request_required: autonomous repair cannot perform "
                + ", ".join(disallowed)
            )
    if len(touched) > maximum:
        raise PlanError(f"plan touches {len(touched)} notes; maximum is {maximum}")

    for purge_source_id in {
        operation["purge_source_id"]
        for operation in normalized
        if operation.get("purge_source_id")
    }:
        source_note = next(
            operation["path"]
            for operation in normalized
            if operation.get("purge_source_id") == purge_source_id
            and not operation.get("capture_artifact")
        )
        proposed_by_path: dict[Path, bytes | None] = {}
        for operation in normalized:
            if operation.get("capture_artifact"):
                continue
            if operation["action"] == "delete":
                proposed_by_path[operation["path"]] = None
            elif operation["action"] in {"create", "update"}:
                proposed_by_path[operation["path"]] = operation[
                    "content_file"
                ].read_bytes()
            elif operation["action"] == "rename":
                proposed_by_path[operation["from"]] = None
                proposed_by_path[operation["path"]] = (
                    operation["content_file"].read_bytes()
                    if operation.get("content_file")
                    else operation["from"].read_bytes()
                )
        tracked = run_git(vault, "ls-files", "-z", "--", "*.md")
        if tracked.returncode != 0:
            raise PlanError("unable to inspect source references before purge")
        remaining_references: list[str] = []
        for raw in tracked.stdout.split("\0"):
            if not raw:
                continue
            path = vault / raw
            if path == source_note:
                continue
            content = proposed_by_path.get(path)
            if content is None and path in proposed_by_path:
                continue
            if content is None:
                content = path.read_bytes()
            if purge_source_id.encode("utf-8") in content:
                remaining_references.append(raw)
        if remaining_references:
            raise PlanError(
                "purge_capture has remaining source_id references: "
                + ", ".join(sorted(remaining_references))
            )
        archive_root = source_archive_dir(config, vault)
        for candidate in sorted(archive_root.iterdir()) if archive_root.is_dir() else []:
            if candidate.name == purge_source_id or not candidate.is_dir():
                continue
            try:
                other = load_validated_capture(
                    candidate,
                    source_id=candidate.name,
                    vault=vault,
                )
            except CaptureError as exc:
                raise PlanError(
                    f"cannot prove purge safety because another capture is invalid: {exc}"
                ) from exc
            if other.get("source", {}).get("supersedes") == purge_source_id:
                raise PlanError(
                    f"purge_capture is referenced by superseding capture {candidate.name}"
                )

    try:
        obsidian_validation = validate_proposed_obsidian_state(
            config,
            vault,
            [
                operation
                for operation in normalized
                if not operation.get("capture_artifact")
            ],
        )
    except ObsidianValidationError as exc:
        raise PlanError(str(exc)) from exc
    if intent == "integrate":
        resolved = obsidian_validation["resolved_links_by_path"]
        raw_roles = obsidian_validation["prospective_note_roles"]
        role_sets = note_role_sets(config)
        modified_synthesis_paths = {
            str(operation["path"].relative_to(vault))
            for operation in normalized
            if operation["action"] in {"create", "update", "rename"}
            and note_role_for_type(
                raw_roles.get(str(operation["path"].relative_to(vault))),
                role_sets,
            )
            in {"concept", "index"}
        }
        for source in source_evidence:
            source_note = source["source_note"]
            if not any(
                source_note in resolved.get(owner, [])
                for owner in modified_synthesis_paths
            ):
                raise PlanError(
                    "integration source "
                    f"{source['source_id']} must be referenced by a modified "
                    "synthesis note"
                )
    if authorization["kind"] == "autonomous_repair_policy":
        selected_findings = [
            finding
            for item in quality_evidence
            for finding in item.get("findings", [])
        ]
        prospective_debt = {
            (item["kind"], item["path"], item["target"])
            for item in obsidian_validation["prospective_reference_debt"]
        }
        roles = obsidian_validation["prospective_note_roles"]
        resolved = obsidian_validation["resolved_links_by_path"]
        owner_paths = {str(finding.get("path")) for finding in selected_findings}
        source_synthesis_owners = {
            str(finding.get("path"))
            for finding in selected_findings
            if finding.get("rule_id") == "source_without_synthesis_link"
        }
        capabilities = {
            str(finding.get("autonomous_capability"))
            for finding in selected_findings
        }
        capability_actions = set().union(
            *(
                AUTONOMOUS_CAPABILITY_ACTIONS.get(capability, frozenset())
                for capability in capabilities
            )
        )
        linked_targets = {
            target
            for owner in owner_paths
            for target in resolved.get(owner, [])
        }
        configured_roles = note_role_sets(config)

        def repairs_source_synthesis_link(path: str) -> bool:
            return (
                note_role_for_type(roles.get(path), configured_roles)
                in {"concept", "index"}
                and any(
                    source in resolved.get(path, [])
                    for source in source_synthesis_owners
                )
            )

        creates = 0
        modified_paths: set[str] = set()
        for operation in normalized:
            action = operation["action"]
            relative = str(operation["path"].relative_to(vault))
            modified_paths.add(relative)
            if action not in capability_actions:
                raise PlanError(
                    "maintenance_request_required: selected autonomous "
                    f"capabilities do not authorize {action}"
                )
            if (
                action == "update"
                and relative not in owner_paths
                and not repairs_source_synthesis_link(relative)
            ):
                raise PlanError(
                    "maintenance_request_required: autonomous repair can only "
                    "update a selected finding owner or a synthesis note that "
                    "repairs a selected Source Note"
                )
            if action == "create":
                creates += 1
                if (
                    relative not in linked_targets
                    and not repairs_source_synthesis_link(relative)
                ):
                    raise PlanError(
                        "maintenance_request_required: autonomous repair can only "
                        "create a note referenced by a selected finding owner or "
                        "a synthesis note that repairs a selected Source Note"
                    )
        if creates > len(selected_findings):
            raise PlanError(
                "maintenance_request_required: autonomous repair creates more "
                "notes than selected findings"
            )
        for finding in selected_findings:
            rule_id = finding.get("rule_id")
            owner = finding.get("path")
            if rule_id == "broken_wikilink":
                identity = ("broken_wikilink", owner, finding.get("target"))
                if identity in prospective_debt:
                    raise PlanError(
                        "autonomous repair does not satisfy the selected finding"
                    )
            elif rule_id == "source_without_synthesis_link":
                if not any(
                    owner in resolved.get(path, [])
                    and note_role_for_type(roles.get(path), configured_roles)
                    in {"concept", "index"}
                    for path in modified_paths
                ):
                    raise PlanError(
                        "autonomous repair does not satisfy the selected finding"
                    )
            else:
                raise PlanError(
                    "maintenance_request_required: selected finding has no "
                    "autonomous repair capability"
                )

    policy = review_policy(config)
    risks: list[str] = []
    if intent == "merge":
        risks.append("merge")
    if intent == "split":
        risks.append("split")
    if "delete" in actions:
        risks.append("delete")
    if "purge_capture" in actions:
        risks.extend(["delete", "purge_capture"])
    if contradiction:
        risks.append("contradiction")
    if authorization["kind"] == "autonomous_repair_policy":
        risks.append("autonomous_repair")
    if policy.review_all_semantic_transactions and not risks:
        risks.append("vault_policy")
    history = _review_history(plan_path.parent / "review-history.json")
    _, rejection_count = _validate_review_history(history)
    if history and not risks:
        risks.append("review_history")
    risks = list(dict.fromkeys(risks))
    content_paths = [
        operation["content_file"]
        for operation in normalized
        if operation.get("content_file")
    ]
    plan_bytes = plan_path.read_bytes()
    content_snapshots = {path: path.read_bytes() for path in content_paths}
    governance_sha256 = _governance_digest_snapshot(
        plan_path,
        plan_bytes,
        content_snapshots,
        authorization,
        evidence,
    )
    proposed_contents = []
    for operation in normalized:
        content_file = operation.get("content_file")
        if content_file is None:
            continue
        try:
            text = content_snapshots[content_file].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PlanError("proposed Markdown content must be UTF-8") from exc
        proposed_contents.append(
            {
                "evidence_id": f"content:{operation['path'].relative_to(vault)}",
                "path": str(operation["path"].relative_to(vault)),
                "content": text,
                "sha256": _sha256_bytes(content_snapshots[content_file]),
            }
        )
    evidence_ids = [
        "plan",
        "authorization",
        *[item["evidence_id"] for item in evidence],
        *[item["evidence_id"] for item in proposed_contents],
    ]
    packet_payload = {
        "governance_sha256": governance_sha256,
        "plan": plan,
        "authorization": authorization,
        "evidence": evidence,
        "retrieval": retrieval,
        "index_freshness": index_freshness,
        "proposed_contents": proposed_contents,
        "obsidian_validation": obsidian_validation,
        "risks": risks,
        "required_judgments": list(REQUIRED_JUDGMENTS),
        "evidence_ids": evidence_ids,
        "unassessed": [],
    }
    try:
        review_packet = build_review_packet(packet_payload)
    except ReviewError as exc:
        raise PlanError(str(exc)) from exc
    transaction_sha256 = _transaction_digest(governance_sha256, review_packet)
    revision_limit_exhausted = rejection_count > policy.max_revision_rounds
    approval_valid, approval_path = _assessment_status(
        plan_path.parent,
        transaction_sha256,
        review_packet,
        history,
    )
    return {
        "status": "valid",
        "transaction_id": transaction_id,
        "chain_id": chain_id,
        "summary": summary,
        "authorization": authorization,
        "evidence": evidence,
        "retrieval": retrieval,
        "index_freshness": index_freshness,
        "source_ids": [item["source_id"] for item in source_evidence],
        "plan_sha256": _plan_hash(plan_path),
        "governance_sha256": governance_sha256,
        "transaction_sha256": transaction_sha256,
        "semantic_review_packet": review_packet,
        "review_packet_sha256": packet_sha256(review_packet),
        "touched_notes": len(touched),
        "maximum_touched_notes": maximum,
        "risks": risks,
        "approval_required": bool(risks),
        "approval_valid": approval_valid,
        "assessment_path": approval_path,
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
        raise PlanError("invalid review history") from exc
    if not isinstance(history, list) or not all(
        isinstance(entry, dict) for entry in history
    ):
        raise PlanError("review history must be an array")
    return history


def _validate_review_history(
    history: list[dict[str, Any]],
) -> tuple[set[str], int]:
    """Validate reviewer decisions and return rejected digests and their count."""
    rejected_digests: set[str] = set()
    instance_ids: set[str] = set()
    rejection_count = 0
    for index, entry in enumerate(history):
        assessment = entry.get("assessment")
        digest = entry.get("transaction_sha256")
        policy = entry.get("review_policy")
        if (
            set(entry)
            != {
                "schema_version",
                "plan_sha256",
                "transaction_sha256",
                "review_packet_sha256",
                "assessment",
                "review_policy",
                "reviewed_at",
            }
            or entry.get("schema_version") != 2
            or not isinstance(assessment, dict)
            or assessment.get("status") not in {"approve", "reject"}
            or not isinstance(entry.get("reviewed_at"), str)
            or not entry["reviewed_at"].strip()
            or not isinstance(policy, dict)
            or not isinstance(policy.get("review_all_semantic_transactions"), bool)
            or isinstance(policy.get("max_revision_rounds"), bool)
            or not isinstance(policy.get("max_revision_rounds"), int)
            or not 0 <= policy["max_revision_rounds"] <= 5
        ):
            raise PlanError(f"review history entry {index} is invalid")
        try:
            validate_digest(digest, f"review history entry {index} transaction")
            validate_digest(
                entry.get("plan_sha256"),
                f"review history entry {index} plan",
            )
            validate_digest(
                entry.get("review_packet_sha256"),
                f"review history entry {index} packet",
            )
        except ReviewError as exc:
            raise PlanError(str(exc)) from exc
        instance_id = assessment.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise PlanError(f"review history entry {index} instance_id is invalid")
        if instance_id in instance_ids:
            raise PlanError("review history reuses a reviewer instance_id")
        instance_ids.add(instance_id)
        if digest in rejected_digests:
            raise PlanError(
                "review history reuses plan content that was already rejected"
            )
        if assessment["status"] == "reject":
            rejected_digests.add(digest)
            rejection_count += 1
    return rejected_digests, rejection_count


def record_review(
    config: dict[str, Any],
    plan_path: Path,
    assessment_path: Path,
) -> dict[str, Any]:
    """Validate and record a structured decision for the current review packet."""
    plan_path = plan_path.expanduser().resolve()
    validation = validate_plan(config, plan_path)
    assessment_path = assessment_path.expanduser().resolve()
    assessment = _read_json(assessment_path, "semantic review assessment")
    transaction_sha256 = validation["transaction_sha256"]
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
    prior_instance_ids = {
        entry["assessment"]["instance_id"] for entry in history
    }
    try:
        validate_assessment(
            assessment,
            validation["semantic_review_packet"],
            prior_instance_ids=prior_instance_ids,
        )
    except ReviewError as exc:
        raise PlanError(str(exc)) from exc
    entry = {
        "schema_version": 2,
        "plan_sha256": validation["plan_sha256"],
        "transaction_sha256": transaction_sha256,
        "review_packet_sha256": validation["review_packet_sha256"],
        "assessment": assessment,
        "review_policy": {
            "review_all_semantic_transactions": policy.review_all_semantic_transactions,
            "max_revision_rounds": policy.max_revision_rounds,
        },
        "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    history.append(entry)
    _atomic_write(
        history_path,
        (json.dumps(history, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    stored_assessment_path = plan_path.parent / "assessment.json"
    _atomic_write(
        stored_assessment_path,
        (json.dumps(assessment, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return {
        "assessment_path": str(stored_assessment_path),
        "review_history": str(history_path),
        "remaining_revision_rounds": max(
            0, policy.max_revision_rounds - rejection_count
        ),
        **entry,
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


def _plan_request_identity(
    config: dict[str, Any],
    plan_path: Path,
) -> tuple[str, str]:
    """Hash the immutable apply request without depending on current vault state."""
    plan_path = plan_path.expanduser().resolve()
    plan = _read_json(plan_path, "plan")
    transaction_id = plan.get("transaction_id")
    if not isinstance(transaction_id, str) or not TRANSACTION_ID_PATTERN.fullmatch(
        transaction_id
    ):
        raise PlanError("invalid transaction_id")
    digest = hashlib.sha256()
    digest.update(b"plan\0")
    digest.update(plan_path.read_bytes())
    for content_path in _proposed_content_paths(plan_path, plan):
        digest.update(b"content\0")
        digest.update(
            content_path.relative_to(plan_path.parent).as_posix().encode("utf-8")
        )
        digest.update(b"\0")
        digest.update(content_path.read_bytes())
    for name in ("assessment.json", "review-history.json"):
        path = plan_path.parent / name
        digest.update(name.encode("ascii"))
        digest.update(b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
    relevant_config = {
        key: config.get(key, {})
        for key in (
            "semantic",
            "sources",
            "notes",
            "review",
            "quality",
            "index",
            "autonomous_repair",
        )
    }
    digest.update(b"config\0")
    digest.update(canonical_json(relevant_config))
    return transaction_id, digest.hexdigest()


def apply_plan(config: dict[str, Any], plan_path: Path) -> dict[str, Any]:
    """Apply a validated plan atomically and archive its audit record."""
    vault = load_vault_path(config)
    transaction_id, request_sha256 = _plan_request_identity(config, plan_path)
    git_dir = _git_dir(vault)
    try:
        existing = inspect_journaled_mutation(
            vault=vault,
            git_dir=git_dir,
            operation="semantic_plan",
            transaction_id=transaction_id,
            transaction_sha256=None,
            request_sha256=request_sha256,
        )
    except MutationError as exc:
        raise PlanError(str(exc)) from exc
    if existing is not None:
        return mutation_response(existing, applied_status="applied")
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
        governance_sha256 = _governance_digest_snapshot(
            frozen_plan_path,
            plan_bytes,
            content_snapshots,
            validation["authorization"],
            validation["evidence"],
        )
        transaction_sha256 = _transaction_digest(
            governance_sha256,
            validation["semantic_review_packet"],
        )
        if transaction_sha256 != validation["transaction_sha256"]:
            raise PlanError("plan or proposed content changed during validation")
        assessment_path = frozen_plan_path.parent / "assessment.json"
        assessment_bytes = (
            assessment_path.read_bytes() if assessment_path.is_file() else None
        )
        review_history_path = frozen_plan_path.parent / "review-history.json"
        review_history_bytes = (
            review_history_path.read_bytes() if review_history_path.is_file() else None
        )
        if validation["revision_limit_exhausted"]:
            raise PlanError(
                "review revision limit exceeded under the configured vault policy"
            )
        approval_valid, _assessment_record_path = _assessment_status(
            frozen_plan_path.parent,
            transaction_sha256,
            validation["semantic_review_packet"],
            _review_history(review_history_path),
        )
        if validation["approval_required"] and not approval_valid:
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
            "schema_version": 2,
            "transaction_id": validation["transaction_id"],
            "chain_id": validation["chain_id"],
            "source_ids": validation["source_ids"],
            "authorization": validation["authorization"],
            "retrieval": validation["retrieval"],
            "index_freshness": validation["index_freshness"],
            "evidence": [
                {
                    key: value
                    for key, value in item.items()
                    if key != "excerpt"
                }
                for item in validation["evidence"]
            ],
            "plan_sha256": _sha256_bytes(plan_bytes),
            "governance_sha256": governance_sha256,
            "transaction_sha256": transaction_sha256,
            "review_packet_sha256": validation["review_packet_sha256"],
            "base_commit": run_git(vault, "rev-parse", "HEAD").stdout.strip(),
            "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "touched_notes": validation["touched_notes"],
            "risks": validation["risks"],
            "files": [
                {
                    "path": str(path.relative_to(vault)),
                    "original_sha256": expected_original_sha256.get(path),
                    "final_sha256": final_sha256.get(path),
                }
                for path in sorted(affected)
            ],
        }
        result_bytes = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
        archive_payloads = {
            archive / "plan.json": plan_bytes,
            archive / "result.json": result_bytes,
        }
        if assessment_bytes is not None:
            archive_payloads[archive / "assessment.json"] = assessment_bytes
        if review_history_bytes is not None:
            archive_payloads[archive / "review-history.json"] = review_history_bytes
        affected.update(archive_payloads)
        expected_original_sha256.update({path: None for path in archive_payloads})
        expected_original_mode.update({path: None for path in archive_payloads})
        final_sha256.update(
            {path: _sha256_bytes(content) for path, content in archive_payloads.items()}
        )
        final_mode.update({path: "100644" for path in archive_payloads})

        def write(active_dir: Path) -> None:
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
            for source_id in {
                operation["purge_source_id"]
                for operation in operations
                if operation.get("purge_source_id")
            }:
                capture_dir = source_archive_dir(config, vault) / source_id
                for directory in sorted(
                    (
                        path
                        for path in capture_dir.rglob("*")
                        if path.is_dir()
                    ),
                    key=lambda path: len(path.parts),
                    reverse=True,
                ):
                    directory.rmdir()
                capture_dir.rmdir()
            for archive_path, content in archive_payloads.items():
                _install_staged(
                    archive_path,
                    content,
                    active_dir / "write-staging" / str(write_index),
                    final_mode[archive_path],
                )
                write_index += 1

        try:
            outcome = execute_journaled_mutation(
                vault=vault,
                git_dir=git_dir,
                transaction_id=validation["transaction_id"],
                transaction_sha256=validation["transaction_sha256"],
                request_sha256=request_sha256,
                operation="semantic_plan",
                receipt_data=result,
                affected=affected,
                expected_original_sha256=expected_original_sha256,
                expected_original_mode=expected_original_mode,
                final_sha256=final_sha256,
                final_mode=final_mode,
                write=write,
                completion_path=str((archive / "result.json").relative_to(vault)),
                lock=False,
            )
        except MutationError as exc:
            raise PlanError(str(exc)) from exc
        return mutation_response(outcome, applied_status="applied")
