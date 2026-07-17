"""Vault-owned RAG retrieval evaluation and assessor-packet preparation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from .config import ConfigError, index_spec, load_vault_path, vault_relative_path
from .git import GitSafetyError, run_git, vault_write_lock
from .index import IndexUpdateError, validate_qmd_index_freshness
from .mutation import (
    MutationError,
    execute_journaled_mutation,
    inspect_journaled_mutation,
    mutation_response,
)
from .rag_contract import (
    RAG_CASE_ID,
    RagContractError,
    answers_sha256,
    canonical_digest,
    validated_assessor_packet,
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
RAG_BASELINE_SCHEMA_VERSION = 2


def _validate_index_evidence(
    config: dict[str, Any], executable: str
) -> dict[str, Any]:
    try:
        return validate_qmd_index_freshness(
            config,
            executable=executable,
        )
    except IndexUpdateError as exc:
        return {
            "schema_version": 1,
            "eligible": False,
            "reasons": ["index_freshness_validation_failed"],
            "error": str(exc),
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
        if not isinstance(case_id, str) or not RAG_CASE_ID.fullmatch(case_id):
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
    return answers, answers_sha256(
        [
            {
                "case_id": case_id,
                "answer": item["answer"],
                "answer_provenance": item["provenance"],
            }
            for case_id, item in answers.items()
        ]
    )
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
    try:
        with vault_write_lock(vault):
            index_evidence = index_validator(config, qmd)
            for case in cases:
                retrieved = _query_paths(
                    config,
                    case["query"],
                    limit=limit,
                    runner=runner,
                    executable=qmd,
                )
                expected = set(case["expected_paths"])
                ranked_hits = [
                    index + 1
                    for index, item in enumerate(retrieved)
                    if item in expected
                ]
                recall = len(ranked_hits) / len(expected)
                reciprocal_rank = 1 / ranked_hits[0] if ranked_hits else 0.0
                thresholds = case["thresholds"]
                retrieval_pass = (
                    recall >= thresholds["min_recall_at_k"]
                    and reciprocal_rank
                    >= thresholds["min_reciprocal_rank"]
                )
                answer = answers.get(str(case["case_id"]))
                evidence_paths = (
                    expected
                    | set(retrieved)
                    | set(case["expected_provenance"])
                    | set(answer["provenance"] if answer else [])
                )
                results.append(
                    {
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
                        "answer_provenance": (
                            answer["provenance"] if answer else []
                        ),
                    }
                )
    except GitSafetyError as exc:
        raise RagEvaluationError(str(exc)) from exc
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
    required_judgments = (
        [
            {"case_id": item["case_id"], "kind": kind}
            for item in results
            for kind in ("answer_claims", "provenance")
        ]
        if answers
        else []
    )
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
    packet_digest = canonical_digest(packet_payload)
    evaluation = {
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
    }
    if answers:
        evaluation["quality_assessor_packet"] = {
            "schema_version": 1,
            "packet_type": "quality_assessment_input",
            "packet_id": f"pkt_{packet_digest[:20]}",
            "created_at": generated_at,
            "input_sha256": packet_digest,
            "mode": mode,
            "payload": packet_payload,
        }
    else:
        evaluation["rag_discovery"] = {
            "schema_version": 1,
            "diagnostic_type": "rag_discovery",
            "diagnostic_sha256": packet_digest,
            "payload": packet_payload,
        }
    return evaluation


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


def _validate_rag_evaluation_projection(
    evaluation: dict[str, Any],
    packet: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    expected_fields = {
        "schema_version",
        "assessment",
        "generated_at",
        "git_commit",
        "git_clean",
        "assessment_candidate_eligible",
        "baseline_recorded",
        "cases_sha256",
        "answers_sha256",
        "index_evidence",
        "expected_paths_tracked",
        "retrieved_paths_tracked",
        "provenance_paths_tracked",
        "semantic_assessment_ready",
        "limit",
        "metrics",
        "quality_assessor_packet",
    }
    metrics = evaluation.get("metrics")
    cases = payload["cases"]
    expected_metrics = {
        "cases": len(cases),
        "retrieval_passed": sum(item["retrieval_pass"] for item in cases),
        "mean_recall_at_k": sum(item["recall_at_k"] for item in cases)
        / len(cases),
        "mean_reciprocal_rank": sum(
            item["reciprocal_rank"] for item in cases
        )
        / len(cases),
    }
    expected_candidate_eligible = (
        evaluation.get("git_clean") is True
        and evaluation.get("expected_paths_tracked") is True
        and evaluation.get("retrieved_paths_tracked") is True
        and evaluation.get("provenance_paths_tracked") is True
        and payload["index_evidence"].get("eligible") is True
        and evaluation.get("semantic_assessment_ready") is True
        and all(item["retrieval_pass"] for item in cases)
    )
    if (
        set(evaluation) != expected_fields
        or evaluation.get("schema_version") != 1
        or evaluation.get("assessment") != "rag"
        or evaluation.get("generated_at") != packet["created_at"]
        or evaluation.get("git_commit") != payload["git_commit"]
        or not isinstance(evaluation.get("git_clean"), bool)
        or evaluation.get("assessment_candidate_eligible")
        is not expected_candidate_eligible
        or evaluation.get("baseline_recorded") is not False
        or evaluation.get("cases_sha256") != payload["cases_sha256"]
        or evaluation.get("answers_sha256") != payload["answers_sha256"]
        or evaluation.get("index_evidence") != payload["index_evidence"]
        or not isinstance(evaluation.get("expected_paths_tracked"), bool)
        or not isinstance(evaluation.get("retrieved_paths_tracked"), bool)
        or not isinstance(evaluation.get("provenance_paths_tracked"), bool)
        or evaluation.get("semantic_assessment_ready") is not True
        or evaluation.get("limit") != payload["limit"]
        or metrics != expected_metrics
        or evaluation.get("quality_assessor_packet") != packet
    ):
        raise RagEvaluationError(
            "RAG evaluation projection is incomplete or inconsistent"
        )
    if expected_candidate_eligible is not True:
        raise RagEvaluationError(
            "RAG evaluation is not eligible for baseline acceptance"
        )


def _validate_rag_baseline_inputs(
    config: dict[str, Any],
    vault: Path,
    payload: dict[str, Any],
    destination: Path,
    *,
    executable: str | None,
    index_validator: IndexValidator,
) -> None:
    """Revalidate every authoritative RAG input against current vault state."""
    head = run_git(vault, "rev-parse", "HEAD")
    if head.returncode != 0 or head.stdout.strip() != payload["git_commit"]:
        raise RagEvaluationError(
            "vault HEAD no longer matches the assessed evaluation"
        )
    status = run_git(vault, "status", "--porcelain=v1")
    if status.returncode != 0 or status.stdout:
        raise RagEvaluationError(
            "vault working tree no longer matches the assessed evaluation"
        )
    qmd = executable or shutil.which("qmd")
    packet_index_evidence = payload["index_evidence"]
    if (
        packet_index_evidence.get("eligible") is not True
        or packet_index_evidence.get("reasons") != []
        or not qmd
        or index_validator(config, qmd) != packet_index_evidence
    ):
        raise RagEvaluationError(
            "qmd index evidence no longer matches the assessment"
        )

    case_item = next(
        item for item in payload["evidence"] if item["kind"] == "rag_case_set"
    )
    case_relative = Path(case_item["path"])
    if case_relative.is_absolute() or ".." in case_relative.parts:
        raise RagEvaluationError(
            "assessor packet contains an unsafe RAG case path"
        )
    try:
        cases_path, cases_bytes = _read_safe_vault_file(
            vault,
            case_relative,
            label="RAG case set",
            max_bytes=1_000_000,
        )
        _authoritative_path, authoritative_cases = _load_cases(
            config,
            vault,
            case_relative,
        )
    except (ConfigError, RagEvaluationError) as exc:
        raise RagEvaluationError(
            "RAG case set is no longer available"
        ) from exc
    if hashlib.sha256(cases_bytes).hexdigest() != payload["cases_sha256"]:
        raise RagEvaluationError(
            "RAG case set no longer matches the assessment"
        )
    authoritative_by_id = {
        item["case_id"]: item for item in authoritative_cases
    }
    packet_cases = payload["cases"]
    if {item["case_id"] for item in packet_cases} != set(authoritative_by_id):
        raise RagEvaluationError(
            "RAG packet cases do not match the authoritative case set"
        )
    authoritative_fields = (
        "query",
        "expected_paths",
        "expected_claims",
        "expected_provenance",
        "thresholds",
    )
    for case in packet_cases:
        authoritative = authoritative_by_id[case["case_id"]]
        if any(
            case[field] != authoritative[field]
            for field in authoritative_fields
        ):
            raise RagEvaluationError(
                "RAG packet case semantics do not match the authoritative case set"
            )
    if answers_sha256(packet_cases) != payload["answers_sha256"]:
        raise RagEvaluationError(
            "RAG packet answers do not match their semantic digest"
        )

    note_evidence = payload["note_evidence"]
    catalog_paths: set[str] = set()
    catalog_excerpt_bytes = 0
    tracked_paths: set[str] = {cases_path.relative_to(vault).as_posix()}
    for note in note_evidence:
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
            raise RagEvaluationError(
                "RAG note evidence no longer matches the vault"
            ) from exc
        expected_excerpt = note_text[:4000]
        catalog_excerpt_bytes += len(expected_excerpt.encode("utf-8"))
        if (
            relative_note.as_posix() in catalog_paths
            or hashlib.sha256(note_content).hexdigest() != note["sha256"]
            or note["excerpt"] != expected_excerpt
            or note["excerpt_truncated"] is not (len(note_text) > 4000)
            or catalog_excerpt_bytes > 2_000_000
        ):
            raise RagEvaluationError(
                "RAG note evidence no longer matches the vault"
            )
        catalog_paths.add(relative_note.as_posix())
        tracked_paths.add(relative_note.as_posix())

    for case in packet_cases:
        for field in (
            "expected_paths",
            "retrieved_paths",
            "expected_provenance",
            "answer_provenance",
        ):
            tracked_paths.update(case[field])
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
            set(case["evidence_note_refs"]) != expected_evidence_refs
            or not expected_evidence_refs.issubset(catalog_paths)
        ):
            raise RagEvaluationError(
                "RAG case evidence references are incomplete"
            )
        expected = case["expected_paths"]
        retrieved = case["retrieved_paths"]
        thresholds = case["thresholds"]
        ranked_hits = [
            index + 1
            for index, item in enumerate(retrieved)
            if item in set(expected)
        ]
        recall = len(ranked_hits) / len(set(expected))
        reciprocal_rank = 1 / ranked_hits[0] if ranked_hits else 0.0
        retrieval_pass = (
            recall >= thresholds["min_recall_at_k"]
            and reciprocal_rank >= thresholds["min_reciprocal_rank"]
        )
        if (
            case["recall_at_k"] != recall
            or case["reciprocal_rank"] != reciprocal_rank
            or case["retrieval_pass"] is not retrieval_pass
            or not retrieval_pass
        ):
            raise RagEvaluationError(
                "failed retrieval thresholds cannot be baselined"
            )
    if not _all_paths_tracked(vault, tracked_paths):
        raise RagEvaluationError(
            "RAG baseline evidence must remain Git tracked"
        )
    if destination.exists() or destination.is_symlink():
        raise RagEvaluationError("RAG baseline record already exists")


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
    try:
        packet, payload = validated_assessor_packet(evaluation, assessment)
    except RagContractError as exc:
        raise RagEvaluationError(str(exc)) from exc
    _validate_rag_evaluation_projection(evaluation, packet, payload)
    if assessment.get("status") != "pass":
        raise RagEvaluationError("only a passed Quality Assessor output can be recorded")
    if assessment.get("unassessed"):
        raise RagEvaluationError("Quality Assessor output still has unassessed items")
    packet_id = str(packet["packet_id"])
    relative = Path(".vault-pr/quality/rag/baselines") / f"{packet_id}.json"
    destination = vault / relative
    record = {
        "schema_version": RAG_BASELINE_SCHEMA_VERSION,
        "record_type": "rag_evaluation_baseline",
        "packet_id": packet_id,
        "input_sha256": packet["input_sha256"],
        "git_commit_evaluated": payload["git_commit"],
        "evaluation_sha256": hashlib.sha256(evaluation_bytes).hexdigest(),
        "assessment_sha256": hashlib.sha256(assessment_bytes).hexdigest(),
        "quality_assessor_packet": packet,
        "quality_assessment": assessment,
    }
    record_bytes = (
        json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    planned = {
        "path": relative.as_posix(),
        "packet_id": packet_id,
        "record_sha256": hashlib.sha256(record_bytes).hexdigest(),
    }
    transaction_sha256 = str(packet["input_sha256"])
    request_sha256 = canonical_digest(
        {
            "evaluation_sha256": record["evaluation_sha256"],
            "assessment_sha256": record["assessment_sha256"],
        }
    )
    transaction_id = f"txn_rag_{transaction_sha256[:16]}"
    git_dir_result = run_git(vault, "rev-parse", "--absolute-git-dir")
    if git_dir_result.returncode != 0:
        raise RagEvaluationError("unable to resolve vault Git directory")
    git_dir = Path(git_dir_result.stdout.strip())
    if apply:
        try:
            existing = inspect_journaled_mutation(
                vault=vault,
                git_dir=git_dir,
                operation="rag_baseline",
                transaction_id=transaction_id,
                transaction_sha256=transaction_sha256,
                request_sha256=request_sha256,
            )
        except MutationError as exc:
            raise RagEvaluationError(str(exc)) from exc
        if existing is not None:
            if existing["status"] == "already_committed" and (
                not destination.is_file()
                or destination.read_bytes() != record_bytes
            ):
                raise RagEvaluationError(
                    "committed RAG baseline integrity validation failed"
                )
            return mutation_response(existing, applied_status="baseline_applied")
    if not apply:
        _validate_rag_baseline_inputs(
            config,
            vault,
            payload,
            destination,
            executable=executable,
            index_validator=index_validator,
        )
        return {**planned, "status": "baseline_ready", "requires_commit": False}

    def write(active_dir: Path) -> None:
        staging = active_dir / "write-staging" / "baseline.json"
        staging.parent.mkdir(parents=True)
        staging.write_bytes(record_bytes)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, destination)

    try:
        outcome = execute_journaled_mutation(
            vault=vault,
            git_dir=git_dir,
            transaction_id=transaction_id,
            transaction_sha256=transaction_sha256,
            request_sha256=request_sha256,
            operation="rag_baseline",
            receipt_data=planned,
            affected={destination},
            expected_original_sha256={destination: None},
            expected_original_mode={destination: None},
            final_sha256={destination: planned["record_sha256"]},
            final_mode={destination: "100644"},
            write=write,
            precondition=lambda: _validate_rag_baseline_inputs(
                config,
                vault,
                payload,
                destination,
                executable=executable,
                index_validator=index_validator,
            ),
            completion_path=relative.as_posix(),
        )
    except (GitSafetyError, MutationError) as exc:
        raise RagEvaluationError(str(exc)) from exc
    return mutation_response(outcome, applied_status="baseline_applied")
