"""Git safety checks for target vault operations."""

from __future__ import annotations

import fcntl
import os
import stat
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
        raise GitSafetyError("configured vault is not the Git repository root")

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
    try:
        existing = lock_path.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise GitSafetyError("unable to inspect vault write lock") from exc
    if existing is not None and stat.S_ISLNK(existing.st_mode):
        raise GitSafetyError("vault write lock is a symbolic link")
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise GitSafetyError("vault write lock is not a regular file")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise GitSafetyError("unable to open vault write lock") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            existing is not None
            and (existing.st_dev, existing.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise GitSafetyError("vault write lock is not a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise GitSafetyError("another vault-pr writer holds the vault lock") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
