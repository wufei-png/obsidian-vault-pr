"""Command-line entry point for Obsidian Vault PR."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Sequence


def load_vault_path(config_path: Path) -> Path:
    """Load and validate the configured target vault path."""
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
        raw_path = config["vault"]["path"]
    except FileNotFoundError as exc:
        raise ValueError(f"config not found: {config_path}") from exc
    except (KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"invalid config: {config_path}") from exc

    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("vault.path must be a non-empty string")
    return Path(raw_path).expanduser().resolve()


def run_git(vault: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a read-only Git command against the target vault."""
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def doctor(config_path: Path) -> int:
    """Check whether the configured vault is safe to operate on."""
    try:
        vault = load_vault_path(config_path)
    except ValueError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    failed = False
    print(f"vault: {vault}")

    if not vault.is_dir():
        print("ERROR vault path does not exist or is not a directory")
        return 2

    root = run_git(vault, "rev-parse", "--show-toplevel")
    if root.returncode != 0:
        print("ERROR vault is not a Git repository")
        failed = True
    elif Path(root.stdout.strip()).resolve() != vault:
        print(f"ERROR vault Git root is {root.stdout.strip()}")
        failed = True
    else:
        print("git: repository root verified")

    status = run_git(vault, "status", "--porcelain=v1")
    if status.returncode != 0:
        print("ERROR unable to inspect vault Git status")
        failed = True
    elif status.stdout:
        print("ERROR vault working tree is not clean")
        failed = True
    else:
        print("git: working tree clean")

    for executable, required in (("defuddle", True), ("qmd", False)):
        path = shutil.which(executable)
        label = "required" if required else "optional"
        if path:
            print(f"tool: {executable} found at {path}")
        else:
            print(f"{'ERROR' if required else 'WARN'} {label} tool missing: {executable}")
            failed = failed or required

    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(prog="vault-pr")
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor_parser = subparsers.add_parser(
        "doctor", help="check the configured target vault without modifying it"
    )
    doctor_parser.add_argument(
        "--config",
        type=Path,
        default=Path("vault-pr.local.toml"),
        help="path to the machine-local TOML config",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI."""
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        return doctor(args.config)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
