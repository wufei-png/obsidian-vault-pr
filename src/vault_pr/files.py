"""Small filesystem primitives shared by governance write paths."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_replace_bytes(path: Path, content: bytes, *, mode: int = 0o644) -> None:
    """Atomically replace one file through a private, unpredictable sibling."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as file:
            descriptor = -1
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def atomic_replace_text(
    path: Path, content: str, *, encoding: str = "utf-8", mode: int = 0o644
) -> None:
    """Encode and atomically replace one text file."""
    atomic_replace_bytes(path, content.encode(encoding), mode=mode)
