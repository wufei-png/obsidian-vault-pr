"""Declarative qmd collection reconciliation and bounded index refresh."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import (
    ConfigError,
    IndexSpec,
    index_spec,
    load_vault_path,
    retrieval_mode,
)
from .files import atomic_replace_text
from .git import require_clean_git_root, run_git, vault_write_lock
from .recovery import list_active_transactions
from .tooling import ToolProbeError, probe_qmd_capabilities


class IndexUpdateError(RuntimeError):
    """Raised when local index configuration or execution is invalid."""


Runner = Callable[..., subprocess.CompletedProcess[str]]
Sleeper = Callable[[float], None]
QmdCapabilityProbe = Callable[..., dict[str, object]]
INDEX_STATUS_SCHEMA_VERSION = 2
INDEX_STATUS_KIND = "qmd_index_status"
INDEX_STATUS_MAX_BYTES = 1_000_000
HEX64_PATTERN = re.compile(r"[0-9a-f]{64}")
GIT_OID_PATTERN = re.compile(r"[0-9a-f]{40,64}")


@dataclass(frozen=True)
class CollectionState:
    """The supported subset of `qmd collection show` output."""

    path: Path
    mask: str


def _retry_settings(config: dict[str, Any]) -> tuple[int, list[float]]:
    index_config = config.get("index", {})
    try:
        max_retries = int(index_config.get("max_retries", 3))
        backoff = [
            float(value)
            for value in index_config.get("retry_backoff_seconds", [2, 10, 30])
        ]
    except (AttributeError, TypeError, ValueError) as exc:
        raise ConfigError("invalid index retry configuration") from exc
    if max_retries < 0:
        raise ConfigError("index.max_retries must be non-negative")
    if max_retries > 3:
        raise ConfigError("index.max_retries cannot exceed the hard limit of 3")
    if max_retries and len(backoff) < max_retries:
        raise ConfigError(
            "index.retry_backoff_seconds must contain one value per retry"
        )
    if any(delay < 0 for delay in backoff):
        raise ConfigError("index retry delays must be non-negative")
    return max_retries, backoff


def _index_name(config: dict[str, Any]) -> str:
    name = config.get("index", {}).get("name", "notebook")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("index.name must be a non-empty string")
    return name.strip()


def _embed_enabled(config: dict[str, Any]) -> bool:
    return retrieval_mode(config) == "hybrid"


def _batch_size(config: dict[str, Any]) -> int:
    index_config = config.get("index", {})
    if not isinstance(index_config, dict):
        raise ConfigError("index must be a table")
    batch_size = index_config.get("max_docs_per_batch", 64)
    if isinstance(batch_size, bool) or not isinstance(batch_size, int):
        raise ConfigError("index.max_docs_per_batch must be an integer")
    if batch_size < 1:
        raise ConfigError("index.max_docs_per_batch must be positive")
    return batch_size


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def relevant_index_config(config: dict[str, Any]) -> dict[str, object]:
    """Return only configuration that changes qmd index identity or semantics."""
    vault = load_vault_path(config)
    spec = _configured_collection(config, vault)
    return {
        "schema_version": 1,
        "index": {
            "name": _index_name(config),
            "collection": spec.collection,
            "mask": spec.mask,
            "context": spec.context,
            "retrieval_mode": retrieval_mode(config),
        },
    }


def relevant_index_config_sha256(config: dict[str, Any]) -> str:
    """Hash the canonical qmd-relevant subset of the local configuration."""
    return hashlib.sha256(_canonical_json(relevant_index_config(config))).hexdigest()


def _qmd_command(qmd: str, index_name: str, *args: str) -> list[str]:
    return [qmd, "--index", index_name, *args]


def _run_qmd(
    runner: Runner, qmd: str, index_name: str, *args: str
) -> subprocess.CompletedProcess[str]:
    return runner(
        _qmd_command(qmd, index_name, *args),
        check=False,
        capture_output=True,
        text=True,
    )


def _parse_collection_show(output: str) -> CollectionState:
    path_match = re.search(r"^\s*Path:\s+(.+?)\s*$", output, flags=re.MULTILINE)
    mask_match = re.search(r"^\s*Pattern:\s+(.+?)\s*$", output, flags=re.MULTILINE)
    if not path_match or not mask_match:
        raise IndexUpdateError("unsupported qmd collection show output")
    return CollectionState(
        path=Path(path_match.group(1)).expanduser().resolve(),
        mask=mask_match.group(1).strip(),
    )


def _configured_collection(config: dict[str, Any], vault: Path) -> IndexSpec:
    spec = index_spec(config, vault)
    if not spec.root_dir.is_dir():
        raise ConfigError(f"index root does not exist: {spec.root_dir}")
    return spec


def _root_context(context_output: str, collection: str) -> str | None:
    """Read one collection's root context from qmd's supported CLI output."""
    block = re.search(
        rf"^{re.escape(collection)}\s*$\n(?P<body>(?:^[ \t].*$\n?)*)",
        context_output,
        flags=re.MULTILINE,
    )
    if not block:
        return None
    root = re.search(
        r"^[ \t]+/ \(root\)\s*$\n(?P<context>(?:^[ \t]{4,}.*$\n?)*)",
        block.group("body"),
        flags=re.MULTILINE,
    )
    if not root:
        return None
    actual = "\n".join(
        line.strip() for line in root.group("context").splitlines()
    ).strip()
    return actual or None


def _qmd_failure(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout).strip()


def _restore_qmd_state(
    *,
    runner: Runner,
    qmd: str,
    index_name: str,
    collection: str,
    original_state: CollectionState | None,
    original_context: str | None,
    remove_current: bool,
    restore_collection: bool,
    restore_context: bool,
) -> list[str]:
    """Best-effort compensation for destructive qmd reconciliation steps."""
    failures: list[str] = []
    if remove_current:
        removed = _run_qmd(
            runner, qmd, index_name, "collection", "remove", collection
        )
        if removed.returncode != 0:
            failures.append("remove replacement: " + _qmd_failure(removed))
    if restore_collection and original_state is not None:
        restored = _run_qmd(
            runner,
            qmd,
            index_name,
            "collection",
            "add",
            str(original_state.path),
            "--name",
            collection,
            "--mask",
            original_state.mask,
        )
        if restored.returncode != 0:
            failures.append("restore collection: " + _qmd_failure(restored))
    if restore_context and original_context is not None:
        restored_context = _run_qmd(
            runner,
            qmd,
            index_name,
            "context",
            "add",
            f"qmd://{collection}",
            original_context,
        )
        if restored_context.returncode != 0:
            failures.append("restore context: " + _qmd_failure(restored_context))
    return failures


def reconcile_qmd_collection(
    config: dict[str, Any],
    *,
    check_only: bool = False,
    runner: Runner = subprocess.run,
    executable: str | None = None,
) -> dict[str, Any]:
    """Compare the configured vault collection with qmd and repair drift."""
    vault = load_vault_path(config)
    index_name = _index_name(config)
    qmd = executable or shutil.which("qmd")
    if not qmd:
        raise IndexUpdateError("required tool missing: qmd")
    spec = _configured_collection(config, vault)

    contexts = _run_qmd(runner, qmd, index_name, "context", "list")
    if contexts.returncode != 0:
        detail = (contexts.stderr or contexts.stdout).strip()
        raise IndexUpdateError(f"unable to inspect qmd contexts: {detail}")

    actions: list[dict[str, str]] = []
    shown = _run_qmd(runner, qmd, index_name, "collection", "show", spec.collection)
    original_state: CollectionState | None = None
    if shown.returncode == 0:
        original_state = _parse_collection_show(shown.stdout)
        definition_ok = (
            original_state.path == spec.root_dir and original_state.mask == spec.mask
        )
        action = "none" if definition_ok else "replace"
    elif "Collection not found" in (shown.stderr or shown.stdout):
        action = "create"
    else:
        detail = (shown.stderr or shown.stdout).strip()
        raise IndexUpdateError(
            f"unable to inspect qmd collection {spec.collection}: {detail}"
        )

    current_context = _root_context(contexts.stdout, spec.collection)
    context_ok = not spec.context or current_context == spec.context
    if action != "none":
        actions.append({"action": action, "collection": spec.collection})
    if not context_ok:
        actions.append({"action": "update_context", "collection": spec.collection})

    if not check_only:
        original_removed = False
        collection_add_attempted = False
        original_context_removed = False
        try:
            if action == "replace":
                removed = _run_qmd(
                    runner, qmd, index_name, "collection", "remove", spec.collection
                )
                if removed.returncode != 0:
                    raise IndexUpdateError(
                        f"unable to replace qmd collection {spec.collection}: "
                        f"{_qmd_failure(removed)}"
                    )
                original_removed = True
            if action in {"create", "replace"}:
                collection_add_attempted = True
                added = _run_qmd(
                    runner,
                    qmd,
                    index_name,
                    "collection",
                    "add",
                    str(spec.root_dir),
                    "--name",
                    spec.collection,
                    "--mask",
                    spec.mask,
                )
                if added.returncode != 0:
                    raise IndexUpdateError(
                        f"unable to create qmd collection {spec.collection}: "
                        f"{_qmd_failure(added)}"
                    )
            should_set_context = bool(
                spec.context and (not context_ok or action in {"create", "replace"})
            )
            if should_set_context and spec.context:
                if action == "none" and current_context is not None:
                    removed_context = _run_qmd(
                        runner,
                        qmd,
                        index_name,
                        "context",
                        "rm",
                        f"qmd://{spec.collection}",
                    )
                    if removed_context.returncode != 0:
                        raise IndexUpdateError(
                            f"unable to replace qmd context {spec.collection}: "
                            f"{_qmd_failure(removed_context)}"
                        )
                    original_context_removed = True
                context_added = _run_qmd(
                    runner,
                    qmd,
                    index_name,
                    "context",
                    "add",
                    f"qmd://{spec.collection}",
                    spec.context,
                )
                if context_added.returncode != 0:
                    raise IndexUpdateError(
                        f"unable to set qmd context {spec.collection}: "
                        f"{_qmd_failure(context_added)}"
                    )
        except IndexUpdateError as exc:
            rollback_failures = _restore_qmd_state(
                runner=runner,
                qmd=qmd,
                index_name=index_name,
                collection=spec.collection,
                original_state=original_state,
                original_context=current_context,
                remove_current=collection_add_attempted,
                restore_collection=original_removed,
                restore_context=original_removed or original_context_removed,
            )
            if rollback_failures:
                raise IndexUpdateError(
                    f"{exc}; rollback failed: " + "; ".join(rollback_failures)
                ) from exc
            raise

    return {
        "status": "drift" if actions else "aligned",
        "index": index_name,
        "managed_collections": 1,
        "actions": actions,
    }


def index_status_path(vault: Path) -> Path:
    """Return the private Git-directory path for the qmd freshness record."""
    git_dir = run_git(vault, "rev-parse", "--absolute-git-dir")
    if git_dir.returncode != 0:
        raise IndexUpdateError("unable to resolve vault Git directory")
    return Path(git_dir.stdout.strip()) / "vault-pr-index-status.json"


def _validate_qmd_snapshot(snapshot: object) -> dict[str, object]:
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "schema_version",
        "tool",
        "version",
        "retrieval_mode",
        "embedding_model_sha256",
        "chunk_strategy",
        "runtime_identity_sha256",
    }:
        raise IndexUpdateError("index status contains an invalid qmd runtime snapshot")
    if (
        snapshot.get("schema_version") != 3
        or snapshot.get("tool") != "qmd"
        or not isinstance(snapshot.get("version"), str)
        or not snapshot["version"]
    ):
        raise IndexUpdateError("index status contains an invalid qmd runtime snapshot")
    mode = snapshot.get("retrieval_mode")
    model_sha256 = snapshot.get("embedding_model_sha256")
    chunk_strategy = snapshot.get("chunk_strategy")
    if mode == "lexical":
        if model_sha256 is not None or chunk_strategy is not None:
            raise IndexUpdateError("lexical qmd snapshot contains vector identity")
    elif mode == "hybrid":
        if (
            not isinstance(model_sha256, str)
            or not HEX64_PATTERN.fullmatch(model_sha256)
            or chunk_strategy != "regex"
        ):
            raise IndexUpdateError("hybrid qmd snapshot lacks vector identity")
    else:
        raise IndexUpdateError("qmd snapshot contains an invalid retrieval mode")
    runtime_identity = {
        "schema_version": snapshot["schema_version"],
        "tool": snapshot["tool"],
        "version": snapshot["version"],
        "retrieval_mode": mode,
        "embedding_model_sha256": model_sha256,
        "chunk_strategy": chunk_strategy,
    }
    expected_runtime = hashlib.sha256(_canonical_json(runtime_identity)).hexdigest()
    if snapshot.get("runtime_identity_sha256") != expected_runtime:
        raise IndexUpdateError("qmd runtime identity hash does not match")
    return snapshot


def _validate_status(status: object) -> dict[str, Any]:
    if not isinstance(status, dict):
        raise IndexUpdateError("index status must be a JSON object")
    base_fields = {
        "schema_version",
        "kind",
        "status",
        "attempts",
        "updated_at",
        "reconciliation",
        "git_commit",
        "relevant_config_sha256",
        "qmd",
    }
    expected = base_fields | (
        {"last_error"} if status.get("status") == "index_failed" else set()
    )
    if set(status) != expected:
        raise IndexUpdateError("index status has unknown or missing fields")
    if (
        status.get("schema_version") != INDEX_STATUS_SCHEMA_VERSION
        or status.get("kind") != INDEX_STATUS_KIND
        or status.get("status") not in {"indexed", "index_failed"}
        or isinstance(status.get("attempts"), bool)
        or not isinstance(status.get("attempts"), int)
        or status["attempts"] < 1
        or not isinstance(status.get("updated_at"), str)
        or not status["updated_at"]
        or not isinstance(status.get("reconciliation"), dict)
        or not isinstance(status.get("git_commit"), str)
        or not GIT_OID_PATTERN.fullmatch(status["git_commit"])
        or not isinstance(status.get("relevant_config_sha256"), str)
        or not HEX64_PATTERN.fullmatch(status["relevant_config_sha256"])
    ):
        raise IndexUpdateError("index status schema is invalid")
    if status["status"] == "index_failed" and not isinstance(
        status.get("last_error"), str
    ):
        raise IndexUpdateError("failed index status must contain last_error")
    _validate_qmd_snapshot(status.get("qmd"))
    return status


def _write_status(vault: Path, status: dict[str, Any]) -> None:
    _validate_status(status)
    status_path = index_status_path(vault)
    atomic_replace_text(
        status_path,
        json.dumps(status, ensure_ascii=False, indent=2) + "\n",
        mode=0o600,
    )


def _metadata(
    config: dict[str, Any],
    vault: Path,
    qmd_snapshot: dict[str, object],
) -> dict[str, object]:
    head = run_git(vault, "rev-parse", "HEAD")
    if head.returncode != 0 or not head.stdout.strip():
        raise IndexUpdateError("unable to resolve vault HEAD for index status")
    return {
        "git_commit": head.stdout.strip(),
        "relevant_config_sha256": relevant_index_config_sha256(config),
        "qmd": qmd_snapshot,
    }


def _is_default_model_materialization(
    before: dict[str, object], after: dict[str, object]
) -> bool:
    """Allow qmd to materialize its version-bound default model once."""
    if (
        before.get("retrieval_mode") != "hybrid"
        or after.get("retrieval_mode") != "hybrid"
        or before.get("version") != after.get("version")
        or before.get("chunk_strategy") != after.get("chunk_strategy")
    ):
        return False
    version = before.get("version")
    if not isinstance(version, str):
        return False
    default_model_sha256 = hashlib.sha256(
        f"qmd-version-default:{version}".encode("utf-8")
    ).hexdigest()
    return before.get("embedding_model_sha256") == default_model_sha256


def load_index_status(vault: Path) -> tuple[dict[str, Any], str]:
    """Load, bound, validate, and hash the persisted qmd index status."""
    path = index_status_path(vault)
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > INDEX_STATUS_MAX_BYTES
        ):
            raise IndexUpdateError("qmd index status is missing, unsafe, or too large")
        content = path.read_bytes()
        parsed = json.loads(content)
    except IndexUpdateError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IndexUpdateError("qmd index status is not valid UTF-8 JSON") from exc
    return _validate_status(parsed), hashlib.sha256(content).hexdigest()


def _incremental_embed_is_trusted(
    vault: Path, qmd_snapshot: dict[str, object]
) -> bool:
    """Return whether existing vectors are bound to the current runtime identity."""
    try:
        status, _status_sha256 = load_index_status(vault)
    except IndexUpdateError:
        return False
    prior_qmd = status.get("qmd")
    return (
        status.get("status") == "indexed"
        and isinstance(prior_qmd, dict)
        and prior_qmd.get("retrieval_mode") == "hybrid"
        and prior_qmd.get("runtime_identity_sha256")
        == qmd_snapshot.get("runtime_identity_sha256")
    )


def validate_qmd_index_freshness(
    config: dict[str, Any],
    *,
    runner: Runner = subprocess.run,
    executable: str | None = None,
    capability_probe: QmdCapabilityProbe = probe_qmd_capabilities,
) -> dict[str, Any]:
    """Compare one persisted index result with exact current governance inputs."""
    vault = load_vault_path(config)
    index_name = _index_name(config)
    qmd = executable or shutil.which("qmd")
    reasons: list[str] = []
    if not qmd:
        raise IndexUpdateError("required tool missing: qmd")
    head = run_git(vault, "rev-parse", "HEAD")
    if head.returncode != 0 or not head.stdout.strip():
        raise IndexUpdateError("unable to resolve vault HEAD for index freshness")
    expected_commit = head.stdout.strip()
    expected_config = relevant_index_config_sha256(config)
    try:
        current_qmd = capability_probe(
            qmd,
            index_name=index_name,
            retrieval_mode=retrieval_mode(config),
        )
        _validate_qmd_snapshot(current_qmd)
    except (ToolProbeError, IndexUpdateError) as exc:
        raise IndexUpdateError(f"qmd compatibility validation failed: {exc}") from exc

    try:
        status, status_sha256 = load_index_status(vault)
    except IndexUpdateError:
        status = {}
        status_sha256 = None
        reasons.append("index_status_unavailable")
    try:
        alignment = reconcile_qmd_collection(
            config, check_only=True, runner=runner, executable=qmd
        )
        collection_status = alignment.get("status")
    except (ConfigError, IndexUpdateError):
        collection_status = "unknown"
        reasons.append("collection_check_failed")

    checks = {
        "collection_aligned": collection_status == "aligned",
        "index_completed": status.get("status") == "indexed",
        "commit_matches": status.get("git_commit") == expected_commit,
        "config_matches": status.get("relevant_config_sha256") == expected_config,
        "qmd_runtime_matches": (
            isinstance(status.get("qmd"), dict)
            and status["qmd"].get("runtime_identity_sha256")
            == current_qmd.get("runtime_identity_sha256")
        ),
    }
    reasons.extend(name for name, passed in checks.items() if not passed)
    return {
        "schema_version": 1,
        "eligible": not reasons,
        "reasons": sorted(set(reasons)),
        "git_commit": expected_commit,
        "relevant_config_sha256": expected_config,
        "qmd": current_qmd,
        "status_sha256": status_sha256,
        "collection_status": collection_status,
    }


def update_qmd_index(
    config: dict[str, Any],
    *,
    check_only: bool = False,
    runner: Runner = subprocess.run,
    sleeper: Sleeper = time.sleep,
    executable: str | None = None,
    capability_probe: QmdCapabilityProbe = probe_qmd_capabilities,
) -> dict[str, Any]:
    """Reconcile and refresh qmd after a clean content commit."""
    vault = load_vault_path(config)
    require_clean_git_root(vault)
    max_retries, backoff = _retry_settings(config)
    index_name = _index_name(config)
    qmd = executable or shutil.which("qmd")
    if not qmd:
        raise IndexUpdateError("required tool missing: qmd")
    try:
        qmd_snapshot = capability_probe(
            qmd,
            index_name=index_name,
            retrieval_mode=retrieval_mode(config),
        )
        _validate_qmd_snapshot(qmd_snapshot)
    except (ToolProbeError, IndexUpdateError) as exc:
        raise IndexUpdateError(f"qmd runtime validation failed: {exc}") from exc

    if check_only:
        git_dir = run_git(vault, "rev-parse", "--absolute-git-dir")
        if git_dir.returncode != 0:
            raise IndexUpdateError("unable to resolve vault Git directory")
        active = list_active_transactions(Path(git_dir.stdout.strip()))
        if active:
            raise IndexUpdateError(
                "active vault-pr transaction must be finalized before indexing: "
                + ", ".join(path.name for path in active)
            )
        return reconcile_qmd_collection(
            config, check_only=True, runner=runner, executable=qmd
        )

    with vault_write_lock(vault):
        require_clean_git_root(vault)
        git_dir = run_git(vault, "rev-parse", "--absolute-git-dir")
        if git_dir.returncode != 0:
            raise IndexUpdateError("unable to resolve vault Git directory")
        active = list_active_transactions(Path(git_dir.stdout.strip()))
        if active:
            raise IndexUpdateError(
                "active vault-pr transaction must be finalized before indexing: "
                + ", ".join(path.name for path in active)
            )
        reconciliation = reconcile_qmd_collection(config, runner=runner, executable=qmd)
        embed = _embed_enabled(config)
        batch_size = _batch_size(config)
        force_embed = embed and not _incremental_embed_is_trusted(
            vault, qmd_snapshot
        )
        collection = _configured_collection(config, vault).collection

        attempts = 0
        last_error = ""
        for attempt_index in range(max_retries + 1):
            attempts += 1
            updated = _run_qmd(runner, qmd, index_name, "update")
            result = updated
            if updated.returncode == 0 and embed:
                rebuild_args = ["-f", "-c", collection] if force_embed else []
                result = _run_qmd(
                    runner,
                    qmd,
                    index_name,
                    "embed",
                    *rebuild_args,
                    "--max-docs-per-batch",
                    str(batch_size),
                    "--chunk-strategy",
                    "regex",
                )
            if result.returncode == 0:
                try:
                    verified_qmd_snapshot = capability_probe(
                        qmd,
                        index_name=index_name,
                        retrieval_mode=retrieval_mode(config),
                    )
                    _validate_qmd_snapshot(verified_qmd_snapshot)
                except (ToolProbeError, IndexUpdateError) as exc:
                    raise IndexUpdateError(
                        f"post-index qmd identity validation failed: {exc}"
                    ) from exc
                if (
                    verified_qmd_snapshot["runtime_identity_sha256"]
                    != qmd_snapshot["runtime_identity_sha256"]
                    and not _is_default_model_materialization(
                        qmd_snapshot,
                        verified_qmd_snapshot,
                    )
                ):
                    raise IndexUpdateError(
                        "qmd runtime identity changed during index refresh"
                    )
                status = {
                    "schema_version": INDEX_STATUS_SCHEMA_VERSION,
                    "kind": INDEX_STATUS_KIND,
                    "status": "indexed",
                    "attempts": attempts,
                    "updated_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "reconciliation": reconciliation,
                    **_metadata(config, vault, verified_qmd_snapshot),
                }
                _write_status(vault, status)
                return status

            last_error = (result.stderr or result.stdout).strip()
            if attempt_index < max_retries:
                sleeper(backoff[attempt_index])

        status = {
            "schema_version": INDEX_STATUS_SCHEMA_VERSION,
            "kind": INDEX_STATUS_KIND,
            "status": "index_failed",
            "attempts": attempts,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_error": last_error,
            "reconciliation": reconciliation,
            **_metadata(config, vault, qmd_snapshot),
        }
        _write_status(vault, status)
        return status
