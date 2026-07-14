"""Command-line entry point for Obsidian Vault PR."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Sequence

from .config import ConfigError, load_config, load_vault_path
from .git import GitSafetyError, require_clean_git_root, run_git
from .index import IndexUpdateError, reconcile_qmd_collections, update_qmd_index
from .ingest import IngestError, ingest_url
from .plan import PlanError, apply_plan, init_plan, record_review, validate_plan
from .recovery import (
    RecoveryError,
    list_active_transactions,
    recover_interrupted_transaction,
)
from .retrieve import RetrievalError, retrieve_related_notes


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

    git_dir_result = run_git(vault, "rev-parse", "--absolute-git-dir")
    if git_dir_result.returncode == 0:
        active = list_active_transactions(Path(git_dir_result.stdout.strip()))
        if active:
            print(
                "ERROR interrupted vault-pr transaction requires recovery: "
                + ", ".join(path.name for path in active)
            )
            failed = True

    backend = config.get("index", {}).get("backend", "qmd")
    if backend != "qmd":
        print(f"ERROR unsupported index backend: {backend}")
        failed = True
    for executable, required in (("defuddle", True), ("qmd", backend == "qmd")):
        path = shutil.which(executable)
        label = "required" if required else "optional"
        if path:
            print(f"tool: {executable} found at {path}")
        else:
            print(
                f"{'ERROR' if required else 'WARN'} {label} tool missing: {executable}"
            )
            failed = failed or required

    if shutil.which("qmd"):
        try:
            alignment = reconcile_qmd_collections(config, check_only=True)
            print(f"qmd: collections {alignment['status']}")
            failed = failed or alignment["status"] != "aligned"
        except (ConfigError, IndexUpdateError) as exc:
            print(f"ERROR qmd collection check failed: {exc}")
            failed = True

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
        "--snapshot",
        type=Path,
        help="local HTML, Markdown, or PDF snapshot for authenticated/tool-assisted input",
    )
    ingest_parser.add_argument(
        "--adapter",
        choices=("auto", "http", "wechat", "authenticated"),
        default="auto",
        help="source-specific adapter; auto detects WeChat and local snapshots",
    )
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
    index_parser.add_argument(
        "--check",
        action="store_true",
        help="report collection drift without changing qmd",
    )

    retrieve_parser = subparsers.add_parser(
        "retrieve", help="retrieve related notes for a captured source"
    )
    retrieve_parser.add_argument("source_id")
    retrieve_parser.add_argument("--limit", type=int, default=10)
    retrieve_parser.add_argument(
        "--config", type=Path, default=Path("vault-pr.local.toml")
    )

    plan_parser = subparsers.add_parser(
        "plan", help="create, check, or apply a semantic change plan"
    )
    plan_subparsers = plan_parser.add_subparsers(dest="plan_command", required=True)
    plan_init = plan_subparsers.add_parser("init", help="create a plan template")
    plan_init.add_argument("source_id")
    plan_init.add_argument("--summary", required=True)
    plan_init.add_argument("--config", type=Path, default=Path("vault-pr.local.toml"))
    plan_check = plan_subparsers.add_parser("check", help="validate a plan")
    plan_check.add_argument("plan", type=Path)
    plan_check.add_argument("--config", type=Path, default=Path("vault-pr.local.toml"))
    plan_apply = plan_subparsers.add_parser("apply", help="apply a validated plan")
    plan_apply.add_argument("plan", type=Path)
    plan_apply.add_argument("--config", type=Path, default=Path("vault-pr.local.toml"))

    review_parser = subparsers.add_parser(
        "review", help="record an independent decision bound to a plan hash"
    )
    review_parser.add_argument("plan", type=Path)
    review_parser.add_argument("--reviewer", required=True)
    review_parser.add_argument(
        "--decision", choices=("approve", "reject"), required=True
    )
    review_parser.add_argument("--notes", default="")

    recover_parser = subparsers.add_parser(
        "recover", help="roll back one interrupted semantic transaction"
    )
    recover_parser.add_argument(
        "--config", type=Path, default=Path("vault-pr.local.toml")
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
                snapshot_path=args.snapshot,
                adapter=args.adapter,
            )
        except (ConfigError, GitSafetyError, IngestError) as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "index":
        try:
            config = load_config(args.config)
            result = update_qmd_index(config, check_only=args.check)
        except (ConfigError, GitSafetyError, IndexUpdateError) as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] in {"indexed", "aligned"} else 1
    if args.command == "retrieve":
        try:
            result = retrieve_related_notes(
                load_config(args.config), args.source_id, limit=args.limit
            )
        except (ConfigError, RetrievalError) as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "plan":
        try:
            config = load_config(args.config)
            if args.plan_command == "init":
                result = init_plan(config, args.source_id, args.summary)
            elif args.plan_command == "check":
                checked = validate_plan(config, args.plan)
                result = {
                    key: value
                    for key, value in checked.items()
                    if key not in {"operations", "plan", "plan_path"}
                }
            elif args.plan_command == "apply":
                result = apply_plan(config, args.plan)
            else:
                raise AssertionError(f"unhandled plan command: {args.plan_command}")
        except (ConfigError, GitSafetyError, PlanError) as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "review":
        try:
            result = record_review(
                args.plan, args.reviewer, args.decision, notes=args.notes
            )
        except PlanError as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if args.decision == "approve" else 1
    if args.command == "recover":
        try:
            config = load_config(args.config)
            vault = load_vault_path(config)
            git_dir = run_git(vault, "rev-parse", "--absolute-git-dir")
            if git_dir.returncode != 0:
                raise RecoveryError("unable to resolve vault Git directory")
            result = recover_interrupted_transaction(
                vault, Path(git_dir.stdout.strip())
            )
        except (ConfigError, GitSafetyError, RecoveryError) as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
