"""Auditable semantic change plans with reviewer and note-count gates."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .files import atomic_replace_bytes
import yaml

from .asset_promotion import (
    AssetPromotionError,
    install_created_promotion,
    load_promotion_snapshots,
    load_managed_asset_index,
    normalize_asset_promotion,
    promotion_results,
    promotion_review_evidence,
    validate_asset_promotion_batch,
    validate_promotion_references,
)
from .capture import (
    CaptureError,
    load_validated_capture,
    load_validated_capture_artifact_text,
)
from .config import (
    load_vault_path,
    max_touched_notes,
    note_path_allowed,
    review_policy,
    source_archive_dir,
    vault_relative_path,
)
from .errors import IngestError, PlanError
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
from .source_note import resolve_source_note, source_note_bytes_have_id
from .quality import (
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
from .retrieve import RetrievalError, retrieve_related_notes
from .plan_models import (
    ALLOWED_ACTIONS,
    ALLOWED_INTENTS,
    FOCUS_RANGES_ABSENT,
    MAX_EVIDENCE_EXCERPT_CHARACTERS,
    MAX_RETRIEVAL_SNIPPET_CHARACTERS,
    PLAN_SCHEMA_VERSION,
    SOURCE_ID_PATTERN,
    TRANSACTION_ID_PATTERN,
    PlanBundle,
    normalize_maintenance_authorization,
    normalized_summary,
    source_review_evidence,
)
from .plan_bundle import (
    content_file as _content_file,
    freeze_plan_bundle as _freeze_plan_bundle,
    read_json as _read_json,
)


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
    authorization_summary: str,
    *,
    source_ids: list[str] | None = None,
    request_ref: str | None = None,
    request_sha256: str | None = None,
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
    authorization: dict[str, Any] = {
        "kind": "maintenance_request",
        "summary": authorization_summary,
    }
    if (request_ref is None) != (request_sha256 is None):
        raise PlanError(
            "authorization request_ref and request_sha256 must be provided together"
        )
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
        "schema_version": PLAN_SCHEMA_VERSION,
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


def _bounded_excerpt(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PlanError("evidence is not readable UTF-8 text") from exc
    return text[:MAX_EVIDENCE_EXCERPT_CHARACTERS]


def _review_retrieval_context(
    config: dict[str, Any], source_id: str
) -> dict[str, Any]:
    """Bind one live retrieval result into the private semantic review packet."""
    try:
        result = retrieve_related_notes(config, source_id)
    except RetrievalError as exc:
        raise PlanError(f"required retrieval failed: {exc}") from exc
    if result.get("source_id") != source_id:
        raise PlanError("required retrieval returned the wrong source id")
    raw_candidates = result.get("candidates")
    if not isinstance(raw_candidates, list):
        raise PlanError("required retrieval returned invalid candidates")
    candidates: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_candidates):
        if not isinstance(raw, dict):
            raise PlanError(f"required retrieval candidate {index} is invalid")
        path = raw.get("path")
        score = raw.get("score")
        score_kind = raw.get("score_kind")
        if (
            not isinstance(path, str)
            or not path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not isinstance(score_kind, str)
            or not score_kind
        ):
            raise PlanError(f"required retrieval candidate {index} is invalid")
        title = raw.get("title")
        snippet = raw.get("snippet")
        if title is not None and not isinstance(title, str):
            raise PlanError(f"required retrieval candidate {index} title is invalid")
        if snippet is not None and not isinstance(snippet, str):
            raise PlanError(
                f"required retrieval candidate {index} snippet is invalid"
            )
        candidates.append(
            {
                "evidence_id": f"retrieval_candidate:{source_id}:{path}",
                "path": path,
                "title": title,
                "snippet": (snippet or "")[:MAX_RETRIEVAL_SNIPPET_CHARACTERS],
                "score": float(score),
                "score_kind": score_kind,
            }
        )
    query = result.get("query")
    if not isinstance(query, str) or not query:
        raise PlanError("required retrieval returned an invalid query")
    return {
        "evidence_id": f"retrieval:{source_id}",
        "source_id": source_id,
        "query": query,
        "collection": result.get("collection"),
        "retrieval_mode": result.get("retrieval_mode"),
        "fusion_strategy": result.get("fusion_strategy"),
        "query_types": result.get("query_types"),
        "chunk_strategy": result.get("chunk_strategy"),
        "rerank": result.get("rerank"),
        "candidates": candidates,
    }


def _review_visible_changes(
    vault: Path,
    normalized: list[dict[str, Any]],
    content_snapshots: dict[Path, bytes],
) -> list[dict[str, Any]]:
    """Describe exact visible-note changes without copying Capture binaries."""
    changes: list[dict[str, Any]] = []
    for operation in normalized:
        action = operation["action"]
        if operation.get("capture_artifact") or action in {"create", "promote_asset"}:
            continue
        source_path = operation.get("from", operation["path"])
        try:
            before = source_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PlanError("visible note preimage is not readable UTF-8 text") from exc
        content_file = operation.get("content_file")
        if action == "rename" and content_file is None:
            after = before
        elif content_file is None:
            after = ""
        else:
            try:
                after = content_snapshots[content_file].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PlanError("proposed Markdown content must be UTF-8") from exc
        old_path = str(source_path.relative_to(vault))
        new_path = str(operation["path"].relative_to(vault))
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{old_path}",
                tofile=f"b/{new_path}" if after else "/dev/null",
            )
        )
        changes.append(
            {
                "evidence_id": f"change:{old_path}",
                "action": action,
                "path": new_path,
                **({"from": old_path} if action == "rename" else {}),
                "preimage_sha256": _sha256_bytes(before.encode("utf-8")),
                "proposed_sha256": (
                    _sha256_bytes(after.encode("utf-8")) if after else None
                ),
                "unified_diff": diff,
            }
        )
    return changes


def _review_purge_evidence(
    config: dict[str, Any], vault: Path, normalized: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Bind each purge target without exposing raw Capture content."""
    results: list[dict[str, Any]] = []
    source_ids = sorted(
        {
            operation["purge_source_id"]
            for operation in normalized
            if operation.get("purge_source_id")
        }
    )
    for source_id in source_ids:
        capture = source_archive_dir(config, vault) / source_id
        try:
            manifest = load_validated_capture(
                capture,
                source_id=source_id,
                vault=vault,
            )
            source_note = resolve_source_note(config, vault, source_id)
        except (CaptureError, IngestError) as exc:
            raise PlanError(f"purge_capture source is invalid: {exc}") from exc
        extracted_record = manifest["derived"]["extracted"]
        extracted_path = capture / str(extracted_record["path"])
        primary = manifest["primary"]
        results.append(
            {
                "evidence_id": f"purge_capture:{source_id}",
                "source_id": source_id,
                "title": str(manifest.get("title", "")),
                "manifest_sha256": _sha256_file(capture / "manifest.json"),
                "primary": {
                    "path": str(primary["path"]),
                    "sha256": str(primary["sha256"]),
                    "size": int(primary["size"]),
                    "mime": str(primary["mime"]),
                },
                "extracted": {
                    "path": str(extracted_record["path"]),
                    "sha256": str(extracted_record["sha256"]),
                    "size": int(extracted_record["size"]),
                    "mime": str(extracted_record["mime"]),
                    "excerpt": _bounded_excerpt(extracted_path),
                },
                "source_note": str(source_note.relative_to(vault)),
                "source_note_sha256": _sha256_file(source_note),
                "source_note_excerpt": _bounded_excerpt(source_note),
            }
        )
    return results


