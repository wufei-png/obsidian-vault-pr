"""Bounded subprocess execution for untrusted-source extractors."""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Sequence


class LimitedProcessTimeout(RuntimeError):
    """Raised when a bounded child exceeds its wall-clock deadline."""


@dataclass(frozen=True)
class LimitedProcessResult:
    returncode: int
    stdout: str
    stderr: str


def _resource_limiter(max_memory_bytes: int, cpu_seconds: float, max_file_bytes: int):
    def apply() -> None:
        import resource

        for limit_name in ("RLIMIT_AS", "RLIMIT_DATA"):
            limit = getattr(resource, limit_name, None)
            if limit is None:
                continue
            try:
                resource.setrlimit(limit, (max_memory_bytes, max_memory_bytes))
            except (OSError, ValueError):
                pass
        cpu_limit = max(1, math.ceil(cpu_seconds))
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit + 1))
        except (OSError, ValueError):
            pass
        try:
            resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_bytes, max_file_bytes))
        except (OSError, ValueError):
            pass

    return apply


def run_limited_process(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    max_memory_bytes: int,
    max_file_bytes: int,
    max_capture_bytes: int = 16_384,
) -> LimitedProcessResult:
    """Run a child with wall, CPU, memory, file, and captured-output bounds."""
    kwargs = {}
    if os.name == "posix":
        kwargs["preexec_fn"] = _resource_limiter(
            max_memory_bytes, timeout_seconds, max_file_bytes
        )
    with (
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        try:
            completed = subprocess.run(
                list(command),
                check=False,
                stdout=stdout_file,
                stderr=stderr_file,
                timeout=timeout_seconds,
                **kwargs,
            )
        except subprocess.TimeoutExpired as exc:
            raise LimitedProcessTimeout(
                f"process exceeded {timeout_seconds} seconds"
            ) from exc
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read(max_capture_bytes + 1)
        stderr = stderr_file.read(max_capture_bytes + 1)
    if len(stdout) > max_capture_bytes:
        stdout = stdout[:max_capture_bytes] + b"\n[truncated]"
    if len(stderr) > max_capture_bytes:
        stderr = stderr[:max_capture_bytes] + b"\n[truncated]"
    return LimitedProcessResult(
        completed.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )
