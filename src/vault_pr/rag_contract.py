"""Closed contracts for blind RAG assessment packets."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .assessor_schema import AssessorSchemaError, validate_rag_assessment


class RagContractError(ValueError):
    """Raised when a RAG assessor packet violates its closed contract."""


RAG_CASE_ID = re.compile(r"rag_[a-z0-9_-]{1,80}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RAG_PAYLOAD_FIELDS = {
    "mode",
    "git_commit",
    "cases_sha256",
    "answers_sha256",
    "limit",
    "index_evidence",
    "evidence",
    "cases",
    "note_evidence",
    "required_judgments",
    "unassessed",
}
_RAG_CASE_FIELDS = {
    "case_id",
    "query",
    "expected_paths",
    "retrieved_paths",
    "recall_at_k",
    "reciprocal_rank",
    "retrieval_pass",
    "thresholds",
    "expected_claims",
    "expected_provenance",
    "evidence_note_refs",
    "answer",
    "answer_provenance",
}


def canonical_digest(value: Any) -> str:
    """Return the SHA-256 digest of a canonical JSON value."""
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def answers_sha256(cases: list[dict[str, Any]]) -> str:
    """Hash the canonical generated-answer projection."""
    normalized = [
        {
            "case_id": str(case["case_id"]),
            "answer": str(case["answer"]),
            "provenance": list(case["answer_provenance"]),
        }
        for case in sorted(cases, key=lambda item: str(item["case_id"]))
    ]
    return canonical_digest(normalized)


def _packet_strings(
    value: object,
    *,
    label: str,
    minimum: int,
    maximum: int,
    max_length: int = 2000,
) -> list[str]:
    if (
        not isinstance(value, list)
        or not minimum <= len(value) <= maximum
        or not all(
            isinstance(item, str)
            and item.strip()
            and len(item) <= max_length
            for item in value
        )
        or len(value) != len(set(value))
    ):
        raise RagContractError(f"{label} is invalid")
    return value


def _validate_rag_packet_payload(payload: dict[str, Any]) -> None:
    if set(payload) != _RAG_PAYLOAD_FIELDS:
        raise RagContractError(
            "Quality Assessor payload has unknown or missing fields"
        )
    limit = payload.get("limit")
    if (
        payload.get("mode") != "rag_blind"
        or not isinstance(payload.get("git_commit"), str)
        or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", payload["git_commit"])
        or not isinstance(payload.get("cases_sha256"), str)
        or not _SHA256.fullmatch(payload["cases_sha256"])
        or not isinstance(payload.get("answers_sha256"), str)
        or not _SHA256.fullmatch(payload["answers_sha256"])
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 50
        or not isinstance(payload.get("index_evidence"), dict)
    ):
        raise RagContractError("Quality Assessor payload identity is invalid")

    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        raise RagContractError("Quality Assessor payload evidence is invalid")
    evidence_by_kind: dict[str, dict[str, Any]] = {}
    for item in evidence:
        if not isinstance(item, dict) or not isinstance(item.get("kind"), str):
            raise RagContractError("Quality Assessor payload evidence is invalid")
        kind = item["kind"]
        expected_fields = (
            {"kind", "path", "sha256"}
            if kind == "rag_case_set"
            else {"kind", "sha256"}
            if kind in {"generated_answers", "qmd_index_status"}
            else set()
        )
        if (
            not expected_fields
            or set(item) != expected_fields
            or kind in evidence_by_kind
            or not isinstance(item.get("sha256"), str)
            or not _SHA256.fullmatch(item["sha256"])
            or (
                kind == "rag_case_set"
                and (
                    not isinstance(item.get("path"), str)
                    or not item["path"]
                )
            )
        ):
            raise RagContractError("Quality Assessor payload evidence is invalid")
        evidence_by_kind[kind] = item
    if (
        set(evidence_by_kind)
        not in (
            {"rag_case_set", "generated_answers"},
            {"rag_case_set", "generated_answers", "qmd_index_status"},
        )
        or evidence_by_kind["rag_case_set"]["sha256"]
        != payload["cases_sha256"]
        or evidence_by_kind["generated_answers"]["sha256"]
        != payload["answers_sha256"]
    ):
        raise RagContractError("Quality Assessor payload evidence is incomplete")
    status_sha256 = payload["index_evidence"].get("status_sha256")
    status_evidence = evidence_by_kind.get("qmd_index_status")
    if (
        isinstance(status_sha256, str)
        and (
            not _SHA256.fullmatch(status_sha256)
            or status_evidence is None
            or status_evidence["sha256"] != status_sha256
        )
    ) or (status_sha256 is None) != (status_evidence is None):
        raise RagContractError(
            "Quality Assessor index evidence is not hash-bound"
        )

    note_evidence = payload.get("note_evidence")
    if not isinstance(note_evidence, list) or not 1 <= len(note_evidence) <= 500:
        raise RagContractError("Quality Assessor note evidence is invalid")
    note_paths: set[str] = set()
    for note in note_evidence:
        if (
            not isinstance(note, dict)
            or set(note)
            != {"path", "sha256", "excerpt", "excerpt_truncated"}
            or not isinstance(note.get("path"), str)
            or not note["path"]
            or note["path"] in note_paths
            or not isinstance(note.get("sha256"), str)
            or not _SHA256.fullmatch(note["sha256"])
            or not isinstance(note.get("excerpt"), str)
            or len(note["excerpt"]) > 4000
            or not isinstance(note.get("excerpt_truncated"), bool)
        ):
            raise RagContractError("Quality Assessor note evidence is invalid")
        note_paths.add(note["path"])

    cases = payload.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise RagContractError("Quality Assessor RAG cases are invalid")
    case_ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != _RAG_CASE_FIELDS:
            raise RagContractError(
                "Quality Assessor RAG case has unknown or missing fields"
            )
        case_id = case.get("case_id")
        query = case.get("query")
        answer = case.get("answer")
        thresholds = case.get("thresholds")
        if (
            not isinstance(case_id, str)
            or not RAG_CASE_ID.fullmatch(case_id)
            or case_id in case_ids
            or not isinstance(query, str)
            or not query.strip()
            or len(query) > 2000
            or not isinstance(answer, str)
            or not answer.strip()
            or len(answer) > 20_000
            or not isinstance(case.get("retrieval_pass"), bool)
            or not isinstance(thresholds, dict)
            or set(thresholds)
            != {"min_recall_at_k", "min_reciprocal_rank"}
        ):
            raise RagContractError("Quality Assessor RAG case is invalid")
        case_ids.add(case_id)
        expected_paths = _packet_strings(
            case["expected_paths"],
            label="RAG case expected_paths",
            minimum=1,
            maximum=20,
        )
        retrieved_paths = _packet_strings(
            case["retrieved_paths"],
            label="RAG case retrieved_paths",
            minimum=0,
            maximum=limit,
        )
        expected_claims = _packet_strings(
            case["expected_claims"],
            label="RAG case expected_claims",
            minimum=1,
            maximum=20,
        )
        expected_provenance = _packet_strings(
            case["expected_provenance"],
            label="RAG case expected_provenance",
            minimum=1,
            maximum=20,
        )
        answer_provenance = _packet_strings(
            case["answer_provenance"],
            label="RAG case answer_provenance",
            minimum=1,
            maximum=20,
        )
        evidence_refs = _packet_strings(
            case["evidence_note_refs"],
            label="RAG case evidence_note_refs",
            minimum=1,
            maximum=500,
        )
        expected_refs = set(
            expected_paths
            + retrieved_paths
            + expected_provenance
            + answer_provenance
        )
        numeric_values = (
            thresholds["min_recall_at_k"],
            thresholds["min_reciprocal_rank"],
            case["recall_at_k"],
            case["reciprocal_rank"],
        )
        if (
            not all(
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and 0 <= value <= 1
                for value in numeric_values
            )
            or not expected_claims
            or set(evidence_refs) != expected_refs
            or not expected_refs.issubset(note_paths)
        ):
            raise RagContractError("Quality Assessor RAG case is invalid")

    expected_judgments = {
        (case_id, kind)
        for case_id in case_ids
        for kind in ("answer_claims", "provenance")
    }
    required = payload.get("required_judgments")
    if not isinstance(required, list):
        raise RagContractError("Quality Assessor required judgments are invalid")
    actual_judgments: set[tuple[str, str]] = set()
    for item in required:
        if (
            not isinstance(item, dict)
            or set(item) != {"case_id", "kind"}
            or not isinstance(item.get("case_id"), str)
            or not isinstance(item.get("kind"), str)
        ):
            raise RagContractError(
                "Quality Assessor required judgments are invalid"
            )
        actual_judgments.add((item["case_id"], item["kind"]))
    if (
        len(actual_judgments) != len(required)
        or actual_judgments != expected_judgments
        or payload.get("unassessed") != ["answer_claims", "provenance"]
    ):
        raise RagContractError(
            "Quality Assessor required judgments are incomplete"
        )


def validated_assessor_packet(
    evaluation: dict[str, Any], assessment: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate and return a blind RAG assessor packet and payload."""
    packet = evaluation.get("quality_assessor_packet")
    if (
        not isinstance(packet, dict)
        or set(packet)
        != {
            "schema_version",
            "packet_type",
            "packet_id",
            "created_at",
            "input_sha256",
            "mode",
            "payload",
        }
    ):
        raise RagContractError("evaluation has no Quality Assessor packet")
    payload = packet.get("payload")
    if not isinstance(payload, dict):
        raise RagContractError("Quality Assessor packet has no canonical payload")
    _validate_rag_packet_payload(payload)
    digest = canonical_digest(payload)
    packet_id = f"pkt_{digest[:20]}"
    if (
        packet.get("schema_version") != 1
        or packet.get("packet_type") != "quality_assessment_input"
        or packet.get("packet_id") != packet_id
        or packet.get("input_sha256") != digest
        or not isinstance(packet.get("created_at"), str)
        or not packet["created_at"]
        or packet.get("mode") != "rag_blind"
        or payload.get("mode") != "rag_blind"
        or assessment.get("mode") != "rag_blind"
    ):
        raise RagContractError("Quality Assessor packet hash validation failed")
    if (
        assessment.get("schema_version") != 1
        or assessment.get("packet_type") != "quality_assessment"
        or assessment.get("packet_id") != packet_id
        or assessment.get("input_sha256") != digest
    ):
        raise RagContractError("Quality Assessor output does not match the input")
    try:
        validate_rag_assessment(packet, assessment)
    except AssessorSchemaError as exc:
        raise RagContractError(str(exc)) from exc
    return packet, payload
