"""Bounded capability probes for external command-line tools."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import yaml

from .process import LimitedProcessResult, LimitedProcessTimeout, run_limited_process


class ToolProbeError(RuntimeError):
    """Raised when an external tool cannot prove required capabilities."""


ProbeRunner = Callable[[Sequence[str]], LimitedProcessResult]
EmbeddingModelReader = Callable[[str, str], str]
TOOL_CAPABILITY_SCHEMA_VERSION = 1
PROBE_TIMEOUT_SECONDS = 5.0
PROBE_MAX_MEMORY_BYTES = 512 * 1024 * 1024
PROBE_MAX_FILE_BYTES = 64 * 1024
PROBE_MAX_CAPTURE_BYTES = 64 * 1024


@dataclass(frozen=True)
class CapabilityProbe:
    """One non-mutating command and the output markers it must expose."""

    name: str
    arguments: tuple[str, ...]
    markers: tuple[str, ...] = ()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _bounded_probe(command: Sequence[str]) -> LimitedProcessResult:
    return run_limited_process(
        command,
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
        max_memory_bytes=PROBE_MAX_MEMORY_BYTES,
        max_file_bytes=PROBE_MAX_FILE_BYTES,
        max_capture_bytes=PROBE_MAX_CAPTURE_BYTES,
    )


def _normalized_version(output: str) -> str:
    version = " ".join(output.split()).strip()
    if not version:
        raise ToolProbeError("tool version output is empty")
    if len(version) > 500:
        raise ToolProbeError("tool version output is too long")
    return version


def probe_tool_capabilities(
    *,
    tool: str,
    executable: str,
    probes: Sequence[CapabilityProbe],
    runner: ProbeRunner = _bounded_probe,
    known_incompatible_versions: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Return a versioned capability snapshot from bounded, read-only probes."""
    if not tool or not executable:
        raise ToolProbeError("tool name and executable must be non-empty")
    if not probes or len(probes) > 16:
        raise ToolProbeError("tool capability probe count must be between 1 and 16")
    names = [probe.name for probe in probes]
    if len(names) != len(set(names)) or any(not name for name in names):
        raise ToolProbeError("tool capability probe names must be unique")

    try:
        version_result = runner([executable, "--version"])
    except LimitedProcessTimeout as exc:
        raise ToolProbeError(f"{tool} version probe timed out") from exc
    except OSError as exc:
        raise ToolProbeError(f"{tool} version probe could not start") from exc
    if version_result.returncode != 0:
        detail = (version_result.stderr or version_result.stdout).strip()
        raise ToolProbeError(f"{tool} version probe failed: {detail}")
    version = _normalized_version(version_result.stdout or version_result.stderr)
    if version in known_incompatible_versions:
        raise ToolProbeError(f"{tool} version is known to be incompatible: {version}")

    capabilities: dict[str, bool] = {}
    for probe in probes:
        try:
            result = runner([executable, *probe.arguments])
        except LimitedProcessTimeout as exc:
            raise ToolProbeError(
                f"{tool} capability probe timed out: {probe.name}"
            ) from exc
        except OSError as exc:
            raise ToolProbeError(
                f"{tool} capability probe could not start: {probe.name}"
            ) from exc
        output = f"{result.stdout}\n{result.stderr}".casefold()
        capabilities[probe.name] = result.returncode == 0 and all(
            marker.casefold() in output for marker in probe.markers
        )

    missing = sorted(name for name, available in capabilities.items() if not available)
    if missing:
        raise ToolProbeError(
            f"{tool} is missing required capabilities: " + ", ".join(missing)
        )
    identity = {
        "schema_version": TOOL_CAPABILITY_SCHEMA_VERSION,
        "tool": tool,
        "capabilities": capabilities,
    }
    return {
        **identity,
        "version": version,
        "capability_sha256": hashlib.sha256(_canonical_json(identity)).hexdigest(),
    }


