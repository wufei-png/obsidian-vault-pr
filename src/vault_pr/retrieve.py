"""Retrieve related vault notes for semantic integration planning."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from .capture import CaptureError, load_validated_capture
from .config import ConfigError, domain_specs, load_vault_path, source_archive_dir


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


def _parse_results(output: str) -> list[dict[str, Any]]:
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


def retrieve_related_notes(
    config: dict[str, Any],
    source_id: str,
    *,
    limit: int = 10,
    runner: Runner = subprocess.run,
    executable: str | None = None,
) -> dict[str, Any]:
    """Combine BM25 and vector candidates within the source's semantic domain."""
    if limit < 1 or limit > 50:
        raise RetrievalError("limit must be between 1 and 50")
    manifest, extracted = _source_artifacts(config, source_id)
    domain = manifest.get("domain")
    if not isinstance(domain, str):
        raise RetrievalError("source manifest is missing domain")
    vault = load_vault_path(config)
    specs = domain_specs(config, vault)
    spec = specs.get(domain)
    if not spec or not spec.qmd_collection:
        raise ConfigError(f"domain has no qmd collection: {domain}")

    qmd = executable or shutil.which("qmd")
    if not qmd:
        raise RetrievalError("required tool missing: qmd")
    index_name = config.get("index", {}).get("name", "notebook")
    query = _retrieval_query(str(manifest.get("title", "")), extracted)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise RetrievalError("source manifest is missing artifacts")
    source_note = artifacts.get("source_note")

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
                spec.qmd_collection,
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
        for item in _parse_results(result.stdout):
            file_name = str(item.get("file", ""))
            if not file_name:
                continue
            parsed_file = urlsplit(file_name)
            if parsed_file.scheme != "qmd" or parsed_file.netloc != spec.qmd_collection:
                raise RetrievalError("qmd result is outside the configured collection")
            collection_relative = unquote(parsed_file.path).lstrip("/")
            candidate_path = (spec.root_dir / collection_relative).resolve()
            if not candidate_path.is_relative_to(spec.root_dir):
                raise RetrievalError("qmd result path escapes the configured domain")
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
        "domain": domain,
        "query": query,
        "collection": spec.qmd_collection,
        "candidates": candidates,
    }