def _review_prior_rejections(
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Carry bounded prior rejection reasons into a revised immutable packet."""
    results: list[dict[str, Any]] = []
    for entry in history:
        assessment = entry["assessment"]
        if assessment["status"] != "reject":
            continue
        transaction_sha256 = str(entry["transaction_sha256"])
        failed_judgments = {
            name: {
                "reason": value["reason"],
                "evidence_refs": value["evidence_refs"],
            }
            for name, value in assessment["judgments"].items()
            if value["status"] == "fail"
        }
        results.append(
            {
                "evidence_id": f"prior_rejection:{transaction_sha256}",
                "transaction_sha256": transaction_sha256,
                "review_packet_sha256": str(entry["review_packet_sha256"]),
                "instance_id": str(assessment["instance_id"]),
                "failed_judgments": failed_judgments,
                "reasons": assessment["reasons"],
                "unassessed": assessment["unassessed"],
            }
        )
    return results


def _normalize_source_evidence(
    config: dict[str, Any],
    vault: Path,
    raw: dict[str, Any],
    source_manifests: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if set(raw) not in (
        {"kind", "source_id"},
        {"kind", "source_id", "focus_ranges"},
    ):
        raise PlanError("source_capture evidence has unknown or missing fields")
    source_id = raw.get("source_id")
    if not isinstance(source_id, str) or not SOURCE_ID_PATTERN.fullmatch(source_id):
        raise PlanError("source_capture evidence has an invalid source_id")
    capture = source_archive_dir(config, vault) / source_id
    try:
        manifest, extracted_text = load_validated_capture_artifact_text(
            capture,
            source_id=source_id,
            vault=vault,
            artifact="extracted",
        )
    except CaptureError as exc:
        raise PlanError(f"source capture integrity failed: {exc}") from exc
    source_manifests[source_id] = manifest
    manifest_path = capture / "manifest.json"
    derived = manifest.get("derived")
    if not isinstance(derived, dict) or not isinstance(
        derived.get("extracted"), dict
    ):
        raise PlanError("source capture manifest is missing derived.extracted")
    review_evidence = source_review_evidence(
        extracted_text,
        raw.get("focus_ranges", FOCUS_RANGES_ABSENT),
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
        **review_evidence,
    }


def _normalize_evidence(
    config: dict[str, Any],
    vault: Path,
    raw_evidence: object,
    source_manifests: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(raw_evidence, list):
        raise PlanError("plan evidence must be an array")
    normalized: list[dict[str, Any]] = []
    identities: set[str] = set()
    for index, raw in enumerate(raw_evidence):
        if not isinstance(raw, dict):
            raise PlanError(f"evidence {index} has an unsupported kind")
        kind = raw.get("kind")
        if kind != "source_capture":
            raise PlanError(f"evidence {index} has an unsupported kind")
        item = _normalize_source_evidence(
            config,
            vault,
            raw,
            source_manifests,
        )
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
    try:
        assessment_bytes = assessment_path.read_bytes()
    except OSError as exc:
        raise PlanError("unable to read semantic review assessment") from exc
    valid = _assessment_status_snapshot(
        assessment_bytes,
        transaction_sha256,
        packet,
        history,
    )
    return valid, str(assessment_path)


def _assessment_status_snapshot(
    assessment_bytes: bytes | None,
    transaction_sha256: str,
    packet: dict[str, Any],
    history: list[dict[str, Any]],
) -> bool:
    if assessment_bytes is None or not history:
        return False
    try:
        assessment = json.loads(assessment_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError("invalid semantic review assessment") from exc
    if not isinstance(assessment, dict):
        raise PlanError("semantic review assessment must be a JSON object")
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
        return False
    return bool(
        last.get("transaction_sha256") == transaction_sha256
        and last.get("review_packet_sha256") == packet_sha256(packet)
        and last.get("assessment") == assessment
        and assessment.get("status") == "approve"
    )


def validate_plan(
    config: dict[str, Any],
    plan_path: Path,
    *,
    bundle: PlanBundle | None = None,
) -> dict[str, Any]:
    """Validate paths, hashes, note count, risk, and reviewer approval."""
    vault = load_vault_path(config)
    plan_path = plan_path.expanduser().resolve()
    if bundle is None:
        plan = _read_json(plan_path, "plan")
        bundle_contents: dict[Path, bytes] | None = None
    else:
        if bundle.plan_path != plan_path:
            raise PlanError("plan bundle path does not match validation path")
        try:
            plan = json.loads(bundle.plan_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlanError("invalid plan") from exc
        if not isinstance(plan, dict):
            raise PlanError("plan must be a JSON object")
        bundle_contents = dict(bundle.content_snapshots)
    plan_schema_version = plan.get("schema_version")
    if plan_schema_version != PLAN_SCHEMA_VERSION:
        raise PlanError("unsupported plan schema_version")
    allowed_plan_fields = {
        "schema_version",
        "transaction_id",
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
    summary = normalized_summary(plan.get("summary"), "plan summary")
    authorization = normalize_maintenance_authorization(plan.get("authorization"))
    source_manifests: dict[str, dict[str, Any]] = {}
    evidence = _normalize_evidence(
        config,
        vault,
        plan.get("evidence"),
        source_manifests,
    )
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
        reason = normalized_summary(
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
    if intent == "integrate" and not source_evidence:
        raise PlanError("integration plan requires source_capture evidence")
    operations = plan.get("operations")
    if not isinstance(operations, list) or not operations:
        raise PlanError("plan operations must be a non-empty array")

    touched: set[Path] = set()
    affected_paths: set[Path] = set()
    normalized: list[dict[str, Any]] = []
    actions: set[str] = set()
    source_evidence_ids = {item["source_id"] for item in source_evidence}
    asset_dir: Path | None = None
    managed_assets: dict[str, list[Path]] = {}
    for index, raw_operation in enumerate(operations):
        if not isinstance(raw_operation, dict):
            raise PlanError(f"operation {index} must be an object")
        action = raw_operation.get("action")
        if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
            raise PlanError(f"operation {index} has unsupported action: {action}")
        if action == "promote_asset":
            if asset_dir is None:
                try:
                    asset_dir, managed_assets = load_managed_asset_index(
                        config,
                        vault,
                    )
                except AssetPromotionError as exc:
                    raise PlanError(str(exc)) from exc
            try:
                promotion = normalize_asset_promotion(
                    vault,
                    raw_operation,
                    index,
                    source_evidence_ids,
                    source_manifests,
                    asset_dir,
                    managed_assets,
                )
            except AssetPromotionError as exc:
                raise PlanError(str(exc)) from exc
            if promotion["path"] in affected_paths:
                raise PlanError(
                    f"operation {index} duplicates an affected asset path"
                )
            affected_paths.add(promotion["path"])
            actions.add(action)
            normalized.append(promotion)
            continue
        if action == "purge_capture":
            if set(raw_operation) != {"action", "source_id"}:
                raise PlanError(
                    f"operation {index} purge_capture has unknown fields"
                )
            if authorization["kind"] != "maintenance_request":
                raise PlanError(
                    "maintenance_request_required: purge_capture requires an "
                    "explicit maintenance request",
                    code="maintenance_request_required",
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
            affected_paths.add(source_note)
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
                if path in affected_paths:
                    raise PlanError(
                        f"operation {index} duplicates an affected capture path"
                    )
                affected_paths.add(path)
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
        affected_paths.add(target)
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
            affected_paths.add(source)
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
            content_path = _content_file(plan_path, raw_operation, bundle_contents)
            content = (
                bundle_contents[content_path]
                if bundle_contents is not None
                else content_path.read_bytes()
            )
            if not _frontmatter_ok(content):
                raise PlanError(f"operation {index} content has invalid frontmatter")
            operation["content_file"] = content_path
        normalized.append(operation)

    promotions = [
        operation
        for operation in normalized
        if operation["action"] == "promote_asset"
    ]
    try:
        validate_asset_promotion_batch(
            config,
            vault,
            promotions,
        )
    except AssetPromotionError as exc:
        raise PlanError(str(exc)) from exc

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
                content_path = source_updates[0]["content_file"]
                content = (
                    bundle_contents[content_path]
                    if bundle_contents is not None
                    else content_path.read_bytes()
                )
                preserves_source_id = source_note_bytes_have_id(
                    content, source["source_id"]
                )
            except IngestError as exc:
                raise PlanError(f"proposed source note is invalid: {exc}") from exc
            if not preserves_source_id:
                raise PlanError(
                    "proposed source note must preserve source_id "
                    f"{source['source_id']}"
                )

    maximum = max_touched_notes(config)
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
                content_path = operation["content_file"]
                proposed_by_path[operation["path"]] = (
                    bundle_contents[content_path]
                    if bundle_contents is not None
                    else content_path.read_bytes()
                )
            elif operation["action"] == "rename":
                proposed_by_path[operation["from"]] = None
                proposed_by_path[operation["path"]] = (
                    (
                        bundle_contents[operation["content_file"]]
                        if bundle_contents is not None
                        else operation["content_file"].read_bytes()
                    )
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
            bundle_contents,
        )
    except ObsidianValidationError as exc:
        raise PlanError(str(exc)) from exc
    if promotions:
        modified_note_paths = {
            str(operation["path"].relative_to(vault))
            for operation in normalized
            if operation["action"] in {"create", "update", "rename"}
            and operation["path"].suffix.casefold() == ".md"
        }
        try:
            validate_promotion_references(
                vault,
                promotions,
                modified_note_paths,
                obsidian_validation["resolved_assets_by_path"],
            )
        except AssetPromotionError as exc:
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
    if len(touched) > 15:
        risks.append("large_transaction")
    if policy.review_all_semantic_transactions and not risks:
        risks.append("vault_policy")
    history = (
        _review_history_snapshot(bundle.review_history_bytes)
        if bundle is not None
        else _review_history(plan_path.parent / "review-history.json")
    )
    _, rejection_count = _validate_review_history(history)
    if history and not risks:
        risks.append("review_history")
    risks = list(dict.fromkeys(risks))
    content_paths = [
        operation["content_file"]
        for operation in normalized
        if operation.get("content_file")
    ]
    plan_bytes = bundle.plan_bytes if bundle is not None else plan_path.read_bytes()
    content_snapshots = {
        path: (
            bundle_contents[path]
            if bundle_contents is not None
            else path.read_bytes()
        )
        for path in content_paths
    }
    retrieval_contexts = (
        [
            _review_retrieval_context(config, item["source_id"])
            for item in source_evidence
        ]
        if retrieval["required"]
        else []
    )
    visible_changes = _review_visible_changes(
        vault,
        normalized,
        content_snapshots,
    )
    purge_evidence = _review_purge_evidence(config, vault, normalized)
    prior_rejections = _review_prior_rejections(history)
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
    proposed_assets = promotion_review_evidence(vault, promotions)
    evidence_ids = [
        "plan",
        "authorization",
        *[item["evidence_id"] for item in evidence],
        *[item["evidence_id"] for item in retrieval_contexts],
        *[
            candidate["evidence_id"]
            for item in retrieval_contexts
            for candidate in item["candidates"]
        ],
        *[item["evidence_id"] for item in visible_changes],
        *[item["evidence_id"] for item in purge_evidence],
        *[item["evidence_id"] for item in prior_rejections],
        *[item["evidence_id"] for item in proposed_contents],
        *[item["evidence_id"] for item in proposed_assets],
    ]
    packet_payload = {
        "governance_sha256": governance_sha256,
        "plan": plan,
        "authorization": authorization,
        "evidence": evidence,
        "retrieval": retrieval,
        "retrieval_contexts": retrieval_contexts,
        "index_freshness": index_freshness,
        "visible_changes": visible_changes,
        "purge_captures": purge_evidence,
        "prior_rejections": prior_rejections,
        "proposed_contents": proposed_contents,
        "obsidian_validation": obsidian_validation,
        "risks": risks,
        "required_judgments": list(REQUIRED_JUDGMENTS),
        "evidence_ids": evidence_ids,
        "unassessed": [],
    }
    packet_payload["proposed_assets"] = proposed_assets
    try:
        review_packet = build_review_packet(packet_payload)
    except ReviewError as exc:
        raise PlanError(str(exc)) from exc
    transaction_sha256 = _transaction_digest(governance_sha256, review_packet)
    revision_limit_exhausted = rejection_count > policy.max_revision_rounds
    if bundle is None:
        approval_valid, approval_path = _assessment_status(
            plan_path.parent,
            transaction_sha256,
            review_packet,
            history,
        )
    else:
        approval_valid = _assessment_status_snapshot(
            bundle.assessment_bytes,
            transaction_sha256,
            review_packet,
            history,
        )
        approval_path = (
            str(plan_path.parent / "assessment.json")
            if bundle.assessment_bytes is not None
            else None
        )
    return {
        "status": "valid",
        "transaction_id": transaction_id,
        "summary": summary,
        "authorization": authorization,
        "evidence": evidence,
        "retrieval": retrieval,
        "index_freshness": index_freshness,
        "source_ids": [item["source_id"] for item in source_evidence],
        "plan_sha256": _sha256_bytes(plan_bytes),
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
        "plan_schema_version": plan_schema_version,
        "plan": plan,
        "plan_path": plan_path,
    }


def _review_history_snapshot(content: bytes | None) -> list[dict[str, Any]]:
    if content is None:
        return []
    try:
        history = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError("invalid review history") from exc
    if not isinstance(history, list) or not all(
        isinstance(entry, dict) for entry in history
    ):
        raise PlanError("review history must be an array")
    return history


def _review_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise PlanError("unable to read review history") from exc
    return _review_history_snapshot(content)


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
        schema_version = entry.get("schema_version")
        expected_fields = {
            "schema_version",
            "plan_sha256",
            "governance_sha256",
            "transaction_sha256",
            "review_packet_sha256",
            "assessment",
            "review_policy",
            "reviewed_at",
        }
        if (
            set(entry) != expected_fields
            or schema_version != PLAN_SCHEMA_VERSION
            or not isinstance(digest, str)
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
            validate_digest(
                entry.get("governance_sha256"),
                f"review history entry {index} governance",
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
    vault = load_vault_path(config)
    plan_path = plan_path.expanduser().resolve()
    assessment_path = assessment_path.expanduser().resolve()
    with vault_write_lock(vault):
        bundle = _freeze_plan_bundle(config, plan_path)
        validation = validate_plan(config, plan_path, bundle=bundle)
        if _freeze_plan_bundle(config, plan_path).request_sha256 != bundle.request_sha256:
            raise PlanError("plan bundle changed during review validation")
        assessment = _read_json(assessment_path, "semantic review assessment")
        transaction_sha256 = validation["transaction_sha256"]
        policy = review_policy(config)
        history_path = plan_path.parent / "review-history.json"
        history = _review_history_snapshot(bundle.review_history_bytes)
        rejected_digests, rejection_count = _validate_review_history(history)
        if rejection_count > policy.max_revision_rounds:
            raise PlanError(
                "review revision limit exhausted; preserve the unapplied plan for inspection",
                code="review_revision_exhausted",
            )
        rejected_governance = {
            entry["governance_sha256"]
            for entry in history
            if entry["assessment"]["status"] == "reject"
        }
        if (
            transaction_sha256 in rejected_digests
            or validation["governance_sha256"] in rejected_governance
        ):
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
            "schema_version": PLAN_SCHEMA_VERSION,
            "plan_sha256": validation["plan_sha256"],
            "governance_sha256": validation["governance_sha256"],
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
        stored_assessment_path = plan_path.parent / "assessment.json"
        _atomic_write(
            stored_assessment_path,
            (json.dumps(assessment, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )
        try:
            _atomic_write(
                history_path,
                (json.dumps(history, ensure_ascii=False, indent=2) + "\n").encode(
                    "utf-8"
                ),
            )
        except Exception as exc:
            try:
                if bundle.assessment_bytes is None:
                    stored_assessment_path.unlink(missing_ok=True)
                else:
                    _atomic_write(stored_assessment_path, bundle.assessment_bytes)
            except Exception as rollback_exc:
                raise PlanError(
                    "unable to record review history; assessment rollback also failed"
                ) from rollback_exc
            raise PlanError(
                "unable to record review history; assessment was rolled back"
            ) from exc
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


def apply_plan(config: dict[str, Any], plan_path: Path) -> dict[str, Any]:
    """Apply a validated plan atomically and archive its audit record."""
    vault = load_vault_path(config)
    git_dir = _git_dir(vault)
    with vault_write_lock(vault):
        bundle = _freeze_plan_bundle(config, plan_path)
        try:
            existing = inspect_journaled_mutation(
                vault=vault,
                git_dir=git_dir,
                operation="semantic_plan",
                transaction_id=bundle.transaction_id,
                transaction_sha256=None,
                request_sha256=bundle.request_sha256,
                lock=False,
            )
        except MutationError as exc:
            raise PlanError(str(exc)) from exc
        if existing is not None:
            return mutation_response(existing, applied_status="applied")
        require_clean_git_root(vault)
        validation = validate_plan(config, plan_path, bundle=bundle)
        verified_bundle = _freeze_plan_bundle(config, plan_path)
        if verified_bundle.request_sha256 != bundle.request_sha256:
            raise PlanError("plan bundle changed during validation")
        if validation["transaction_id"] != bundle.transaction_id:
            raise PlanError("plan bundle transaction changed during validation")
        frozen_plan_path = validation["plan_path"]
        plan_bytes = bundle.plan_bytes
        content_snapshots = dict(bundle.content_snapshots)
        promotions = [
            operation
            for operation in validation["operations"]
            if operation["action"] == "promote_asset"
        ]
        try:
            asset_snapshots = load_promotion_snapshots(
                config,
                vault,
                promotions,
            )
        except AssetPromotionError as exc:
            raise PlanError(str(exc)) from exc
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
        assessment_bytes = bundle.assessment_bytes
        review_history_bytes = bundle.review_history_bytes
        frozen_history = _review_history_snapshot(bundle.review_history_bytes)
        _, rejection_count = _validate_review_history(frozen_history)
        if rejection_count > review_policy(config).max_revision_rounds:
            raise PlanError(
                "review revision limit exceeded under the configured vault policy",
                code="review_revision_exhausted",
            )
        approval_valid = _assessment_status_snapshot(
            bundle.assessment_bytes,
            transaction_sha256,
            validation["semantic_review_packet"],
            frozen_history,
        )
        if validation["approval_required"] and not approval_valid:
            raise PlanError(
                "a hash-bound reviewer approval record is required for: "
                + ", ".join(validation["risks"]),
                code="review_required",
            )

        operations = validation["operations"]
        validated_original_mode: dict[Path, str | None] = {}
        for operation in operations:
            action = operation["action"]
            target = operation["path"]
            try:
                if action == "promote_asset":
                    if operation["disposition"] == "create":
                        unchanged = not target.exists()
                        validated_original_mode[target] = None
                    else:
                        unchanged = (
                            target.is_file()
                            and not target.is_symlink()
                            and _sha256_file(target) == operation["sha256"]
                        )
                        validated_original_mode[target] = worktree_file_mode(
                            target
                        )
                elif action == "create":
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
            if (
                operation["action"] == "promote_asset"
                and operation["disposition"] == "reuse"
            ):
                affected.add(target)
                expected_original_sha256[target] = operation["sha256"]
                expected_original_mode[target] = validated_original_mode[target]
                continue
            affected.add(target)
            if operation["action"] == "rename":
                source = operation["from"]
                affected.add(source)
                expected_original_sha256[source] = operation["expected_sha256"]
                expected_original_sha256[target] = None
                expected_original_mode[source] = validated_original_mode[source]
                expected_original_mode[target] = None
            elif operation["action"] in {"create", "promote_asset"}:
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
            elif action == "promote_asset":
                if operation["disposition"] == "create":
                    final_sha256[target] = operation["sha256"]
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
            "schema_version": validation["plan_schema_version"],
            "transaction_id": validation["transaction_id"],
            "source_ids": validation["source_ids"],
            "authorization": validation["authorization"],
            "retrieval": validation["retrieval"],
            "index_freshness": validation["index_freshness"],
            "evidence": [
                {
                    key: value
                    for key, value in item.items()
                    if key not in {"excerpt", "focus_excerpts"}
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
        result["asset_promotions"] = promotion_results(
            vault,
            promotions,
        )
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

        def required_final_mode(path: Path) -> str:
            mode = final_mode[path]
            if mode is None:
                raise PlanError("write target has no final Git mode")
            return mode

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
                        required_final_mode(target),
                    )
                    write_index += 1
                elif action == "promote_asset":
                    if operation["disposition"] == "create":
                        install_created_promotion(
                            vault,
                            target,
                            asset_snapshots[target],
                            required_final_mode(target),
                        )
                elif action == "delete":
                    target.unlink()
                elif action == "rename":
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if operation.get("content_file"):
                        _install_staged(
                            target,
                            content_snapshots[operation["content_file"]],
                            active_dir / "write-staging" / str(write_index),
                            required_final_mode(target),
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
                    required_final_mode(archive_path),
                )
                write_index += 1

        def postcondition(_active_dir: Path) -> None:
            for operation in promotions:
                if operation["disposition"] != "reuse":
                    continue
                target = operation["path"]
                if (
                    target.is_symlink()
                    or not target.is_file()
                    or _sha256_file(target) != operation["sha256"]
                    or worktree_file_mode(target)
                    != validated_original_mode[target]
                ):
                    raise PlanError(
                        "reused asset changed during transaction: "
                        + str(target.relative_to(vault))
                    )

        try:
            outcome = execute_journaled_mutation(
                vault=vault,
                git_dir=git_dir,
                transaction_id=validation["transaction_id"],
                transaction_sha256=validation["transaction_sha256"],
                request_sha256=bundle.request_sha256,
                operation="semantic_plan",
                receipt_data=result,
                affected=affected,
                expected_original_sha256=expected_original_sha256,
                expected_original_mode=expected_original_mode,
                final_sha256=final_sha256,
                final_mode=final_mode,
                write=write,
                postcondition=postcondition,
                completion_path=str((archive / "result.json").relative_to(vault)),
                lock=False,
            )
        except MutationError as exc:
            raise PlanError(str(exc)) from exc
        return mutation_response(outcome, applied_status="applied")
