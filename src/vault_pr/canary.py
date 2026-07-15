"""Disposable capture-route canaries for real URLs and local snapshots."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from .git import run_git
from .index import update_qmd_index
from .ingest import ingest_url
from .recovery import commit_applied_transaction
from .retrieve import (
    RetrievalError,
    build_qmd_path_map,
    parse_qmd_results,
    resolve_qmd_path,
    retrieve_related_notes,
)


class CanaryError(RuntimeError):
    """Raised when a disposable route canary cannot complete safely."""


Indexer = Callable[[dict[str, Any]], dict[str, Any]]
Retriever = Callable[[dict[str, Any], str], dict[str, Any]]
Cleaner = Callable[[str, str], None]
Probe = Callable[[dict[str, Any], str, str], int]


def _checked_git(vault: Path, *args: str) -> str:
    result = run_git(vault, *args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise CanaryError(f"fixture Git command failed: {detail}")
    return result.stdout.strip()


def _cleanup_qmd_collection(index_name: str, collection: str) -> None:
    qmd = shutil.which("qmd")
    if not qmd:
        raise CanaryError("qmd disappeared before disposable collection cleanup")
    shown = subprocess.run(
        [qmd, "--index", index_name, "collection", "show", collection],
        check=False,
        capture_output=True,
        text=True,
    )
    if shown.returncode != 0 and "Collection not found" in (
        shown.stderr or shown.stdout
    ):
        return
    if shown.returncode != 0:
        raise CanaryError("unable to inspect disposable qmd collection")
    removed = subprocess.run(
        [qmd, "--index", index_name, "collection", "remove", collection],
        check=False,
        capture_output=True,
        text=True,
    )
    if removed.returncode != 0:
        raise CanaryError("unable to remove disposable qmd collection")


def _index(config: dict[str, Any]) -> dict[str, Any]:
    return update_qmd_index(config)


def _retrieve(config: dict[str, Any], source_id: str) -> dict[str, Any]:
    return retrieve_related_notes(config, source_id, limit=5)


def _probe_source_note(
    config: dict[str, Any], source_id: str, source_note: str
) -> int:
    qmd = shutil.which("qmd")
    if not qmd:
        raise CanaryError("required tool missing: qmd")
    index = config["index"]
    result = subprocess.run(
        [
            qmd,
            "--index",
            str(index["name"]),
            "search",
            source_id,
            "-c",
            str(index["collection"]),
            "-n",
            "5",
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise CanaryError(f"source-note index probe failed: {detail}")
    try:
        vault = Path(str(config["vault"]["path"])).resolve()
        collection = str(index["collection"])
        path_map = build_qmd_path_map(vault)
        matches = 0
        for item in parse_qmd_results(result.stdout):
            parsed = urlsplit(str(item.get("file", "")))
            if parsed.scheme != "qmd" or parsed.netloc != collection:
                continue
            resolved = resolve_qmd_path(
                vault, unquote(parsed.path).lstrip("/"), path_map
            )
            if resolved.relative_to(vault).as_posix() == source_note:
                matches += 1
        return matches
    except RetrievalError as exc:
        raise CanaryError("source-note index probe returned invalid results") from exc


def run_capture_route_canary(
    *,
    url: str,
    adapter: str = "auto",
    snapshot_path: Path | None = None,
    title: str | None = None,
    indexer: Indexer = _index,
    retriever: Retriever = _retrieve,
    cleaner: Cleaner = _cleanup_qmd_collection,
    probe: Probe = _probe_source_note,
) -> dict[str, Any]:
    """Exercise capture, commit, index, and retrieval in a disposable Fixture Vault."""
    token = uuid.uuid4().hex[:12]
    index_name = "vault-pr-canary"
    collection = f"canary-{token}"
    index_started = False
    primary_error: Exception | None = None
    result: dict[str, Any] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="vault-pr-canary-") as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            _checked_git(vault, "init", "-q")
            _checked_git(vault, "config", "user.name", "Vault PR Canary")
            _checked_git(vault, "config", "user.email", "canary@example.invalid")
            seed = vault / "fixture-concept.md"
            seed.write_text(
                "---\ntype: concept\n---\n\n# Fixture concept\n\n"
                "A synthetic note used only for route verification.\n",
                encoding="utf-8",
            )
            _checked_git(vault, "add", "--", seed.name)
            _checked_git(vault, "commit", "-qm", "Seed disposable fixture vault")
            config: dict[str, Any] = {
                "vault": {"path": str(vault)},
                "sources": {
                    "public_archive_dir": ".vault-pr/sources/public",
                    "visible_notes_dir": "sources",
                },
                "notes": {
                    "include": ["**/*.md"],
                    "exclude": [".git/**", ".obsidian/**", ".vault-pr/**"],
                },
                "index": {
                    "name": index_name,
                    "collection": collection,
                    "context": "Disposable Vault PR capture-route canary.",
                    "mask": "**/*.md",
                    "embed": True,
                    "max_retries": 0,
                },
            }
            captured = ingest_url(
                config=config,
                url=url,
                apply=True,
                title_override=title,
                snapshot_path=snapshot_path,
                adapter=adapter,
            )
            transaction_id = captured.get("transaction_id")
            source_id = captured.get("source_id")
            if not isinstance(transaction_id, str) or not isinstance(source_id, str):
                raise CanaryError("capture route did not create a commit transaction")
            capture_relative = captured.get("capture_dir")
            if not isinstance(capture_relative, str):
                raise CanaryError("capture route did not return its manifest path")
            manifest_path = vault / capture_relative / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise CanaryError("capture route manifest is unreadable") from exc
            actual_adapter = manifest.get("adapter")
            if not isinstance(actual_adapter, str):
                raise CanaryError("capture route manifest has no adapter")
            artifacts = manifest.get("artifacts")
            source_note = artifacts.get("source_note") if isinstance(artifacts, dict) else None
            if not isinstance(source_note, str):
                raise CanaryError("capture route manifest has no source note")
            git_dir = Path(_checked_git(vault, "rev-parse", "--absolute-git-dir"))
            committed = commit_applied_transaction(
                vault,
                git_dir,
                transaction_id,
                "Capture disposable route canary source",
            )
            index_started = True
            indexed = indexer(config)
            if indexed.get("status") != "indexed":
                raise CanaryError("capture route index did not complete")
            indexed_source_hits = probe(config, source_id, source_note)
            if indexed_source_hits < 1:
                raise CanaryError("qmd did not retrieve the committed source note")
            retrieved = retriever(config, source_id)
            candidates = retrieved.get("candidates")
            if not isinstance(candidates, list):
                raise CanaryError("capture route retrieval returned invalid candidates")
            result = {
                "status": "capture_route_passed",
                "source_id": source_id,
                "adapter": actual_adapter,
                "commit": committed.get("commit"),
                "index_status": indexed.get("status"),
                "indexed_source_hits": indexed_source_hits,
                "related_candidate_count": len(candidates),
                "fixture_retained": False,
                "semantic_outcome_required": True,
            }
    except Exception as exc:
        primary_error = exc
    cleanup_error: Exception | None = None
    if index_started:
        try:
            cleaner(index_name, collection)
        except Exception as exc:
            cleanup_error = exc
    if primary_error is not None:
        if cleanup_error is not None:
            raise CanaryError(
                f"capture route failed and qmd cleanup also failed: {cleanup_error}"
            ) from primary_error
        if isinstance(primary_error, CanaryError):
            raise primary_error
        raise CanaryError(f"capture route failed: {primary_error}") from primary_error
    if cleanup_error is not None:
        if isinstance(cleanup_error, CanaryError):
            raise cleanup_error
        raise CanaryError(f"qmd cleanup failed: {cleanup_error}") from cleanup_error
    if result is None:
        raise CanaryError("capture route completed without a result")
    return result
