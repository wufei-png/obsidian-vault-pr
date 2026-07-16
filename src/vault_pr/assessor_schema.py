"""Closed schemas for Quality Assessor outputs."""

from __future__ import annotations

from typing import Any


class AssessorSchemaError(ValueError):
    """Raised when a Quality Assessor output is incomplete or malformed."""


def _refs(value: object, allowed: set[str], field: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise AssessorSchemaError(f"{field} must be an array of evidence ids")
    if len(value) != len(set(value)):
        raise AssessorSchemaError(f"{field} contains duplicate evidence ids")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise AssessorSchemaError(
            f"{field} references unknown evidence: {', '.join(unknown)}"
        )
    return value


def _judgment(
    value: object,
    allowed_evidence: set[str],
    field: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "decision",
        "reason",
        "evidence_refs",
    }:
        raise AssessorSchemaError(f"{field} has unknown or missing fields")
    if not isinstance(value.get("decision"), str) or value[
        "decision"
    ] not in {"pass", "fail"}:
        raise AssessorSchemaError(f"{field}.decision must be pass or fail")
    reason = value.get("reason")
    refs = _refs(value.get("evidence_refs"), allowed_evidence, f"{field}.evidence_refs")
    if not isinstance(reason, str) or not reason.strip() or not refs:
        raise AssessorSchemaError(f"{field} requires a reason and evidence")
    return value


def _finding(
    value: object,
    allowed_evidence: set[str],
    field: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "finding_id",
        "kind",
        "summary",
        "evidence_refs",
    }:
        raise AssessorSchemaError(f"{field} has unknown or missing fields")
    for name in ("finding_id", "kind", "summary"):
        if not isinstance(value.get(name), str) or not value[name].strip():
            raise AssessorSchemaError(f"{field}.{name} must be non-empty")
    _refs(value.get("evidence_refs"), allowed_evidence, f"{field}.evidence_refs")
    return value


def _conflict(
    value: object,
    allowed_evidence: set[str],
    field: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "conflict_id",
        "summary",
        "evidence_refs",
    }:
        raise AssessorSchemaError(f"{field} has unknown or missing fields")
    for name in ("conflict_id", "summary"):
        if not isinstance(value.get(name), str) or not value[name].strip():
            raise AssessorSchemaError(f"{field}.{name} must be non-empty")
    _refs(value.get("evidence_refs"), allowed_evidence, f"{field}.evidence_refs")
    return value


def validate_rag_assessment(
    packet: dict[str, Any],
    assessment: dict[str, Any],
) -> dict[str, Any]:
    """Validate semantic completeness of one blind RAG assessment."""
    payload = packet.get("payload")
    if not isinstance(payload, dict) or payload.get("mode") != "rag_blind":
        raise AssessorSchemaError("RAG assessment requires a rag_blind input packet")
    expected_top = {
        "schema_version",
        "packet_type",
        "packet_id",
        "input_sha256",
        "instance_id",
        "mode",
        "status",
        "case_judgments",
        "findings",
        "claim_conflicts",
        "unassessed",
    }
    if set(assessment) != expected_top:
        raise AssessorSchemaError(
            "Quality Assessor output has unknown or missing fields"
        )
    if assessment.get("mode") != "rag_blind":
        raise AssessorSchemaError("Quality Assessor mode does not match the packet")
    if not isinstance(assessment.get("status"), str) or assessment[
        "status"
    ] not in {"pass", "fail"}:
        raise AssessorSchemaError("Quality Assessor status must be pass or fail")
    instance_id = assessment.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise AssessorSchemaError("Quality Assessor output has no instance id")

    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise AssessorSchemaError("RAG packet cases are invalid")
    note_evidence = payload.get("note_evidence")
    if not isinstance(note_evidence, list):
        raise AssessorSchemaError("RAG packet note evidence is invalid")
    note_ids = {
        f"note:{item.get('path')}"
        for item in note_evidence
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    case_evidence: dict[str, set[str]] = {}
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("case_id"), str):
            raise AssessorSchemaError("RAG packet case identities are invalid")
        case_id = case["case_id"]
        raw_refs = case.get("evidence_note_refs")
        if (
            case_id in case_evidence
            or not isinstance(raw_refs, list)
            or not all(isinstance(ref, str) and ref for ref in raw_refs)
            or len(raw_refs) != len(set(raw_refs))
        ):
            raise AssessorSchemaError("RAG packet case evidence is invalid")
        refs = {f"note:{ref}" for ref in raw_refs}
        if not refs.issubset(note_ids):
            raise AssessorSchemaError("RAG packet case evidence is invalid")
        case_evidence[case_id] = refs
    case_ids = set(case_evidence)
    allowed_evidence = note_ids | {f"case:{case_id}" for case_id in case_ids}

    raw_judgments = assessment.get("case_judgments")
    if not isinstance(raw_judgments, list):
        raise AssessorSchemaError("case_judgments must be an array")
    seen_cases: set[str] = set()
    failed = False
    for index, item in enumerate(raw_judgments):
        if not isinstance(item, dict) or set(item) != {
            "case_id",
            "answer_claims",
            "provenance",
        }:
            raise AssessorSchemaError(
                f"case_judgments[{index}] has unknown or missing fields"
            )
        case_id = item.get("case_id")
        if (
            not isinstance(case_id, str)
            or case_id not in case_ids
            or case_id in seen_cases
        ):
            raise AssessorSchemaError(
                "case_judgments contains an unknown or duplicate case"
            )
        seen_cases.add(case_id)
        judgment_evidence = case_evidence[case_id] | {f"case:{case_id}"}
        answer = _judgment(
            item.get("answer_claims"),
            judgment_evidence,
            f"case_judgments[{index}].answer_claims",
        )
        provenance = _judgment(
            item.get("provenance"),
            judgment_evidence,
            f"case_judgments[{index}].provenance",
        )
        for name, judgment in (
            ("answer_claims", answer),
            ("provenance", provenance),
        ):
            refs = set(judgment["evidence_refs"])
            if f"case:{case_id}" not in refs:
                raise AssessorSchemaError(
                    f"case_judgments[{index}].{name} must cite its case"
                )
            if (
                judgment["decision"] == "pass"
                and case_evidence[case_id]
                and refs.isdisjoint(case_evidence[case_id])
            ):
                raise AssessorSchemaError(
                    f"case_judgments[{index}].{name} pass must cite note evidence"
                )
        failed = failed or answer["decision"] == "fail" or provenance["decision"] == "fail"

    unassessed = assessment.get("unassessed")
    if not isinstance(unassessed, list):
        raise AssessorSchemaError("Quality Assessor unassessed must be an array")
    unassessed_ids: set[tuple[str, str]] = set()
    for index, item in enumerate(unassessed):
        if (
            not isinstance(item, dict)
            or set(item) != {"case_id", "kind"}
            or not isinstance(item.get("case_id"), str)
            or item["case_id"] not in case_ids
            or not isinstance(item.get("kind"), str)
            or item["kind"] not in {"answer_claims", "provenance"}
        ):
            raise AssessorSchemaError(f"unassessed[{index}] is invalid")
        identity = (item["case_id"], item["kind"])
        if identity in unassessed_ids:
            raise AssessorSchemaError("unassessed contains duplicates")
        unassessed_ids.add(identity)

    findings = assessment.get("findings")
    conflicts = assessment.get("claim_conflicts")
    if not isinstance(findings, list) or not isinstance(conflicts, list):
        raise AssessorSchemaError(
            "Quality Assessor findings and claim_conflicts must be arrays"
        )
    for index, item in enumerate(findings):
        _finding(item, allowed_evidence, f"findings[{index}]")
    for index, item in enumerate(conflicts):
        _conflict(item, allowed_evidence, f"claim_conflicts[{index}]")

    if assessment["status"] == "pass":
        if seen_cases != case_ids or unassessed:
            raise AssessorSchemaError(
                "passed assessment must cover every case with no unassessed items"
            )
        if failed:
            raise AssessorSchemaError("passed assessment cannot contain failed judgments")
    elif not failed:
        raise AssessorSchemaError(
            "failed assessment requires at least one failed structured judgment"
        )
    return assessment
