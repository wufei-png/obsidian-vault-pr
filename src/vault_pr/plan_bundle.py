"""Immutable Plan bundle loading and request identity."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .errors import PlanError
from .plan_models import PLAN_SCHEMA_VERSION, TRANSACTION_ID_PATTERN, PlanBundle
from .review import canonical_json


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PlanError(f"{label} not found") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise PlanError(f"{label} must be a JSON object")
    return value


def content_file(
    plan_path: Path,
    operation: dict[str, Any],
    content_snapshots: dict[Path, bytes] | None = None,
) -> Path:
    raw_content_file = operation.get("content_file")
    if not isinstance(raw_content_file, str) or not raw_content_file.strip():
        raise PlanError("create/update operation requires content_file")
    content_path = (plan_path.parent / raw_content_file).resolve()
    if not content_path.is_relative_to(plan_path.parent.resolve()):
        raise PlanError("content_file escapes the plan directory")
    if content_snapshots is not None:
        if content_path not in content_snapshots:
            raise PlanError(f"content_file is missing from plan bundle: {raw_content_file}")
    elif not content_path.is_file():
        raise PlanError(f"content_file does not exist: {raw_content_file}")
    return content_path


def proposed_content_paths(
    plan_path: Path,
    plan: dict[str, Any] | None = None,
) -> list[Path]:
    plan = plan if plan is not None else read_json(plan_path, "plan")
    operations = plan.get("operations")
    if not isinstance(operations, list):
        raise PlanError("plan operations must be an array")
    content_paths: set[Path] = set()
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise PlanError(f"operation {index} must be an object")
        if operation.get("content_file") is not None:
            content_paths.add(content_file(plan_path, operation))
    return sorted(content_paths)


def _optional_regular_bytes(path: Path, label: str) -> bytes | None:
    if path.is_symlink():
        raise PlanError(f"{label} must be a regular file")
    if not path.exists():
        return None
    if not path.is_file():
        raise PlanError(f"{label} must be a regular file")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PlanError(f"unable to read {label}") from exc


def freeze_plan_bundle(config: dict[str, Any], plan_path: Path) -> PlanBundle:
    """Read all plan-controlled bytes once for validation and application."""
    plan_path = plan_path.expanduser().resolve()
    try:
        plan_bytes = plan_path.read_bytes()
        plan = json.loads(plan_bytes)
    except FileNotFoundError as exc:
        raise PlanError("plan not found") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError("invalid plan") from exc
    if not isinstance(plan, dict):
        raise PlanError("plan must be a JSON object")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise PlanError("unsupported plan schema_version")
    transaction_id = plan.get("transaction_id")
    if not isinstance(transaction_id, str) or not TRANSACTION_ID_PATTERN.fullmatch(
        transaction_id
    ):
        raise PlanError("invalid transaction_id")
    digest = hashlib.sha256()
    digest.update(b"plan\0")
    digest.update(plan_bytes)
    content_snapshots: list[tuple[Path, bytes]] = []
    for path in proposed_content_paths(plan_path, plan):
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise PlanError("unable to read proposed content") from exc
        content_snapshots.append((path, content))
        digest.update(b"content\0")
        digest.update(path.relative_to(plan_path.parent).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
    optional: dict[str, bytes | None] = {}
    for name, label in (
        ("assessment.json", "semantic review assessment"),
        ("review-history.json", "review history"),
    ):
        content = _optional_regular_bytes(plan_path.parent / name, label)
        optional[name] = content
        digest.update(name.encode("ascii"))
        digest.update(b"\0")
        if content is not None:
            digest.update(content)
    relevant_config = {
        key: config.get(key, {})
        for key in (
            "semantic",
            "sources",
            "notes",
            "review",
            "quality",
            "index",
            "assets",
        )
    }
    digest.update(b"config\0")
    digest.update(canonical_json(relevant_config))
    return PlanBundle(
        plan_path=plan_path,
        transaction_id=transaction_id,
        plan_bytes=plan_bytes,
        content_snapshots=tuple(content_snapshots),
        assessment_bytes=optional["assessment.json"],
        review_history_bytes=optional["review-history.json"],
        request_sha256=digest.hexdigest(),
    )
