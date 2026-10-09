"""Retrieve related vault notes for semantic integration planning."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from .capture import CaptureError, load_validated_capture_artifact_text
from .config import (
    index_spec,
    load_vault_path,
    retrieval_mode,
    source_archive_dir,
)
from .errors import IngestError
from .index import IndexUpdateError, validate_qmd_index_freshness
from .source_note import resolve_source_note
from .tooling import QMD_PATH_PRESERVING_VERSION_OUTPUTS


class RetrievalError(RuntimeError):
    """Raised when related-note retrieval cannot be completed."""


Runner = Callable[..., subprocess.CompletedProcess[str]]
IndexValidator = Callable[..., dict[str, Any]]


def _source_artifacts(
    config: dict[str, Any], source_id: str
) -> tuple[dict[str, Any], str]:
    if not re.fullmatch(r"src_[0-9a-f]{20}", source_id):
        raise RetrievalError("invalid source id")
    vault = load_vault_path(config)
    capture = source_archive_dir(config, vault) / source_id
    try:
        return load_validated_capture_artifact_text(
            capture,
            source_id=source_id,
            vault=vault,
            artifact="extracted",
        )
    except CaptureError as exc:
        raise RetrievalError(f"source capture integrity failed: {exc}") from exc


def _retrieval_query(title: str, extracted: str) -> str:
    headings = [
        re.sub(r"^#+\s*", "", line).strip()
        for line in extracted.splitlines()
        if re.match(r"^#{1,3}\s+", line)
    ][:12]
    body = re.sub(r"[`*_>#\[\]()]", " ", extracted[:1200])
    body = " ".join(body.split())
    return " ".join([title, *headings, body])[:1800]


def parse_qmd_results(output: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        start = output.find("[")
        if start < 0:
            raise RetrievalError("qmd did not return JSON results") from None
        try:
            parsed = json.loads(output[start:])
        except json.JSONDecodeError as exc:
            raise RetrievalError("qmd returned invalid JSON results") from exc
    if not isinstance(parsed, list):
        raise RetrievalError("qmd result must be a JSON array")
    return [item for item in parsed if isinstance(item, dict)]


def qmd_retrieval_strategy(config: dict[str, Any]) -> dict[str, Any]:
    """Describe the stable qmd retrieval contract for machine-readable output."""
    mode = retrieval_mode(config)
    if mode == "lexical":
        return {
            "retrieval_mode": mode,
            "fusion_strategy": "none",
            "score_kind": "bm25",
            "query_types": ["lex"],
            "chunk_strategy": None,
            "rerank": False,
        }
    return {
        "retrieval_mode": mode,
        "fusion_strategy": "qmd_rrf",
        "score_kind": "qmd_rrf",
        "query_types": ["lex", "vec"],
        "chunk_strategy": "regex",
        "rerank": False,
    }


def _qmd_typed_query_text(query: str) -> str:
    """Render untrusted prose as one grammar-safe qmd typed-query line."""
    normalized = " ".join(query.split()).replace('"', " ")
    without_negation = re.sub(r"(^|\s)-(?=[\w'])", r"\1", normalized)
    return " ".join(without_negation.split())


def build_qmd_retrieval_command(
    config: dict[str, Any],
    *,
    executable: str,
    index_name: str,
    collection: str,
    query: str,
    limit: int,
) -> list[str]:
    """Build one qmd request without comparing scores from different scales."""
    normalized_query = _qmd_typed_query_text(query)
    mode = retrieval_mode(config)
    if mode == "lexical":
        command = [executable, "--index", index_name, "search", normalized_query]
    else:
        structured_query = f"lex: {normalized_query}\nvec: {normalized_query}"
        command = [executable, "--index", index_name, "query", structured_query]
    command.extend(["--collection", collection, "-n", str(limit)])
    if mode == "hybrid":
        command.extend(
            [
                "--candidate-limit",
                str(max(40, limit)),
                "--no-rerank",
                "--chunk-strategy",
                "regex",
            ]
        )
    command.extend(["--format", "json"])
    return command


def _qmd_handelize(path: str) -> str:
    """Mirror qmd's token-friendly virtual-path normalization."""

    def emoji_to_hex(value: str) -> str:
        rendered: list[str] = []
        index = 0
        while index < len(value):
            category = unicodedata.category(value[index])
            if category not in {"So", "Sk"}:
                rendered.append(value[index])
                index += 1
                continue

            codepoints: list[str] = []
            while index < len(value):
                category = unicodedata.category(value[index])
                if category == "So":
                    codepoints.append(format(ord(value[index]), "x"))
                    index += 1
                    if (
                        index < len(value)
                        and unicodedata.category(value[index]) == "Mn"
                    ):
                        index += 1
                elif category == "Sk":
                    codepoints.append(format(ord(value[index]), "x"))
                    index += 1
                else:
                    break
            rendered.append("-".join(codepoints))
        return "".join(rendered)

    def normalize_segment(segment: str, *, preserve_extension: bool) -> str:
        extension = ""
        stem = segment
        if preserve_extension:
            match = re.search(r"(\.[a-z0-9]+)$", segment, flags=re.IGNORECASE)
            if match:
                extension = match.group(1)
                stem = segment[: -len(extension)]

        expanded = emoji_to_hex(stem)
        normalized = re.sub(
            r"-+",
            "-",
            "".join(
                character
                if unicodedata.category(character)[0] in {"L", "N"} or character == "$"
                else "-"
                for character in expanded
            ),
        ).strip("-")
        return normalized + extension

    segments = path.replace("___", "/").split("/")
    normalized = [
        normalize_segment(segment, preserve_extension=index == len(segments) - 1)
        for index, segment in enumerate(segments)
        if segment
    ]
    return "/".join(segment for segment in normalized if segment)