def probe_qmd_capabilities(
    executable: str,
    *,
    index_name: str,
    retrieval_mode: str = "hybrid",
    runner: ProbeRunner = _bounded_probe,
    embedding_model_reader: EmbeddingModelReader | None = None,
) -> dict[str, object]:
    """Probe the exact non-mutating qmd surface required by the index kernel."""
    if not index_name.strip():
        raise ToolProbeError("qmd index name must be non-empty")
    if retrieval_mode not in {"lexical", "hybrid"}:
        raise ToolProbeError("qmd retrieval mode must be lexical or hybrid")
    probes = [
        CapabilityProbe("index_selection", ("--help",), ("--index",)),
        CapabilityProbe(
            "collection_inspection",
            ("--index", index_name, "collection", "--help"),
            ("show",),
        ),
        CapabilityProbe(
            "context_inspection",
            ("--index", index_name, "context", "--help"),
            ("list",),
        ),
        CapabilityProbe(
            "incremental_update",
            ("--index", index_name, "update", "--help"),
        ),
        CapabilityProbe(
            "lexical_search",
            ("--index", index_name, "search", "--help"),
            ("--format",),
        ),
    ]
    if retrieval_mode == "hybrid":
        probes.extend(
            [
                CapabilityProbe(
                    "bounded_embedding_batch",
                    ("--index", index_name, "embed", "--help"),
                    ("--max-docs-per-batch", "--chunk-strategy"),
                ),
                CapabilityProbe(
                    "structured_hybrid_query",
                    ("--index", index_name, "query", "--help"),
                    ("lex:", "vec:", "--no-rerank", "--chunk-strategy"),
                ),
            ]
        )
    snapshot = probe_tool_capabilities(
        tool="qmd",
        executable=executable,
        probes=probes,
        runner=runner,
    )
    model_sha256: str | None = None
    chunk_strategy: str | None = None
    if retrieval_mode == "hybrid":
        reader = embedding_model_reader or _read_qmd_embedding_model
        model = reader(index_name, str(snapshot["version"]))
        if not model.strip():
            raise ToolProbeError("qmd embedding model identity is empty")
        model_sha256 = _embedding_model_sha256(model)
        chunk_strategy = "regex"

    capabilities = snapshot["capabilities"]
    capability_identity = {
        "schema_version": 2,
        "tool": "qmd",
        "capabilities": capabilities,
    }
    capability_sha256 = hashlib.sha256(
        _canonical_json(capability_identity)
    ).hexdigest()
    runtime_identity = {
        **capability_identity,
        "version": snapshot["version"],
        "capability_sha256": capability_sha256,
        "retrieval_mode": retrieval_mode,
        "embedding_model_sha256": model_sha256,
        "chunk_strategy": chunk_strategy,
    }
    return {
        **runtime_identity,
        "runtime_identity_sha256": hashlib.sha256(
            _canonical_json(runtime_identity)
        ).hexdigest(),
    }


def _qmd_config_path(index_name: str) -> Path:
    if "/" in index_name:
        absolute = os.path.abspath(index_name)
        config_name = absolute.replace("/", "_").lstrip("_")
    else:
        config_name = index_name
    configured = os.environ.get("QMD_CONFIG_DIR")
    if configured:
        config_dir = Path(configured).expanduser()
    elif os.environ.get("XDG_CONFIG_HOME"):
        config_dir = Path(os.environ["XDG_CONFIG_HOME"]).expanduser() / "qmd"
    else:
        config_dir = Path.home() / ".config" / "qmd"
    return config_dir / f"{config_name}.yml"


def _qmd_model_cache_dir() -> Path:
    configured = os.environ.get("XDG_CACHE_HOME")
    cache_root = (
        Path(configured).expanduser() if configured else Path.home() / ".cache"
    )
    return cache_root / "qmd" / "models"


def _qmd_huggingface_cache_filename(model: str) -> str | None:
    """Return qmd's exact key for a conservative explicit-file hf URI subset."""
    matched = re.fullmatch(
        r"hf:([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)",
        model,
    )
    if matched is None:
        return None
    user, repository, filename = matched.groups()
    prefix = ["hf", user]
    repository_stem = repository[:-5] if repository.casefold().endswith("-gguf") else None
    filename_stem = filename[:-5] if filename.casefold().endswith(".gguf") else None
    filename_matches_repository = (
        repository_stem is not None
        and filename_stem is not None
        and filename_stem.casefold().startswith(repository_stem.casefold())
    )
    if not filename_matches_repository:
        prefix.append(repository)
    return "_".join(prefix) + "_" + filename


