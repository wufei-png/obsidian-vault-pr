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


_GIT_REPOSITORY_ENVIRONMENT = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_SYSTEM",
    "GIT_DIR",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_EXEC_PATH",
    "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_QUARANTINE_PATH",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
}


def git_environment(*, index_file: Path | None = None) -> dict[str, str]:
    """Return ambient process state without Git repository-routing overrides."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in _GIT_REPOSITORY_ENVIRONMENT
        and not key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_", "GIT_TRACE"))
    }
    if index_file is not None:
        environment["GIT_INDEX_FILE"] = str(index_file)
    return environment


def run_git(vault: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run Git against the target vault."""
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        check=False,
        capture_output=True,
        text=True,
        env=git_environment(),
    )


def resolve_git_dir(vault: Path) -> Path:
    """Resolve the configured vault's own Git directory under a clean environment."""
    vault = vault.resolve()
    root = run_git(vault, "rev-parse", "--show-toplevel")
    if root.returncode != 0 or Path(root.stdout.strip()).resolve() != vault:
        raise GitSafetyError("configured vault is not the Git repository root")
    result = run_git(vault, "rev-parse", "--absolute-git-dir")
    if result.returncode != 0 or not result.stdout.strip():
        raise GitSafetyError("unable to resolve vault Git directory")
    return Path(result.stdout.strip()).resolve()


def require_clean_git_root(vault: Path) -> None:
    """Require the vault to be the root of a clean Git working tree."""
    vault = vault.resolve()
    resolve_git_dir(vault)

    status = run_git(vault, "status", "--porcelain=v1")
    if status.returncode != 0:
        raise GitSafetyError("unable to inspect vault Git status")
    if status.stdout:
        raise GitSafetyError("vault working tree is not clean")


@contextmanager
def vault_write_lock(vault: Path) -> Iterator[None]:
    """Serialize writers with a kernel-released lock in the Git directory."""
    lock_path = resolve_git_dir(vault) / "vault-pr.lock"
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
