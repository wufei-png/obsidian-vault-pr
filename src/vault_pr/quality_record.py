"""Git-tracked deterministic evidence for eligible autonomous repairs."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .config import load_vault_path
from .git import GitSafetyError, run_git
from .mutation import (
    MutationError,
    execute_journaled_mutation,
    inspect_journaled_mutation,
    mutation_response,
)
from .quality import (
    AUTONOMOUS_RULE_CAPABILITIES,
    GRAPH_AUDIT_SCHEMA_VERSION,
    GRAPH_RULESET_VERSION,
    audit_graph,
)


QUALITY_RECORD_ID = re.compile(r"quality_[0-9a-f]{20}")


class QualityRecordError(RuntimeError):
    """Raised when autonomous-repair evidence cannot be trusted."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _read_json(path: Path, label: str, maximum: int = 8_000_000) -> dict[str, Any]:
    path = path.expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise QualityRecordError(f"{label} is missing, unsafe, or too large")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualityRecordError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise QualityRecordError(f"{label} must be a JSON object")
    return value


def _git_dir(vault: Path) -> Path:
    result = run_git(vault, "rev-parse", "--absolute-git-dir")
    if result.returncode != 0:
        raise QualityRecordError("unable to resolve vault Git directory")
    return Path(result.stdout.strip())


def _record_core(
    audit: dict[str, Any],
    selected: list[dict[str, Any]],
) -> dict[str, Any]:
    audit_sha256 = audit.get("audit_sha256")
    if not isinstance(audit_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", audit_sha256
    ):
        raise QualityRecordError("graph audit has no valid audit digest")
    return {
        "assessment": {
            "kind": "graph_audit",
            "input_sha256": audit_sha256,
            "git_commit": audit.get("git_commit"),
            "rules_sha256": audit.get("rules_sha256"),
            "relevant_config_sha256": audit.get("config_sha256"),
            "graph_ruleset_version": GRAPH_RULESET_VERSION,
        },
        "findings": selected,
    }


def record_quality_findings(
    config: dict[str, Any],
    *,
    audit_path: Path,
    finding_ids: list[str],
    apply: bool = False,
) -> dict[str, Any]:
    """Select eligible deterministic findings into one Quality Record."""
    if not finding_ids or len(finding_ids) != len(set(finding_ids)):
        raise QualityRecordError("finding_ids must be a non-empty unique list")
    vault = load_vault_path(config)
    audit = _read_json(audit_path, "graph audit")
    by_id = {
        item.get("finding_id"): item
        for item in audit.get("findings", [])
        if isinstance(item, dict) and isinstance(item.get("finding_id"), str)
    }
    missing = sorted(set(finding_ids) - set(by_id))
    if missing:
        raise QualityRecordError("unknown finding ids: " + ", ".join(missing))
    selected = [by_id[finding_id] for finding_id in sorted(finding_ids)]
    for finding in selected:
        expected = AUTONOMOUS_RULE_CAPABILITIES.get(str(finding.get("rule_id")))
        if finding.get("autonomous_capability") != expected or expected is None:
            raise QualityRecordError(
                "selected finding is not eligible for autonomous repair"
            )
    core = _record_core(audit, selected)
    digest = hashlib.sha256(_canonical(core)).hexdigest()
    record_id = f"quality_{digest[:20]}"
    record = {
        "schema_version": 1,
        "record_type": "quality_record",
        "record_id": record_id,
        **core,
    }
    content = (
        json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    relative = Path(".vault-pr/quality/records") / f"{record_id}.json"
    destination = vault / relative
    planned = {
        "record_id": record_id,
        "path": relative.as_posix(),
        "record_sha256": hashlib.sha256(content).hexdigest(),
        "finding_ids": sorted(finding_ids),
    }
    if not apply:
        return {
            **planned,
            "status": "quality_record_ready",
            "requires_commit": False,
        }
    transaction_id = f"txn_quality_{digest[:16]}"
    try:
        existing = inspect_journaled_mutation(
            vault=vault,
            git_dir=_git_dir(vault),
            operation="quality_record",
            transaction_id=transaction_id,
            transaction_sha256=digest,
        )
    except MutationError as exc:
        raise QualityRecordError(str(exc)) from exc
    if existing is not None:
        if existing["status"] == "already_committed" and (
            not destination.is_file() or destination.read_bytes() != content
        ):
            raise QualityRecordError(
                "committed quality record integrity validation failed"
            )
        return mutation_response(existing, applied_status="quality_record_applied")

    current = audit_graph(config)
    if (
        audit.get("schema_version") != GRAPH_AUDIT_SCHEMA_VERSION
        or audit.get("audit") != "graph"
        or audit.get("git_clean") is not True
        or audit.get("git_commit") != current.get("git_commit")
        or audit.get("config_sha256") != current.get("config_sha256")
        or audit.get("rules_sha256") != current.get("rules_sha256")
        or audit.get("audit_sha256") != current.get("audit_sha256")
        or audit.get("findings") != current.get("findings")
    ):
        raise QualityRecordError("graph audit is stale or does not match the vault")
    if destination.exists() or destination.is_symlink():
        raise QualityRecordError("quality record path already exists with other content")

    def write(active_dir: Path) -> None:
        staging = active_dir / "write-staging" / "quality-record.json"
        staging.parent.mkdir(parents=True)
        staging.write_bytes(content)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, destination)

    try:
        outcome = execute_journaled_mutation(
            vault=vault,
            git_dir=_git_dir(vault),
            transaction_id=transaction_id,
            transaction_sha256=digest,
            operation="quality_record",
            receipt_data=planned,
            affected={destination},
            expected_original_sha256={destination: None},
            expected_original_mode={destination: None},
            final_sha256={destination: planned["record_sha256"]},
            final_mode={destination: "100644"},
            write=write,
            completion_path=relative.as_posix(),
        )
    except (GitSafetyError, MutationError) as exc:
        raise QualityRecordError(str(exc)) from exc
    return mutation_response(outcome, applied_status="quality_record_applied")


