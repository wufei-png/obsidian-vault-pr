"""Stable machine-readable CLI envelopes."""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO


PROTOCOL_VERSION = 1
EXIT_OK = 0
EXIT_NOT_ACHIEVED = 1
EXIT_ERROR = 2


def success_envelope(
    command: str,
    status: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a successful or valid-but-not-achieved command result."""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "command": command,
        "status": status,
        "data": data or {},
    }


def error_envelope(
    command: str,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a stable command failure result."""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "command": command,
        "status": "error",
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        },
    }


def emit(payload: dict[str, Any], *, stream: TextIO | None = None) -> None:
    """Write one JSON result document."""
    print(
        json.dumps(payload, ensure_ascii=False, indent=2),
        file=stream if stream is not None else sys.stdout,
    )
