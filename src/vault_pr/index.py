"""Local qmd index maintenance with bounded retries."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import ConfigError, load_vault_path
from .git import require_clean_git_root, run_git


class IndexUpdateError(RuntimeError):
    """Raised when local index configuration or execution is invalid."""


Runner = Callable[..., subprocess.CompletedProcess[str]]
Sleeper = Callable[[float], None]


def _retry_settings(config: dict[str, Any]) -> tuple[int, list[float]]:
    index_config = config.get("index", {})
    try:
        max_retries = int(index_config.get("max_retries", 3))
        backoff = [
            float(value)
            for value in index_config.get("retry_backoff_seconds", [2, 10, 30])
        ]
    except (TypeError, ValueError) as exc:
        raise ConfigError("invalid index retry configuration") from exc
    if max_retries < 0:
        raise ConfigError("index.max_retries must be non-negative")
    if max_retries and len(backoff) < max_retries:
        raise ConfigError(
            "index.retry_backoff_seconds must contain one value per retry"
        )
    if any(delay < 0 for delay in backoff):
        raise ConfigError("index retry delays must be non-negative")
    return max_retries, backoff


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


def update_qmd_index(
    config: dict[str, Any],
    *,
    runner: Runner = subprocess.run,
    sleeper: Sleeper = time.sleep,
    executable: str | None = None,
) -> dict[str, Any]:
    """Update qmd after a clean content commit, with bounded retries."""
    vault = load_vault_path(config)
    require_clean_git_root(vault)
    max_retries, backoff = _retry_settings(config)
    index_name = config.get("index", {}).get("name", "notebook")
    if not isinstance(index_name, str) or not index_name.strip():
        raise ConfigError("index.name must be a non-empty string")
    qmd = executable or shutil.which("qmd")
    if not qmd:
        raise IndexUpdateError("required tool missing: qmd")

    attempts = 0
    last_error = ""
    for attempt_index in range(max_retries + 1):
        attempts += 1
        result = runner(
            [qmd, "--index", index_name, "update"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            status = {
                "status": "indexed",
                "attempts": attempts,
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
    }
    _write_status(vault, status)
    return status