def _cached_huggingface_model(model: str) -> Path | None:
    """Locate the exact valid single-file GGUF cache entry for one hf URI."""
    cache_filename = _qmd_huggingface_cache_filename(model)
    if cache_filename is None:
        return None
    cache_dir = _qmd_model_cache_dir()
    candidate = cache_dir / cache_filename
    try:
        if not candidate.exists():
            return None
        if candidate.is_symlink() or not candidate.is_file():
            raise ToolProbeError("qmd embedding model cache entry is unsafe")
        with candidate.open("rb") as stream:
            if stream.read(4) != b"GGUF":
                raise ToolProbeError("qmd embedding model cache entry is not GGUF")
    except ToolProbeError:
        raise
    except OSError as exc:
        raise ToolProbeError("qmd embedding model cache cannot be inspected") from exc
    return candidate


def _embedding_model_artifact(model: str) -> Path | None:
    cached = _cached_huggingface_model(model)
    if cached is not None:
        return cached
    if model.startswith("hf:") or "://" in model:
        return None
    candidate = Path(model).expanduser()
    if candidate.exists() and not candidate.is_file():
        raise ToolProbeError("qmd embedding model artifact is not a regular file")
    return candidate if candidate.is_file() else None


def _hash_stable_file(path: Path) -> str:
    """Stream a file digest and reject replacement or mutation during the read."""
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ToolProbeError(
                    "qmd embedding model artifact is not a regular file"
                )
            digest = hashlib.sha256()
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        current = path.stat()
    except ToolProbeError:
        raise
    except OSError as exc:
        raise ToolProbeError("qmd embedding model artifact cannot be hashed") from exc
    identity_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    descriptor_changed = any(
        getattr(before, name) != getattr(after, name) for name in identity_fields
    )
    path_changed = any(
        getattr(after, name) != getattr(current, name) for name in identity_fields
    )
    if descriptor_changed or path_changed:
        raise ToolProbeError("qmd embedding model artifact changed during hashing")
    return digest.hexdigest()


def _embedding_model_sha256(model: str) -> str:
    """Hash active model bytes when materialized, else bind its exact locator."""
    if model.startswith("hf:"):
        cached = _cached_huggingface_model(model)
        if cached is None:
            return hashlib.sha256(model.encode("utf-8")).hexdigest()
        digest = _hash_stable_file(cached)
        if _cached_huggingface_model(model) != cached:
            raise ToolProbeError("qmd embedding model cache changed during hashing")
        return digest
    artifact = _embedding_model_artifact(model)
    if artifact is not None:
        return _hash_stable_file(artifact)
    return hashlib.sha256(model.encode("utf-8")).hexdigest()


def _read_qmd_embedding_model(index_name: str, qmd_version: str) -> str:
    """Resolve qmd's active embedding model without persisting its local path."""
    config_path = _qmd_config_path(index_name)
    try:
        if config_path.is_symlink() or (
            config_path.exists()
            and (not config_path.is_file() or config_path.stat().st_size > 1_000_000)
        ):
            raise ToolProbeError("qmd index config is unsafe or too large")
        if config_path.is_file():
            parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if parsed is not None and not isinstance(parsed, dict):
                raise ToolProbeError("qmd index config must be a YAML mapping")
            models = (parsed or {}).get("models", {})
            if models is not None and not isinstance(models, dict):
                raise ToolProbeError("qmd index models must be a YAML mapping")
            configured = (models or {}).get("embed")
            if configured is not None:
                if not isinstance(configured, str) or not configured.strip():
                    raise ToolProbeError("qmd index models.embed must be non-empty")
                return configured.strip()
    except ToolProbeError:
        raise
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ToolProbeError("qmd index config cannot prove model identity") from exc
    environment_model = os.environ.get("QMD_EMBED_MODEL", "").strip()
    if environment_model:
        return environment_model
    return f"qmd-version-default:{qmd_version}"


def probe_defuddle_capabilities(
    executable: str,
    *,
    runner: ProbeRunner = _bounded_probe,
) -> dict[str, object]:
    """Probe the exact Defuddle surface used for deterministic HTML extraction."""
    probes = (
        CapabilityProbe("parse_command", ("--help",), ("parse",)),
        CapabilityProbe(
            "markdown_output",
            ("parse", "--help"),
            ("--md", "-o", "-p"),
        ),
    )
    return probe_tool_capabilities(
        tool="defuddle",
        executable=executable,
        probes=probes,
        runner=runner,
    )
