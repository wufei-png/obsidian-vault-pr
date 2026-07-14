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

from .config import ConfigError, DomainSpec, domain_specs, load_vault_path
from .git import require_clean_git_root, run_git, vault_write_lock
from .recovery import list_active_transactions


class IndexUpdateError(RuntimeError):
    """Raised when local index configuration or execution is invalid."""


Runner = Callable[..., subprocess.CompletedProcess[str]]
Sleeper = Callable[[float], None]


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


def _configured_collections(config: dict[str, Any], vault: Path) -> list[DomainSpec]:
    specs = [
        spec for spec in domain_specs(config, vault).values() if spec.qmd_collection
    ]
    for spec in specs:
        if not spec.root_dir.is_dir():
            raise ConfigError(f"domain root does not exist: {spec.root_dir}")
    return specs


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


def reconcile_qmd_collections(
    config: dict[str, Any],
    *,
    check_only: bool = False,
    runner: Runner = subprocess.run,
    executable: str | None = None,
) -> dict[str, Any]:
    """Compare configured collections with qmd and optionally repair drift."""
    vault = load_vault_path(config)
    index_name = _index_name(config)
    qmd = executable or shutil.which("qmd")
    if not qmd:
        raise IndexUpdateError("required tool missing: qmd")
    mask = config.get("index", {}).get("mask", "**/*.md")
    if not isinstance(mask, str) or not mask.strip():
        raise ConfigError("index.mask must be a non-empty string")
    mask = mask.strip()
    specs = _configured_collections(config, vault)

    contexts = _run_qmd(runner, qmd, index_name, "context", "list")
    if contexts.returncode != 0:
        detail = (contexts.stderr or contexts.stdout).strip()
        raise IndexUpdateError(f"unable to inspect qmd contexts: {detail}")

    actions: list[dict[str, str]] = []
    for spec in specs:
        assert spec.qmd_collection is not None
        shown = _run_qmd(
            runner, qmd, index_name, "collection", "show", spec.qmd_collection
        )
        if shown.returncode == 0:
            state = _parse_collection_show(shown.stdout)
            definition_ok = state.path == spec.root_dir and state.mask == mask
            action = "none" if definition_ok else "replace"
        elif "Collection not found" in (shown.stderr or shown.stdout):
            action = "create"
        else:
            detail = (shown.stderr or shown.stdout).strip()
            raise IndexUpdateError(
                f"unable to inspect qmd collection {spec.qmd_collection}: {detail}"
            )

        current_context = _root_context(contexts.stdout, spec.qmd_collection)
        context_ok = not spec.qmd_context or current_context == spec.qmd_context
        if action != "none":
            actions.append(
                {
                    "action": action,
                    "collection": spec.qmd_collection,
                    "domain": spec.name,
                }
            )
        if not context_ok:
            actions.append(
                {
                    "action": "update_context",
                    "collection": spec.qmd_collection,
                    "domain": spec.name,
                }
            )

        if check_only:
            continue
        if action == "replace":
            removed = _run_qmd(
                runner, qmd, index_name, "collection", "remove", spec.qmd_collection
            )
            if removed.returncode != 0:
                detail = (removed.stderr or removed.stdout).strip()
                raise IndexUpdateError(
                    f"unable to replace qmd collection {spec.qmd_collection}: {detail}"
                )
        if action in {"create", "replace"}:
            added = _run_qmd(
                runner,
                qmd,
                index_name,
                "collection",
                "add",
                str(spec.root_dir),
                "--name",
                spec.qmd_collection,
                "--mask",
                mask,
            )
            if added.returncode != 0:
                detail = (added.stderr or added.stdout).strip()
                raise IndexUpdateError(
                    f"unable to create qmd collection {spec.qmd_collection}: {detail}"
                )
        should_set_context = bool(
            spec.qmd_context and (not context_ok or action in {"create", "replace"})
        )
        if should_set_context and spec.qmd_context:
            if action == "none" and current_context is not None:
                removed_context = _run_qmd(
                    runner,
                    qmd,
                    index_name,
                    "context",
                    "rm",
                    f"qmd://{spec.qmd_collection}",
                )
                if removed_context.returncode != 0:
                    detail = (removed_context.stderr or removed_context.stdout).strip()
                    raise IndexUpdateError(
                        f"unable to replace qmd context {spec.qmd_collection}: {detail}"
                    )
            context_added = _run_qmd(
                runner,
                qmd,
                index_name,
                "context",
                "add",
                f"qmd://{spec.qmd_collection}",
                spec.qmd_context,
            )
            if context_added.returncode != 0:
                detail = (context_added.stderr or context_added.stdout).strip()
                raise IndexUpdateError(
                    f"unable to set qmd context {spec.qmd_collection}: {detail}"
                )

    return {
        "status": "drift" if actions else "aligned",
        "index": index_name,
        "managed_collections": len(specs),
        "actions": actions,
    }