def load_quality_record(vault: Path, record_id: str) -> tuple[dict[str, Any], Path]:
    """Load and verify a Quality Record's content-derived identity."""
    if not QUALITY_RECORD_ID.fullmatch(record_id):
        raise QualityRecordError("invalid quality record id")
    path = vault / ".vault-pr" / "quality" / "records" / f"{record_id}.json"
    record = _read_json(path, "quality record", 2_000_000)
    if (
        set(record)
        != {
            "schema_version",
            "record_type",
            "record_id",
            "assessment",
            "findings",
        }
        or record.get("schema_version") != 1
        or record.get("record_type") != "quality_record"
        or record.get("record_id") != record_id
    ):
        raise QualityRecordError("quality record schema is invalid")
    core = {
        "assessment": record.get("assessment"),
        "findings": record.get("findings"),
    }
    digest = hashlib.sha256(_canonical(core)).hexdigest()
    if record_id != f"quality_{digest[:20]}":
        raise QualityRecordError("quality record identity does not match its content")
    return record, path


def revalidate_quality_record(
    config: dict[str, Any],
    record: dict[str, Any],
    finding_ids: list[str],
) -> list[dict[str, Any]]:
    """Verify selected findings remain current under the same rules and config."""
    current = audit_graph(config)
    assessment = record.get("assessment")
    if not isinstance(assessment, dict):
        raise QualityRecordError("quality record assessment metadata is invalid")
    if (
        assessment.get("rules_sha256") != current.get("rules_sha256")
        or assessment.get("relevant_config_sha256") != current.get("config_sha256")
    ):
        raise QualityRecordError("quality_record_refresh_required")
    current_by_id = {
        item.get("finding_id"): item
        for item in current.get("findings", [])
        if isinstance(item, dict)
    }
    recorded_by_id = {
        item.get("finding_id"): item
        for item in record.get("findings", [])
        if isinstance(item, dict)
    }
    selected: list[dict[str, Any]] = []
    for finding_id in finding_ids:
        recorded = recorded_by_id.get(finding_id)
        current_finding = current_by_id.get(finding_id)
        if recorded is None:
            raise QualityRecordError("quality record does not contain selected finding")
        if current_finding is None:
            raise QualityRecordError("quality_finding_already_satisfied")
        if current_finding != recorded:
            raise QualityRecordError("quality_record_refresh_required")
        selected.append(recorded)
    return selected
