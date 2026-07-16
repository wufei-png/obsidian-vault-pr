"""Structured semantic review packet and assessment contracts."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


REVIEW_PACKET_SCHEMA_VERSION = 2
REVIEW_ASSESSMENT_SCHEMA_VERSION = 2
MAX_REVIEW_PACKET_BYTES = 8 * 1024 * 1024
REQUIRED_JUDGMENTS = (
    "authorization_alignment",
    "semantic_faithfulness",
    "provenance_sufficiency",
    "conflict_preservation",
    "link_and_structure_quality",
    "scope_and_destructive_safety",
)
_HEX64 = re.compile(r"[0-9a-f]{64}")


class ReviewError(ValueError):
    """Raised when a semantic review packet or assessment is invalid."""


def canonical_json(value: Any) -> bytes:
    """Serialize one governance value deterministically."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_review_packet(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a bounded immutable semantic review input packet."""
    input_sha256 = hashlib.sha256(canonical_json(payload)).hexdigest()
    packet = {
        "schema_version": REVIEW_PACKET_SCHEMA_VERSION,
        "packet_type": "semantic_review_input",
        "packet_id": f"pkt_{input_sha256[:20]}",
        "input_sha256": input_sha256,
        "payload": payload,
    }
    if len(canonical_json(packet)) > MAX_REVIEW_PACKET_BYTES:
        raise ReviewError("semantic review packet exceeds the 8 MiB hard limit")
    return packet


def packet_sha256(packet: dict[str, Any]) -> str:
    """Hash an exact review packet."""
    return hashlib.sha256(canonical_json(packet)).hexdigest()


def validate_review_packet(packet: dict[str, Any]) -> None:
    """Verify a semantic review input packet without trusting stored hashes."""
    expected = {
        "schema_version",
        "packet_type",
        "packet_id",
        "input_sha256",
        "payload",
    }
    if set(packet) != expected:
        raise ReviewError("semantic review packet has unknown or missing fields")
    if (
        packet.get("schema_version") != REVIEW_PACKET_SCHEMA_VERSION
        or packet.get("packet_type") != "semantic_review_input"
        or not isinstance(packet.get("payload"), dict)
    ):
        raise ReviewError("semantic review packet schema is unsupported")
    digest = hashlib.sha256(canonical_json(packet["payload"])).hexdigest()
    if packet.get("input_sha256") != digest:
        raise ReviewError("semantic review packet input hash does not match")
    if packet.get("packet_id") != f"pkt_{digest[:20]}":
        raise ReviewError("semantic review packet id does not match")
    if len(canonical_json(packet)) > MAX_REVIEW_PACKET_BYTES:
        raise ReviewError("semantic review packet exceeds the 8 MiB hard limit")


def _validate_evidence_refs(
    refs: object,
    evidence_ids: set[str],
    field: str,
) -> list[str]:
    if not isinstance(refs, list) or not all(
        isinstance(value, str) and value for value in refs
    ):
        raise ReviewError(f"{field} must be an array of evidence ids")
    if len(refs) != len(set(refs)):
        raise ReviewError(f"{field} contains duplicate evidence ids")
    unknown = sorted(set(refs) - evidence_ids)
    if unknown:
        raise ReviewError(f"{field} references unknown evidence: {', '.join(unknown)}")
    return refs


def _validate_judgment(
    name: str,
    judgment: object,
    evidence_ids: set[str],
) -> dict[str, Any]:
    if not isinstance(judgment, dict):
        raise ReviewError(f"judgment {name} must be an object")
    if set(judgment) != {"status", "reason", "evidence_refs"}:
        raise ReviewError(f"judgment {name} has unknown or missing fields")
    if not isinstance(judgment.get("status"), str) or judgment[
        "status"
    ] not in {"pass", "fail"}:
        raise ReviewError(f"judgment {name} status must be pass or fail")
    if (
        not isinstance(judgment.get("reason"), str)
        or not judgment["reason"].strip()
    ):
        raise ReviewError(f"judgment {name} reason must be non-empty")
    refs = _validate_evidence_refs(
        judgment.get("evidence_refs"),
        evidence_ids,
        f"judgment {name}.evidence_refs",
    )
    if not refs:
        raise ReviewError(f"judgment {name} must cite evidence")
    return judgment