def _write_status(vault: Path, status: dict[str, Any]) -> None:
    git_dir = run_git(vault, "rev-parse", "--absolute-git-dir")
    if git_dir.returncode != 0:
        raise IndexUpdateError("unable to resolve vault Git directory")
    status_path = Path(git_dir.stdout.strip()) / "vault-pr-index-status.json"
    temporary_path = status_path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary_path.replace(status_path)


def _metadata(config: dict[str, Any], vault: Path, qmd_version: str) -> dict[str, str]:
    head = run_git(vault, "rev-parse", "HEAD")
    config_hash = hashlib.sha256(
        json.dumps(config, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return {
        "git_commit": head.stdout.strip() if head.returncode == 0 else "unknown",
        "config_sha256": config_hash,
        "qmd_version": qmd_version,
    }


def update_qmd_index(
    config: dict[str, Any],
    *,
    check_only: bool = False,
    runner: Runner = subprocess.run,
    sleeper: Sleeper = time.sleep,
    executable: str | None = None,
) -> dict[str, Any]:
    """Reconcile and refresh qmd after a clean content commit."""
    vault = load_vault_path(config)
    require_clean_git_root(vault)
    max_retries, backoff = _retry_settings(config)
    index_name = _index_name(config)
    qmd = executable or shutil.which("qmd")
    if not qmd:
        raise IndexUpdateError("required tool missing: qmd")

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
        return reconcile_qmd_collections(
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
        reconciliation = reconcile_qmd_collections(
            config, runner=runner, executable=qmd
        )
        embed = bool(config.get("index", {}).get("embed", True))
        batch_size = config.get("index", {}).get("max_docs_per_batch", 64)
        try:
            batch_size = int(batch_size)
        except (TypeError, ValueError) as exc:
            raise ConfigError("index.max_docs_per_batch must be an integer") from exc
        if batch_size < 1:
            raise ConfigError("index.max_docs_per_batch must be positive")

        attempts = 0
        last_error = ""
        for attempt_index in range(max_retries + 1):
            attempts += 1
            updated = _run_qmd(runner, qmd, index_name, "update")
            result = updated
            if updated.returncode == 0 and embed:
                result = _run_qmd(
                    runner,
                    qmd,
                    index_name,
                    "embed",
                    "--max-docs-per-batch",
                    str(batch_size),
                )
            if result.returncode == 0:
                version = runner(
                    [qmd, "--version"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                status = {
                    "status": "indexed",
                    "attempts": attempts,
                    "updated_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "reconciliation": reconciliation,
                    **_metadata(config, vault, version.stdout.strip() or "unknown"),
                }
                _write_status(vault, status)
                return status

            last_error = (result.stderr or result.stdout).strip()
            if attempt_index < max_retries:
                sleeper(backoff[attempt_index])

        status = {
            "status": "index_failed",
            "attempts": attempts,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_error": last_error,
            "reconciliation": reconciliation,
            **_metadata(config, vault, "unknown"),
        }
        _write_status(vault, status)
        return status
