"""Bounded capability probes for external command-line tools."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable, Sequence

from .process import LimitedProcessResult, LimitedProcessTimeout, run_limited_process


class ToolProbeError(RuntimeError):
    """Raised when an external tool cannot prove required capabilities."""


ProbeRunner = Callable[[Sequence[str]], LimitedProcessResult]
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
    runner: ProbeRunner = _bounded_probe,
) -> dict[str, object]:
    """Probe the exact non-mutating qmd surface required by the index kernel."""
    if not index_name.strip():
        raise ToolProbeError("qmd index name must be non-empty")
    probes = (
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
            "bounded_embedding_batch",
            ("--index", index_name, "embed", "--help"),
            ("--max-docs-per-batch",),
        ),
    )
    return probe_tool_capabilities(
        tool="qmd",
        executable=executable,
        probes=probes,
        runner=runner,
    )


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