def _validate_reason(
    reason: object,
    evidence_ids: set[str],
    index: int,
) -> dict[str, Any]:
    if not isinstance(reason, dict):
        raise ReviewError(f"review reason {index} must be an object")
    if set(reason) != {"code", "message", "evidence_refs"}:
        raise ReviewError(f"review reason {index} has unknown or missing fields")
    if not isinstance(reason.get("code"), str) or not reason["code"].strip():
        raise ReviewError(f"review reason {index} code must be non-empty")
    if not isinstance(reason.get("message"), str) or not reason["message"].strip():
        raise ReviewError(f"review reason {index} message must be non-empty")
    _validate_evidence_refs(
        reason.get("evidence_refs"),
        evidence_ids,
        f"review reason {index}.evidence_refs",
    )
    return reason


def validate_assessment(
    assessment: dict[str, Any],
    packet: dict[str, Any],
    *,
    prior_instance_ids: set[str],
) -> dict[str, Any]:
    """Validate one closed-schema semantic review decision."""
    validate_review_packet(packet)
    expected = {
        "schema_version",
        "packet_type",
        "packet_id",
        "input_sha256",
        "instance_id",
        "status",
        "judgments",
        "reasons",
        "unassessed",
    }
    if set(assessment) != expected:
        raise ReviewError("semantic review assessment has unknown or missing fields")
    if (
        assessment.get("schema_version") != REVIEW_ASSESSMENT_SCHEMA_VERSION
        or assessment.get("packet_type") != "semantic_review"
    ):
        raise ReviewError("semantic review assessment schema is unsupported")
    if (
        assessment.get("packet_id") != packet["packet_id"]
        or assessment.get("input_sha256") != packet["input_sha256"]
    ):
        raise ReviewError("semantic review assessment does not match the packet")
    instance_id = assessment.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise ReviewError("semantic review instance_id must be non-empty")
    if instance_id in prior_instance_ids:
        raise ReviewError("semantic review instance_id was already used")
    status = assessment.get("status")
    if not isinstance(status, str) or status not in {"approve", "reject"}:
        raise ReviewError("semantic review status must be approve or reject")
    judgments = assessment.get("judgments")
    if not isinstance(judgments, dict):
        raise ReviewError("semantic review judgments must be an object")
    unknown_judgments = sorted(set(judgments) - set(REQUIRED_JUDGMENTS))
    if unknown_judgments:
        raise ReviewError(
            "semantic review has unknown judgments: " + ", ".join(unknown_judgments)
        )
    for name, judgment in judgments.items():
        _validate_judgment(name, judgment, _packet_evidence_ids(packet))
    reasons = assessment.get("reasons")
    if not isinstance(reasons, list):
        raise ReviewError("semantic review reasons must be an array")
    evidence_ids = _packet_evidence_ids(packet)
    for index, reason in enumerate(reasons):
        _validate_reason(reason, evidence_ids, index)
    unassessed = assessment.get("unassessed")
    if not isinstance(unassessed, list) or not all(
        isinstance(value, str) and value in REQUIRED_JUDGMENTS for value in unassessed
    ):
        raise ReviewError(
            "semantic review unassessed must contain required judgment names"
        )
    if len(unassessed) != len(set(unassessed)):
        raise ReviewError("semantic review unassessed contains duplicates")
    if set(judgments) & set(unassessed):
        raise ReviewError("a judgment cannot also be unassessed")
    if set(judgments) | set(unassessed) != set(REQUIRED_JUDGMENTS):
        raise ReviewError(
            "judgments and unassessed must cover every required judgment"
        )
    if status == "approve":
        if set(judgments) != set(REQUIRED_JUDGMENTS):
            raise ReviewError("approval must assess every required judgment")
        if any(value.get("status") != "pass" for value in judgments.values()):
            raise ReviewError("approval requires every judgment to pass")
        if unassessed:
            raise ReviewError("approval cannot leave judgments unassessed")
    elif not reasons:
        raise ReviewError("rejection requires at least one structured reason")
    elif not any(reason["evidence_refs"] for reason in reasons):
        raise ReviewError("rejection requires a reason with evidence")
    return assessment


def _packet_evidence_ids(packet: dict[str, Any]) -> set[str]:
    payload = packet["payload"]
    values = payload.get("evidence_ids")
    if not isinstance(values, list) or not all(
        isinstance(value, str) and value for value in values
    ):
        raise ReviewError("semantic review packet evidence_ids are invalid")
    if len(values) != len(set(values)):
        raise ReviewError("semantic review packet evidence_ids contain duplicates")
    return set(values)


def validate_digest(value: object, field: str) -> str:
    """Validate a SHA-256 field used in stored review history."""
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ReviewError(f"{field} must be a SHA-256 digest")
    return value
