"""Git safety checks for target vault operations."""

from __future__ import annotations

import fcntl
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class GitSafetyError(RuntimeError):
    """Raised when a target vault is unsafe to modify."""


def run_git(vault: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run Git against the target vault."""
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def require_clean_git_root(vault: Path) -> None:
    """Require the vault to be the root of a clean Git working tree."""
    root = run_git(vault, "rev-parse", "--show-toplevel")
    if root.returncode != 0:
        raise GitSafetyError("vault is not a Git repository")
    if Path(root.stdout.strip()).resolve() != vault:
        raise GitSafetyError(f"vault Git root is {root.stdout.strip()}")

    status = run_git(vault, "status", "--porcelain=v1")
    if status.returncode != 0:
        raise GitSafetyError("unable to inspect vault Git status")
    if status.stdout:
        raise GitSafetyError("vault working tree is not clean")


@contextmanager
def vault_write_lock(vault: Path) -> Iterator[None]:
    """Serialize writers with a kernel-released lock in the Git directory."""
    git_dir_result = run_git(vault, "rev-parse", "--absolute-git-dir")
    if git_dir_result.returncode != 0:
        raise GitSafetyError("unable to resolve vault Git directory")
    lock_path = Path(git_dir_result.stdout.strip()) / "vault-pr.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise GitSafetyError(f"another vault-pr writer holds {lock_path}") from exc
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"pid={os.getpid()}\n".encode())
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
