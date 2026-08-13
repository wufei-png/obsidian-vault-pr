"""Closed Plan v4 values and pure normalization helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from .errors import PlanError


PLAN_SCHEMA_VERSION = 4
ALLOWED_ACTIONS = frozenset(
    {"create", "update", "delete", "rename", "purge_capture", "promote_asset"}
)
ALLOWED_INTENTS = frozenset(
    {"integrate", "maintenance", "merge", "split", "rename"}
)
SOURCE_ID_PATTERN = re.compile(r"src_[0-9a-f]{20}")
TRANSACTION_ID_PATTERN = re.compile(r"txn_[A-Za-z0-9_-]{1,80}")
HEX64_PATTERN = re.compile(r"[0-9a-f]{64}")
MAX_EVIDENCE_EXCERPT_CHARACTERS = 12_000
EVIDENCE_CONTEXT_LINES = 2
MAX_RETRIEVAL_SNIPPET_CHARACTERS = 2_000
FOCUS_RANGES_ABSENT = object()


class FocusRange(TypedDict):
    start_line: int
    end_line: int


class FocusExcerpt(FocusRange):
    context_start_line: int
    context_end_line: int
    excerpt: str


@dataclass(frozen=True)
class PlanBundle:
    """Immutable bytes that identify one apply request."""

    plan_path: Path
    transaction_id: str
    plan_bytes: bytes
    content_snapshots: tuple[tuple[Path, bytes], ...]
    assessment_bytes: bytes | None
    review_history_bytes: bytes | None
    request_sha256: str


def normalized_summary(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise PlanError(f"{field} must be a string")
    summary = " ".join(value.split()).strip()
    if not summary:
        raise PlanError(f"{field} must not be empty")
    return summary


def normalize_maintenance_authorization(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PlanError("plan authorization must be an object")
    if raw.get("kind") != "maintenance_request":
        raise PlanError("plan authorization kind is unsupported")
    summary = normalized_summary(raw.get("summary"), "authorization.summary")
    allowed = {"kind", "summary", "request_ref", "request_sha256"}
    if set(raw) - allowed:
        raise PlanError("maintenance request authorization has unknown fields")
    request_ref = raw.get("request_ref")
    request_sha256 = raw.get("request_sha256")
    if (request_ref is None) != (request_sha256 is None):
        raise PlanError(
            "authorization request_ref and request_sha256 must be provided together"
        )
    normalized: dict[str, Any] = {
        "kind": "maintenance_request",
        "summary": summary,
    }
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


def source_review_evidence(
    extracted_text: str,
    raw_focus_ranges: object,
) -> dict[str, Any]:
    """Build bounded, reproducible review excerpts from one Capture."""
    if raw_focus_ranges is FOCUS_RANGES_ABSENT:
        if len(extracted_text) > MAX_EVIDENCE_EXCERPT_CHARACTERS:
            raise PlanError(
                "long source_capture evidence requires non-empty focus_ranges"
            )
        return {"excerpt": extracted_text}

    if not isinstance(raw_focus_ranges, list) or not raw_focus_ranges:
        raise PlanError("source_capture focus_ranges must be a non-empty array")
    lines = extracted_text.splitlines(keepends=True)
    normalized_ranges: list[FocusRange] = []
    focus_excerpts: list[FocusExcerpt] = []
    total_characters = 0
    for index, raw_range in enumerate(raw_focus_ranges):
        if not isinstance(raw_range, dict) or set(raw_range) != {
            "start_line",
            "end_line",
        }:
            raise PlanError(f"source_capture focus range {index} is invalid")
        start_line = raw_range.get("start_line")
        end_line = raw_range.get("end_line")
        if (
            isinstance(start_line, bool)
            or not isinstance(start_line, int)
            or isinstance(end_line, bool)
            or not isinstance(end_line, int)
            or start_line < 1
            or end_line < start_line
            or end_line > len(lines)
        ):
            raise PlanError(f"source_capture focus range {index} is out of bounds")
        context_start_line = max(1, start_line - EVIDENCE_CONTEXT_LINES)
        context_end_line = min(len(lines), end_line + EVIDENCE_CONTEXT_LINES)
        excerpt = "".join(lines[context_start_line - 1 : context_end_line])
        total_characters += len(excerpt)
        if total_characters > MAX_EVIDENCE_EXCERPT_CHARACTERS:
            raise PlanError(
                "source_capture focused excerpts exceed the per-source review budget"
            )
        normalized_ranges.append(
            {"start_line": start_line, "end_line": end_line}
        )
        focus_excerpts.append(
            {
                "start_line": start_line,
                "end_line": end_line,
                "context_start_line": context_start_line,
                "context_end_line": context_end_line,
                "excerpt": excerpt,
            }
        )
    return {
        "focus_ranges": normalized_ranges,
        "focus_excerpts": focus_excerpts,
    }
