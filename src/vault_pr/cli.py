"""Command-line entry point for Obsidian Vault PR."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Sequence

from .config import ConfigError, load_config, load_vault_path
from .git import GitSafetyError, require_clean_git_root
from .index import IndexUpdateError, update_qmd_index
from .ingest import IngestError, ingest_url


def doctor(config_path: Path) -> int:
    """Check whether the configured vault is safe to operate on."""
    try:
        config = load_config(config_path)
        vault = load_vault_path(config)
    except ConfigError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    failed = False
    print(f"vault: {vault}")

    if not vault.is_dir():
        print("ERROR vault path does not exist or is not a directory")
        return 2

    try:
        require_clean_git_root(vault)
        print("git: repository root verified")
        print("git: working tree clean")
    except GitSafetyError as exc:
        print(f"ERROR {exc}")
        failed = True

    for executable, required in (("defuddle", True), ("qmd", False)):
        path = shutil.which(executable)
        label = "required" if required else "optional"
        if path:
            print(f"tool: {executable} found at {path}")
        else:
            print(
                f"{'ERROR' if required else 'WARN'} {label} tool missing: {executable}"
            )
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

    ingest_parser = subparsers.add_parser(
        "ingest", help="capture a URL and create an Obsidian source-note draft"
    )
    ingest_parser.add_argument("url", help="public HTTP or HTTPS source URL")
    ingest_parser.add_argument(
        "--domain", required=True, help="configured domain for the source note"
    )
    ingest_parser.add_argument(
        "--config",
        type=Path,
        default=Path("vault-pr.local.toml"),
        help="path to the machine-local TOML config",
    )
    ingest_parser.add_argument("--title", help="override the extracted source title")
    ingest_parser.add_argument(
        "--apply",
        action="store_true",
        help="write the capture and source-note draft into the configured vault",
    )

    index_parser = subparsers.add_parser(
        "index", help="update the configured local qmd index with bounded retries"
    )
    index_parser.add_argument(
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
    if args.command == "ingest":
        try:
            config = load_config(args.config)
            result = ingest_url(
                config=config,
                url=args.url,
                domain=args.domain,
                apply=args.apply,
                title_override=args.title,
            )
        except (ConfigError, GitSafetyError, IngestError) as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "index":
        try:
            config = load_config(args.config)
            result = update_qmd_index(config)
        except (ConfigError, GitSafetyError, IndexUpdateError) as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "indexed" else 1
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
