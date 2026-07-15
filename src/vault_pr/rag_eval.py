"""Vault-owned RAG retrieval evaluation and assessor-packet preparation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from .config import ConfigError, index_spec, load_vault_path, vault_relative_path
from .git import GitSafetyError, require_clean_git_root, run_git, vault_write_lock
from .index import IndexUpdateError, reconcile_qmd_collection
from .recovery import (
    RecoveryError,
    mark_active_applied,
    prepare_active_transaction,
    rollback_active_transaction,
)
from .retrieve import (
    RetrievalError,
    build_qmd_path_map,
    parse_qmd_results,
    resolve_qmd_path,
)


class RagEvaluationError(RuntimeError):
    """Raised when a RAG evaluation case set or retrieval run is invalid."""


Runner = Callable[..., subprocess.CompletedProcess[str]]
IndexValidator = Callable[[dict[str, Any], str], dict[str, Any]]
_CASE_ID = re.compile(r"rag_[a-z0-9_-]{1,80}")


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _validate_index_evidence(
    config: dict[str, Any], executable: str
) -> dict[str, Any]:
    vault = load_vault_path(config)
    head = run_git(vault, "rev-parse", "HEAD")
    config_sha256 = hashlib.sha256(
        json.dumps(config, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    git_dir = run_git(vault, "rev-parse", "--absolute-git-dir")
    reasons: list[str] = []
    status_record: dict[str, Any] = {}
    status_sha256: str | None = None
    if git_dir.returncode != 0:
        reasons.append("index_status_unavailable")
    else:
        status_path = Path(git_dir.stdout.strip()) / "vault-pr-index-status.json"
        try:
            status_bytes = status_path.read_bytes()
            parsed = json.loads(status_bytes)
            if isinstance(parsed, dict):
                status_record = parsed
                status_sha256 = hashlib.sha256(status_bytes).hexdigest()
            else:
                reasons.append("index_status_invalid")
        except (OSError, json.JSONDecodeError):
            reasons.append("index_status_unavailable")
    try:
        alignment = reconcile_qmd_collection(
            config, check_only=True, executable=executable
        )
    except (ConfigError, IndexUpdateError):
        alignment = {"status": "unknown"}
        reasons.append("collection_check_failed")
    version = subprocess.run(
        [executable, "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    qmd_version = version.stdout.strip() if version.returncode == 0 else "unknown"
    expected_commit = head.stdout.strip() if head.returncode == 0 else None
    checks = {
        "collection_aligned": alignment.get("status") == "aligned",
        "index_completed": status_record.get("status") == "indexed",
        "commit_matches": status_record.get("git_commit") == expected_commit,
        "config_matches": status_record.get("config_sha256") == config_sha256,
        "qmd_version_matches": status_record.get("qmd_version") == qmd_version,
    }
    reasons.extend(name for name, passed in checks.items() if not passed)
    return {
        "eligible": not reasons,
        "reasons": sorted(set(reasons)),
        "git_commit": expected_commit,
        "config_sha256": config_sha256,
        "qmd_version": qmd_version,
        "status_sha256": status_sha256,
        "collection_status": alignment.get("status"),
    }


def _string_list(
    value: Any,
    *,
    label: str,
    minimum: int = 1,
    maximum: int = 20,
    max_string_length: int = 2000,
) -> list[str]:
    if not isinstance(value, list) or not (minimum <= len(value) <= maximum):
        raise RagEvaluationError(
            f"{label} must contain between {minimum} and {maximum} strings"
        )
    if not all(
        isinstance(item, str) and item.strip() and len(item) <= max_string_length
        for item in value
    ):
        raise RagEvaluationError(f"{label} must contain non-empty strings")
    normalized = [item.strip() for item in value]
    if len(set(normalized)) != len(normalized):
        raise RagEvaluationError(f"{label} contains duplicates")
    return normalized


def _thresholds(value: Any) -> dict[str, float]:
    raw = value if value is not None else {}
    if not isinstance(raw, dict):
        raise RagEvaluationError("case thresholds must be an object")
    try:
        recall = float(raw.get("min_recall_at_k", 1.0))
        reciprocal_rank = float(raw.get("min_reciprocal_rank", 0.1))
    except (TypeError, ValueError) as exc:
        raise RagEvaluationError("case thresholds must be numeric") from exc
    if not 0 <= recall <= 1 or not 0 <= reciprocal_rank <= 1:
        raise RagEvaluationError("case thresholds must be between zero and one")
    return {
        "min_recall_at_k": recall,
        "min_reciprocal_rank": reciprocal_rank,
    }


def _case_path(config: dict[str, Any], vault: Path, override: Path | None) -> Path:
    if override is not None:
        raw: object = str(override)
    else:
        quality = config.get("quality", {})
        if not isinstance(quality, dict):
            raise ConfigError("quality must be a table")
        raw = quality.get("rag_cases_path", ".vault-pr/quality/rag/cases.jsonl")
    return vault_relative_path(vault, raw, "quality.rag_cases_path")


def _load_cases(
    config: dict[str, Any], vault: Path, override: Path | None
) -> tuple[Path, list[dict[str, Any]]]:
    path = _case_path(config, vault, override)
    if path.is_symlink() or not path.is_file():
        raise RagEvaluationError("RAG evaluation case file is missing or unsafe")
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise RagEvaluationError("RAG evaluation case file is not UTF-8") from exc
    for line_number, raw_line in enumerate(raw_lines, 1):
        if not raw_line.strip():
            continue
        try:
            case = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise RagEvaluationError(
                f"invalid RAG evaluation JSON on line {line_number}"
            ) from exc
        if not isinstance(case, dict) or case.get("schema_version") != 1:
            raise RagEvaluationError("unsupported RAG evaluation case schema")
        case_id = case.get("case_id")
        query = case.get("query")
        if not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id):
            raise RagEvaluationError("invalid RAG evaluation case id")
        if case_id in seen:
            raise RagEvaluationError("duplicate RAG evaluation case id")
        seen.add(case_id)
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise RagEvaluationError("invalid RAG evaluation query")
        if case.get("accepted") is not True:
            raise RagEvaluationError("RAG evaluation cases must be human-accepted")
        expected_paths = _note_paths(
            vault, case.get("expected_paths"), label="expected_paths"
        )
        cases.append(
            {
                "schema_version": 1,
                "case_id": case_id,
                "query": query.strip(),
                "expected_paths": expected_paths,
                "expected_claims": _string_list(
                    case.get("expected_claims"), label="expected_claims"
                ),
                "expected_provenance": _note_paths(
                    vault,
                    case.get("expected_provenance"),
                    label="expected_provenance",
                ),
                "thresholds": _thresholds(case.get("thresholds")),
            }
        )
    if not 1 <= len(cases) <= 100:
        raise RagEvaluationError("RAG evaluation set must contain 1 to 100 cases")
    return path, cases


def _note_paths(vault: Path, value: Any, *, label: str) -> list[str]:
    paths = _string_list(value, label=label)
    normalized: list[str] = []
    for raw_path in paths:
        relative = Path(raw_path)
        candidate = vault / relative
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.suffix.casefold() != ".md"
            or candidate.is_symlink()
            or not candidate.is_file()
            or not candidate.resolve().is_relative_to(vault)
        ):
            raise RagEvaluationError(f"{label} must reference vault Markdown notes")
        normalized.append(relative.as_posix())
    return normalized


def _load_answers(
    path: Path | None, case_ids: set[str], vault: Path
) -> tuple[dict[str, dict[str, Any]], str | None]:
    if path is None:
        return {}, None
    path = path.expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
        raise RagEvaluationError("RAG answer file is missing, unsafe, or too large")
    path = path.resolve()
    try:
        content = path.read_bytes()
        raw_lines = content.decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise RagEvaluationError("RAG answer file is not UTF-8") from exc
    answers: dict[str, dict[str, Any]] = {}
    for line_number, raw_line in enumerate(raw_lines, 1):
        if not raw_line.strip():
            continue
        try:
            item = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise RagEvaluationError(
                f"invalid RAG answer JSON on line {line_number}"
            ) from exc
        if not isinstance(item, dict) or item.get("schema_version") != 1:
            raise RagEvaluationError("unsupported RAG answer schema")
        case_id = item.get("case_id")
        answer = item.get("answer")
        if not isinstance(case_id, str) or case_id not in case_ids:
            raise RagEvaluationError("RAG answer references an unknown case")
        if case_id in answers:
            raise RagEvaluationError("duplicate RAG answer case id")
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 20_000:
            raise RagEvaluationError("RAG answer must contain bounded text")
        answers[case_id] = {
            "answer": answer.strip(),
            "provenance": _note_paths(
                vault, item.get("provenance"), label="answer provenance"
            ),
        }
    if set(answers) != case_ids:
        raise RagEvaluationError("RAG answer file must cover every evaluation case")
    return answers, hashlib.sha256(content).hexdigest()


def _note_evidence(vault: Path, paths: set[str]) -> list[dict[str, Any]]:
    if len(paths) > 500:
        raise RagEvaluationError("RAG note evidence exceeds the 500-note limit")
    evidence: list[dict[str, Any]] = []
    excerpt_bytes = 0
    for relative in sorted(paths):
        try:
            _path, content = _read_safe_vault_file(
                vault,
                Path(relative),
                label="RAG evidence note",
                max_bytes=2_000_000,
            )
            text = content.decode("utf-8")
            excerpt = text[:4000]
        except (OSError, UnicodeDecodeError) as exc:
            raise RagEvaluationError("RAG evidence note is not UTF-8") from exc
        excerpt_bytes += len(excerpt.encode("utf-8"))
        if excerpt_bytes > 2_000_000:
            raise RagEvaluationError("RAG note evidence exceeds the 2 MB excerpt limit")
        evidence.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "excerpt": excerpt,
                "excerpt_truncated": len(text) > 4000,
            }
        )
    return evidence


def _read_safe_vault_file(
    vault: Path, relative: Path, *, label: str, max_bytes: int
) -> tuple[Path, bytes]:
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise RagEvaluationError(f"{label} path is unsafe")
    candidate = vault
    try:
        for part in relative.parts:
            candidate /= part
            if candidate.is_symlink():
                raise RagEvaluationError(f"{label} path is unsafe")
        if (
            not candidate.is_file()
            or not candidate.resolve().is_relative_to(vault)
            or candidate.stat().st_size > max_bytes
        ):
            raise RagEvaluationError(f"{label} is missing, unsafe, or too large")
        return candidate, candidate.read_bytes()
    except OSError as exc:
        raise RagEvaluationError(f"{label} is missing, unsafe, or too large") from exc


def _all_paths_tracked(vault: Path, paths: set[str]) -> bool:
    if not paths:
        return True
    return (
        run_git(vault, "ls-files", "--error-unmatch", "--", *sorted(paths)).returncode
        == 0
    )


def _query_paths(
    config: dict[str, Any],
    query: str,
    *,
    limit: int,
    runner: Runner,
    executable: str,
) -> list[str]:
    vault = load_vault_path(config)
    spec = index_spec(config, vault)
    index_name = str(config.get("index", {}).get("name", "notebook"))
    path_map = build_qmd_path_map(spec.root_dir)
    merged: dict[str, float] = {}
    for method in ("search", "vsearch"):
        result = runner(
            [
                executable,
                "--index",
                index_name,
                method,
                query,
                "-c",
                spec.collection,
                "-n",
                str(limit),
                "--format",
                "json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RagEvaluationError(f"qmd {method} failed: {detail}")
        try:
            items = parse_qmd_results(result.stdout)
        except RetrievalError as exc:
            raise RagEvaluationError(str(exc)) from exc
        for item in items:
            file_name = str(item.get("file", ""))
            parsed = urlsplit(file_name)
            if parsed.scheme != "qmd" or parsed.netloc != spec.collection:
                raise RagEvaluationError("qmd result is outside the evaluated collection")
            relative = unquote(parsed.path).lstrip("/")
            try:
                resolved = resolve_qmd_path(spec.root_dir, relative, path_map)
            except RetrievalError as exc:
                raise RagEvaluationError(str(exc)) from exc
            vault_path = resolved.relative_to(vault).as_posix()
            try:
                score = float(item.get("score", 0))
            except (TypeError, ValueError) as exc:
                raise RagEvaluationError("qmd result contains an invalid score") from exc
            merged[vault_path] = max(score, merged.get(vault_path, float("-inf")))
    return [
        path
        for path, _score in sorted(merged.items(), key=lambda item: (-item[1], item[0]))[
            :limit
        ]
    ]


def evaluate_rag(
    config: dict[str, Any],
    *,
    cases_path: Path | None = None,
    answers_path: Path | None = None,
    limit: int = 10,
    runner: Runner = subprocess.run,
    executable: str | None = None,
    index_validator: IndexValidator = _validate_index_evidence,
) -> dict[str, Any]:
    """Score expected retrieval and prepare bounded semantic-judgment evidence."""
    if not 1 <= limit <= 50:
        raise RagEvaluationError("RAG evaluation limit must be between 1 and 50")
    vault = load_vault_path(config)
    qmd = executable or shutil.which("qmd")
    if not qmd:
        raise RagEvaluationError("required tool missing: qmd")
    path, cases = _load_cases(config, vault, cases_path)
    answers, answers_sha256 = _load_answers(
        answers_path, {str(case["case_id"]) for case in cases}, vault
    )
    mode = "rag_blind" if answers else "rag_discovery"
    results: list[dict[str, Any]] = []
    for case in cases:
        retrieved = _query_paths(
            config,
            case["query"],
            limit=limit,
            runner=runner,
            executable=qmd,
        )
        expected = set(case["expected_paths"])
        ranked_hits = [index + 1 for index, item in enumerate(retrieved) if item in expected]
        recall = len(ranked_hits) / len(expected)
        reciprocal_rank = 1 / ranked_hits[0] if ranked_hits else 0.0
        thresholds = case["thresholds"]
        retrieval_pass = (
            recall >= thresholds["min_recall_at_k"]
            and reciprocal_rank >= thresholds["min_reciprocal_rank"]
        )
        answer = answers.get(str(case["case_id"]))
        evidence_paths = (
            expected
            | set(retrieved)
            | set(case["expected_provenance"])
            | set(answer["provenance"] if answer else [])
        )
        result = {
            "case_id": case["case_id"],
            "query": case["query"],
            "expected_paths": case["expected_paths"],
            "retrieved_paths": retrieved,
            "recall_at_k": recall,
            "reciprocal_rank": reciprocal_rank,
            "retrieval_pass": retrieval_pass,
            "thresholds": thresholds,
            "expected_claims": case["expected_claims"],
            "expected_provenance": case["expected_provenance"],
            "evidence_note_refs": sorted(evidence_paths),
            "answer": answer["answer"] if answer else None,
            "answer_provenance": answer["provenance"] if answer else [],
        }
        results.append(result)
    status = run_git(vault, "status", "--porcelain=v1")
    head = run_git(vault, "rev-parse", "HEAD")
    relative_cases = path.relative_to(vault).as_posix()
    tracked_cases = run_git(vault, "ls-files", "--error-unmatch", "--", relative_cases)
    expected_paths = {
        path for case in cases for path in case["expected_paths"]
    }
    retrieved_paths = {
        path for result in results for path in result["retrieved_paths"]
    }
    provenance_paths = {
        path
        for result in results
        for path in result["expected_provenance"] + result["answer_provenance"]
    }
    expected_paths_tracked = _all_paths_tracked(vault, expected_paths)
    retrieved_paths_tracked = _all_paths_tracked(vault, retrieved_paths)
    provenance_paths_tracked = _all_paths_tracked(vault, provenance_paths)
    index_evidence = index_validator(config, qmd)
    note_evidence_paths = {
        path for result in results for path in result["evidence_note_refs"]
    }
    note_evidence = _note_evidence(vault, note_evidence_paths)
    git_commit = head.stdout.strip() if head.returncode == 0 else None
    cases_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    assessment_candidate_eligible = (
        status.returncode == 0
        and not status.stdout
        and tracked_cases.returncode == 0
        and expected_paths_tracked
        and retrieved_paths_tracked
        and provenance_paths_tracked
        and bool(index_evidence.get("eligible"))
        and bool(answers)
        and all(result["retrieval_pass"] for result in results)
    )
    packet_evidence: list[dict[str, Any]] = [
        {
            "kind": "rag_case_set",
            "path": relative_cases,
            "sha256": cases_sha256,
        }
    ]
    if answers_sha256 is not None:
        packet_evidence.append(
            {"kind": "generated_answers", "sha256": answers_sha256}
        )
    if isinstance(index_evidence.get("status_sha256"), str):
        packet_evidence.append(
            {
                "kind": "qmd_index_status",
                "sha256": index_evidence["status_sha256"],
            }
        )
    required_judgments = ["answer_claims", "provenance"] if answers else []
    unassessed = (
        ["answer_claims", "provenance"]
        if answers
        else ["answer_generation", "answer_claims", "provenance"]
    )
    packet_payload = {
        "mode": mode,
        "git_commit": git_commit,
        "cases_sha256": cases_sha256,
        "answers_sha256": answers_sha256,
        "limit": limit,
        "index_evidence": index_evidence,
        "evidence": packet_evidence,
        "cases": results,
        "note_evidence": note_evidence,
        "required_judgments": required_judgments,
        "unassessed": unassessed,
    }
    packet_digest = _canonical_digest(packet_payload)
    return {
        "schema_version": 1,
        "assessment": "rag",
        "generated_at": generated_at,
        "git_commit": git_commit,
        "git_clean": status.returncode == 0 and not status.stdout,
        "assessment_candidate_eligible": assessment_candidate_eligible,
        "baseline_recorded": False,
        "cases_sha256": cases_sha256,
        "answers_sha256": answers_sha256,
        "index_evidence": index_evidence,
        "expected_paths_tracked": expected_paths_tracked,
        "retrieved_paths_tracked": retrieved_paths_tracked,
        "provenance_paths_tracked": provenance_paths_tracked,
        "semantic_assessment_ready": bool(answers),
        "limit": limit,
        "metrics": {
            "cases": len(results),
            "retrieval_passed": sum(item["retrieval_pass"] for item in results),
            "mean_recall_at_k": sum(item["recall_at_k"] for item in results)
            / len(results),
            "mean_reciprocal_rank": sum(item["reciprocal_rank"] for item in results)
            / len(results),
        },
        "quality_assessor_packet": {
            "schema_version": 1,
            "packet_type": "quality_assessment_input",
            "packet_id": f"pkt_{packet_digest[:20]}",
            "created_at": generated_at,
            "input_sha256": packet_digest,
            "mode": mode,
            "payload": packet_payload,
        },
    }


def _read_bounded_json(
    path: Path, label: str, *, max_bytes: int
) -> tuple[dict[str, Any], bytes]:
    path = path.expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
        raise RagEvaluationError(f"{label} is missing, unsafe, or too large")
    try:
        content = path.read_bytes()
        parsed = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RagEvaluationError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise RagEvaluationError(f"{label} must be a JSON object")
    return parsed, content


def _validated_assessor_packet(
    evaluation: dict[str, Any], assessment: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    packet = evaluation.get("quality_assessor_packet")
    if not isinstance(packet, dict):
        raise RagEvaluationError("evaluation has no Quality Assessor packet")
    payload = packet.get("payload")
    if not isinstance(payload, dict):
        raise RagEvaluationError("Quality Assessor packet has no canonical payload")
    digest = _canonical_digest(payload)
    packet_id = f"pkt_{digest[:20]}"
    if (
        packet.get("schema_version") != 1
        or packet.get("packet_type") != "quality_assessment_input"
        or packet.get("packet_id") != packet_id
        or packet.get("input_sha256") != digest
    ):
        raise RagEvaluationError("Quality Assessor packet hash validation failed")
    if (
        assessment.get("schema_version") != 1
        or assessment.get("packet_type") != "quality_assessment"
        or assessment.get("packet_id") != packet_id
        or assessment.get("input_sha256") != digest
    ):
        raise RagEvaluationError("Quality Assessor output does not match the input")
    instance_id = assessment.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise RagEvaluationError("Quality Assessor output has no instance id")
    status = assessment.get("status")
    if status not in {"pass", "fail"}:
        raise RagEvaluationError("Quality Assessor status must be pass or fail")
    for field in ("findings", "claim_conflicts", "unassessed"):
        if not isinstance(assessment.get(field), list):
            raise RagEvaluationError(f"Quality Assessor {field} must be an array")
    return packet, payload


def accept_rag_baseline(
    config: dict[str, Any],
    *,
    evaluation_path: Path,
    assessment_path: Path,
    apply: bool = False,
    executable: str | None = None,
    index_validator: IndexValidator = _validate_index_evidence,
) -> dict[str, Any]:
    """Validate and optionally record a passed RAG baseline as a Git transaction."""
    vault = load_vault_path(config)
    evaluation, evaluation_bytes = _read_bounded_json(
        evaluation_path, "RAG evaluation", max_bytes=8_000_000
    )
    assessment, assessment_bytes = _read_bounded_json(
        assessment_path, "Quality Assessor output", max_bytes=2_000_000
    )
    packet, payload = _validated_assessor_packet(evaluation, assessment)
    if assessment.get("status") != "pass":
        raise RagEvaluationError("only a passed Quality Assessor output can be recorded")
    if assessment.get("unassessed"):
        raise RagEvaluationError("Quality Assessor output still has unassessed items")
    head = run_git(vault, "rev-parse", "HEAD")
    if head.returncode != 0 or head.stdout.strip() != payload.get("git_commit"):
        raise RagEvaluationError("vault HEAD no longer matches the assessed evaluation")
    if payload.get("mode") != "rag_blind" or not isinstance(
        payload.get("answers_sha256"), str
    ):
        raise RagEvaluationError("only a complete blind assessment can be recorded")
    qmd = executable or shutil.which("qmd")
    packet_index_evidence = payload.get("index_evidence")
    if (
        not isinstance(packet_index_evidence, dict)
        or packet_index_evidence.get("eligible") is not True
        or packet_index_evidence.get("reasons") != []
        or not qmd
        or index_validator(config, qmd) != packet_index_evidence
    ):
        raise RagEvaluationError("qmd index evidence no longer matches the assessment")
    evidence_items = payload.get("evidence")
    if not isinstance(evidence_items, list):
        raise RagEvaluationError("assessor packet contains invalid evidence")
    case_evidence = [
        item
        for item in evidence_items
        if isinstance(item, dict) and item.get("kind") == "rag_case_set"
    ]
    if len(case_evidence) != 1 or not isinstance(case_evidence[0].get("path"), str):
        raise RagEvaluationError("assessor packet must identify one RAG case set")
    case_relative = Path(case_evidence[0]["path"])
    if case_relative.is_absolute() or ".." in case_relative.parts:
        raise RagEvaluationError("assessor packet contains an unsafe RAG case path")
    try:
        cases_path, cases_bytes = _read_safe_vault_file(
            vault, case_relative, label="RAG case set", max_bytes=1_000_000
        )
        current_cases_sha256 = hashlib.sha256(cases_bytes).hexdigest()
    except RagEvaluationError as exc:
        raise RagEvaluationError("RAG case set is no longer available") from exc
    if current_cases_sha256 != payload.get("cases_sha256"):
        raise RagEvaluationError("RAG case set no longer matches the assessment")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise RagEvaluationError("assessor packet contains no RAG cases")
    note_evidence = payload.get("note_evidence")
    if not isinstance(note_evidence, list) or len(note_evidence) > 500:
        raise RagEvaluationError("assessor packet contains invalid note evidence")
    catalog_paths: set[str] = set()
    catalog_excerpt_bytes = 0
    tracked_paths: set[str] = {cases_path.relative_to(vault).as_posix()}
    for note in note_evidence:
        if not isinstance(note, dict) or not isinstance(note.get("path"), str):
            raise RagEvaluationError("assessor packet contains invalid note evidence")
        relative_note = Path(note["path"])
        try:
            _note_path, note_content = _read_safe_vault_file(
                vault,
                relative_note,
                label="RAG note evidence",
                max_bytes=2_000_000,
            )
            note_text = note_content.decode("utf-8")
        except (RagEvaluationError, UnicodeDecodeError) as exc:
            raise RagEvaluationError("RAG note evidence no longer matches the vault") from exc
        expected_excerpt = note_text[:4000]
        catalog_excerpt_bytes += len(expected_excerpt.encode("utf-8"))
        if (
            relative_note.as_posix() in catalog_paths
            or hashlib.sha256(note_content).hexdigest() != note.get("sha256")
            or note.get("excerpt") != expected_excerpt
            or note.get("excerpt_truncated") is not (len(note_text) > 4000)
            or catalog_excerpt_bytes > 2_000_000
        ):
            raise RagEvaluationError("RAG note evidence no longer matches the vault")
        catalog_paths.add(relative_note.as_posix())
        tracked_paths.add(relative_note.as_posix())
    for case in cases:
        if not isinstance(case, dict) or not isinstance(
            case.get("evidence_note_refs"), list
        ):
            raise RagEvaluationError("assessor packet contains invalid note evidence")
        for field in (
            "expected_paths",
            "retrieved_paths",
            "expected_provenance",
            "answer_provenance",
        ):
            values = case.get(field)
            if not isinstance(values, list) or not all(
                isinstance(value, str) for value in values
            ):
                raise RagEvaluationError("assessor packet contains invalid note paths")
            tracked_paths.update(values)
        expected_evidence_refs = {
            path
            for field in (
                "expected_paths",
                "retrieved_paths",
                "expected_provenance",
                "answer_provenance",
            )
            for path in case[field]
        }
        if (
            not all(isinstance(value, str) for value in case["evidence_note_refs"])
            or set(case["evidence_note_refs"]) != expected_evidence_refs
            or not expected_evidence_refs.issubset(catalog_paths)
        ):
            raise RagEvaluationError("RAG case evidence references are incomplete")
        expected = case["expected_paths"]
        retrieved = case["retrieved_paths"]
        thresholds = case.get("thresholds")
        if not isinstance(thresholds, dict) or not expected:
            raise RagEvaluationError("assessor packet contains invalid thresholds")
        try:
            min_recall = float(thresholds["min_recall_at_k"])
            min_rank = float(thresholds["min_reciprocal_rank"])
            stored_recall = float(case["recall_at_k"])
            stored_rank = float(case["reciprocal_rank"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RagEvaluationError("assessor packet contains invalid metrics") from exc
        ranked_hits = [
            index + 1 for index, item in enumerate(retrieved) if item in set(expected)
        ]
        recall = len(ranked_hits) / len(set(expected))
        reciprocal_rank = 1 / ranked_hits[0] if ranked_hits else 0.0
        retrieval_pass = recall >= min_recall and reciprocal_rank >= min_rank
        if (
            stored_recall != recall
            or stored_rank != reciprocal_rank
            or case.get("retrieval_pass") is not retrieval_pass
            or not retrieval_pass
        ):
            raise RagEvaluationError("failed retrieval thresholds cannot be baselined")
    if not _all_paths_tracked(vault, tracked_paths):
        raise RagEvaluationError("RAG baseline evidence must remain Git tracked")

    packet_id = str(packet["packet_id"])
    relative = Path(".vault-pr/quality/rag/baselines") / f"{packet_id}.json"
    destination = vault / relative
    record = {
        "schema_version": 1,
        "record_type": "rag_evaluation_baseline",
        "packet_id": packet_id,
        "input_sha256": packet["input_sha256"],
        "git_commit_evaluated": payload["git_commit"],
        "evaluation_sha256": hashlib.sha256(evaluation_bytes).hexdigest(),
        "assessment_sha256": hashlib.sha256(assessment_bytes).hexdigest(),
        "evaluation": evaluation,
        "quality_assessment": assessment,
    }
    record_bytes = (
        json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    planned = {
        "status": "baseline_ready",
        "path": relative.as_posix(),
        "packet_id": packet_id,
        "record_sha256": hashlib.sha256(record_bytes).hexdigest(),
        "requires_commit": apply,
    }
    if not apply:
        return planned
    if destination.exists() or destination.is_symlink():
        raise RagEvaluationError("RAG baseline record already exists")
    transaction_id = f"txn_quality_{uuid.uuid4().hex[:16]}"
    try:
        with vault_write_lock(vault):
            require_clean_git_root(vault)
            git_dir_result = run_git(vault, "rev-parse", "--absolute-git-dir")
            if git_dir_result.returncode != 0:
                raise RagEvaluationError("unable to resolve vault Git directory")
            active_dir = prepare_active_transaction(
                vault=vault,
                git_dir=Path(git_dir_result.stdout.strip()),
                transaction_id=transaction_id,
                transaction_sha256=str(packet["input_sha256"]),
                affected={destination},
                expected_original_sha256={destination: None},
                expected_original_mode={destination: None},
                final_sha256={destination: planned["record_sha256"]},
                final_mode={destination: "100644"},
            )
            try:
                staging = active_dir / "write-staging" / "baseline.json"
                staging.parent.mkdir(parents=True)
                staging.write_bytes(record_bytes)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, destination)
                mark_active_applied(active_dir, completion_path=relative.as_posix())
            except Exception:
                rollback_active_transaction(vault, active_dir)
                raise
    except (GitSafetyError, RecoveryError) as exc:
        raise RagEvaluationError(str(exc)) from exc
    return {
        **planned,
        "status": "baseline_applied",
        "transaction_id": transaction_id,
        "requires_commit": True,
    }
