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

from .capture import CaptureError, load_validated_capture
from .config import index_spec, load_vault_path, source_archive_dir


class RetrievalError(RuntimeError):
    """Raised when related-note retrieval cannot be completed."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _source_artifacts(
    config: dict[str, Any], source_id: str
) -> tuple[dict[str, Any], str]:
    if not re.fullmatch(r"src_[0-9a-f]{20}", source_id):
        raise RetrievalError("invalid source id")
    vault = load_vault_path(config)
    capture = source_archive_dir(config, vault) / source_id
    try:
        manifest = load_validated_capture(capture, source_id=source_id, vault=vault)
    except CaptureError as exc:
        raise RetrievalError(f"source capture integrity failed: {exc}") from exc
    artifacts = manifest["artifacts"]
    extracted_path = capture / artifacts["extracted"]
    return manifest, extracted_path.read_text(encoding="utf-8")


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


def build_qmd_path_map(root_dir: Path) -> dict[str, list[Path]]:
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
        mapped[_qmd_handelize(relative.as_posix())].append(resolved)
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
) -> dict[str, Any]:
    """Combine BM25 and vector candidates across the configured vault index."""
    if limit < 1 or limit > 50:
        raise RetrievalError("limit must be between 1 and 50")
    manifest, extracted = _source_artifacts(config, source_id)
    vault = load_vault_path(config)
    spec = index_spec(config, vault)

    qmd = executable or shutil.which("qmd")
    if not qmd:
        raise RetrievalError("required tool missing: qmd")
    index_name = config.get("index", {}).get("name", "notebook")
    query = _retrieval_query(str(manifest.get("title", "")), extracted)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise RetrievalError("source manifest is missing artifacts")
    source_note = artifacts.get("source_note")
    path_map = build_qmd_path_map(spec.root_dir)

    merged: dict[str, dict[str, Any]] = {}
    for method in ("search", "vsearch"):
        result = runner(
            [
                qmd,
                "--index",
                str(index_name),
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
            raise RetrievalError(f"qmd {method} failed: {detail}")
        for item in parse_qmd_results(result.stdout):
            file_name = str(item.get("file", ""))
            if not file_name:
                continue
            parsed_file = urlsplit(file_name)
            if parsed_file.scheme != "qmd" or parsed_file.netloc != spec.collection:
                raise RetrievalError("qmd result is outside the configured collection")
            collection_relative = unquote(parsed_file.path).lstrip("/")
            candidate_path = resolve_qmd_path(
                spec.root_dir, collection_relative, path_map
            )
            vault_relative = str(candidate_path.relative_to(vault))
            if isinstance(source_note, str) and vault_relative == source_note:
                continue
            try:
                score = float(item.get("score", 0))
            except (TypeError, ValueError) as exc:
                raise RetrievalError("qmd result contains an invalid score") from exc
            entry = merged.setdefault(
                file_name,
                {
                    "file": file_name,
                    "path": vault_relative,
                    "title": item.get("title"),
                    "snippet": item.get("snippet"),
                    "score": score,
                    "methods": [],
                },
            )
            entry["score"] = max(entry["score"], score)
            entry["methods"].append(method)

    candidates = sorted(
        merged.values(), key=lambda item: (-item["score"], item["file"])
    )[:limit]
    return {
        "source_id": source_id,
        "query": query,
        "collection": spec.collection,
        "candidates": candidates,
    }