def build_qmd_path_map(
    root_dir: Path, *, preserve_paths: bool = False
) -> dict[str, list[Path]]:
    """Map qmd virtual paths back to exact files, retaining collisions."""
    ignored_dirs = {"node_modules", ".git", ".cache", "vendor", "dist", "build"}
    mapped: dict[str, list[Path]] = defaultdict(list)
    for candidate in root_dir.rglob("*.md"):
        relative = candidate.relative_to(root_dir)
        if any(part.startswith(".") or part in ignored_dirs for part in relative.parts):
            continue
        resolved = candidate.resolve()
        if not resolved.is_file() or not resolved.is_relative_to(root_dir):
            continue
        key = relative.as_posix() if preserve_paths else _qmd_handelize(relative.as_posix())
        mapped[key].append(resolved)
    return dict(mapped)


def resolve_qmd_path(
    root_dir: Path, collection_relative: str, path_map: dict[str, list[Path]]
) -> Path:
    parts = Path(collection_relative).parts
    if not collection_relative or any(part in {".", ".."} for part in parts):
        raise RetrievalError("qmd result contains an invalid collection path")
    matches = path_map.get(collection_relative, [])
    if not matches:
        raise RetrievalError("qmd result does not map to an indexed vault file")
    if len(matches) > 1:
        raise RetrievalError("qmd normalized path is ambiguous in the vault")
    candidate = matches[0]
    if not candidate.is_relative_to(root_dir):
        raise RetrievalError("qmd result path escapes the configured index root")
    return candidate


def retrieve_related_notes(
    config: dict[str, Any],
    source_id: str,
    *,
    limit: int = 10,
    runner: Runner = subprocess.run,
    executable: str | None = None,
    index_validator: IndexValidator = validate_qmd_index_freshness,
) -> dict[str, Any]:
    """Retrieve ranked candidates using the configured stable qmd mode."""
    if limit < 1 or limit > 50:
        raise RetrievalError("limit must be between 1 and 50")
    manifest, extracted = _source_artifacts(config, source_id)
    vault = load_vault_path(config)
    spec = index_spec(config, vault)

    qmd = executable or shutil.which("qmd")
    if not qmd:
        raise RetrievalError("required tool missing: qmd")
    try:
        index_freshness = index_validator(config, executable=qmd)
    except IndexUpdateError as exc:
        raise RetrievalError(f"qmd index freshness validation failed: {exc}") from exc
    if not index_freshness.get("eligible"):
        raise RetrievalError(
            "qmd index is stale: "
            + ", ".join(index_freshness.get("reasons", ["unknown"]))
        )
    index_name = config.get("index", {}).get("name", "notebook")
    query = _retrieval_query(str(manifest.get("title", "")), extracted)
    try:
        source_note = str(resolve_source_note(config, vault, source_id).relative_to(vault))
    except IngestError as exc:
        raise RetrievalError(str(exc)) from exc
    qmd_version = index_freshness.get("qmd", {}).get("version")
    path_map = build_qmd_path_map(
        spec.root_dir,
        preserve_paths=qmd_version in QMD_PATH_PRESERVING_VERSION_OUTPUTS,
    )

    strategy = qmd_retrieval_strategy(config)
    command = build_qmd_retrieval_command(
        config,
        executable=qmd,
        index_name=str(index_name),
        collection=spec.collection,
        query=query,
        limit=limit,
    )
    result = runner(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RetrievalError(f"qmd {strategy['retrieval_mode']} retrieval failed: {detail}")

    candidates: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for item in parse_qmd_results(result.stdout):
        file_name = str(item.get("file", ""))
        if not file_name:
            continue
        parsed_file = urlsplit(file_name)
        if parsed_file.scheme != "qmd" or parsed_file.netloc != spec.collection:
            raise RetrievalError("qmd result is outside the configured collection")
        collection_relative = unquote(parsed_file.path).lstrip("/")
        candidate_path = resolve_qmd_path(spec.root_dir, collection_relative, path_map)
        vault_relative = str(candidate_path.relative_to(vault))
        if vault_relative == source_note or vault_relative in seen_paths:
            continue
        try:
            score = float(item.get("score", 0))
        except (TypeError, ValueError) as exc:
            raise RetrievalError("qmd result contains an invalid score") from exc
        seen_paths.add(vault_relative)
        candidates.append(
            {
                "file": file_name,
                "path": vault_relative,
                "title": item.get("title"),
                "snippet": item.get("snippet"),
                "score": score,
                "score_kind": strategy["score_kind"],
            }
        )
        if len(candidates) == limit:
            break

    return {
        "source_id": source_id,
        "query": query,
        "collection": spec.collection,
        "retrieval_mode": strategy["retrieval_mode"],
        "fusion_strategy": strategy["fusion_strategy"],
        "query_types": strategy["query_types"],
        "chunk_strategy": strategy["chunk_strategy"],
        "rerank": strategy["rerank"],
        "index_freshness": index_freshness,
        "candidates": candidates,
    }
