"""Command-line entry point for Obsidian Vault PR."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Sequence

from .canary import CanaryError, run_capture_route_canary
from .config import ConfigError, load_config, load_vault_path
from .git import GitSafetyError, require_clean_git_root, run_git
from .index import IndexUpdateError, reconcile_qmd_collection, update_qmd_index
from .errors import IngestError
from .ingest import ingest_source
from .migrate import MigrationError, migrate_captures_v3
from .plan import PlanError, apply_plan, init_plan, record_review, validate_plan
from .protocol import (
    EXIT_ERROR,
    EXIT_NOT_ACHIEVED,
    EXIT_OK,
    emit,
    error_envelope,
    success_envelope,
)
from .quality import QualityAuditError, audit_graph
from .quality_record import QualityRecordError, record_quality_findings
from .rag_eval import RagEvaluationError, accept_rag_baseline, evaluate_rag
from .recovery import (
    RecoveryError,
    commit_applied_transaction,
    finalize_applied_transaction,
    list_active_transactions,
    recover_interrupted_transaction,
)
from .retrieve import RetrievalError, retrieve_related_notes
from .tooling import (
    ToolProbeError,
    probe_defuddle_capabilities,
    probe_qmd_capabilities,
)


def doctor(config_path: Path) -> int:
    """Check whether the configured vault is safe to operate on."""
    command = "doctor"
    try:
        config = load_config(config_path)
        vault = load_vault_path(config)
    except ConfigError as exc:
        emit(
            error_envelope(command, "invalid_config", str(exc)),
            stream=sys.stderr,
        )
        return EXIT_ERROR

    failed = False
    checks: list[dict[str, object]] = []

    if not vault.is_dir():
        emit(
            error_envelope(
                command,
                "invalid_vault",
                "vault path does not exist or is not a directory",
            ),
            stream=sys.stderr,
        )
        return EXIT_ERROR

    try:
        require_clean_git_root(vault)
        checks.append({"check": "git_clean", "status": "pass"})
    except GitSafetyError as exc:
        checks.append(
            {"check": "git_clean", "status": "fail", "message": str(exc)}
        )
        failed = True

    git_dir_result = run_git(vault, "rev-parse", "--absolute-git-dir")
    if git_dir_result.returncode == 0:
        try:
            active = list_active_transactions(Path(git_dir_result.stdout.strip()))
        except RecoveryError as exc:
            checks.append(
                {
                    "check": "active_transaction",
                    "status": "fail",
                    "message": str(exc),
                }
            )
            failed = True
        else:
            if active:
                checks.append(
                    {
                        "check": "active_transaction",
                        "status": "fail",
                        "transaction_ids": [path.name for path in active],
                    }
                )
                failed = True
            else:
                checks.append({"check": "active_transaction", "status": "pass"})

    tool_paths: dict[str, str] = {}
    for executable in ("defuddle", "qmd"):
        path = shutil.which(executable)
        if path:
            tool_paths[executable] = path
        else:
            checks.append(
                {
                    "check": f"tool_{executable}",
                    "status": "fail",
                    "message": f"required tool missing: {executable}",
                }
            )
            failed = True

    for tool, path in tool_paths.items():
        try:
            capability = (
                probe_qmd_capabilities(
                    path,
                    index_name=str(config.get("index", {}).get("name", "notebook")),
                )
                if tool == "qmd"
                else probe_defuddle_capabilities(path)
            )
            checks.append(
                {
                    "check": f"tool_{tool}",
                    "status": "pass",
                    "path": path,
                    "capability": capability,
                }
            )
        except ToolProbeError as exc:
            checks.append(
                {
                    "check": f"tool_{tool}",
                    "status": "fail",
                    "path": path,
                    "message": str(exc),
                }
            )
            failed = True

    if "qmd" in tool_paths:
        try:
            alignment = reconcile_qmd_collection(config, check_only=True)
            checks.append(
                {
                    "check": "qmd_collection",
                    "status": (
                        "pass" if alignment["status"] == "aligned" else "fail"
                    ),
                    "data": alignment,
                }
            )
            failed = failed or alignment["status"] != "aligned"
        except (ConfigError, IndexUpdateError) as exc:
            checks.append(
                {
                    "check": "qmd_collection",
                    "status": "fail",
                    "message": str(exc),
                }
            )
            failed = True

    emit(
        success_envelope(
            command,
            "unhealthy" if failed else "healthy",
            {"checks": checks},
        )
    )
    return EXIT_NOT_ACHIEVED if failed else EXIT_OK


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
        "ingest", help="capture a URL, local file, or validated Snapshot Bundle"
    )
    ingest_parser.add_argument(
        "source_reference",
        help="HTTP(S) URL, or local file when --adapter local_file is used",
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
        help="local primary file or manifest-driven Snapshot Bundle directory",
    )
    ingest_parser.add_argument(
        "--adapter",
        choices=("auto", "http", "wechat", "authenticated", "local_file"),
        default="auto",
        help="source adapter; WeChat requires a local HTML/Markdown/PDF --snapshot",
    )
    ingest_parser.add_argument(
        "--logical-name",
        help="display name for a local file; the absolute path is never persisted",
    )
    ingest_parser.add_argument(
        "--supersedes",
        help="explicit prior source_id superseded by this immutable capture",
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

    migrate_parser = subparsers.add_parser(
        "migrate", help="plan or apply a journaled vault-pr data migration"
    )
    migrate_subparsers = migrate_parser.add_subparsers(
        dest="migrate_command", required=True
    )
    captures_v3 = migrate_subparsers.add_parser(
        "captures-v3", help="upgrade every schema-v2 source capture"
    )
    captures_v3.add_argument(
        "--apply", action="store_true", help="write one recoverable migration transaction"
    )
    captures_v3.add_argument(
        "--config", type=Path, default=Path("vault-pr.local.toml")
    )

    canary_parser = subparsers.add_parser(
        "canary", help="run a capture route in a disposable synthetic vault"
    )
    canary_parser.add_argument("url", help="real HTTP or HTTPS source reference")
    canary_parser.add_argument(
        "--snapshot", type=Path, help="local provider-produced snapshot bundle entry"
    )
    canary_parser.add_argument(
        "--adapter",
        choices=("auto", "http", "wechat", "authenticated"),
        default="auto",
    )
    canary_parser.add_argument("--title")

    quality_parser = subparsers.add_parser(
        "quality", help="run read-only graph and RAG quality assessments"
    )
    quality_subparsers = quality_parser.add_subparsers(
        dest="quality_command", required=True
    )
    quality_graph = quality_subparsers.add_parser(
        "graph", help="assess durable wikilinks by note role"
    )
    quality_graph.add_argument(
        "--config", type=Path, default=Path("vault-pr.local.toml")
    )
    quality_rag = quality_subparsers.add_parser(
        "rag", help="score a vault-owned RAG evaluation case set"
    )
    quality_rag.add_argument("--cases", type=Path)
    quality_rag.add_argument(
        "--answers", type=Path, help="temporary JSONL answers to judge semantically"
    )
    quality_rag.add_argument("--limit", type=int, default=10)
    quality_rag.add_argument(
        "--config", type=Path, default=Path("vault-pr.local.toml")
    )
    quality_rag_accept = quality_subparsers.add_parser(
        "rag-accept", help="record a passed RAG assessment as a Git transaction"
    )
    quality_rag_accept.add_argument("--evaluation", type=Path, required=True)
    quality_rag_accept.add_argument("--assessment", type=Path, required=True)
    quality_rag_accept.add_argument("--apply", action="store_true")
    quality_rag_accept.add_argument(
        "--config", type=Path, default=Path("vault-pr.local.toml")
    )
    quality_record = quality_subparsers.add_parser(
        "record-repair-evidence",
        help="record selected deterministic findings for autonomous repair",
    )
    quality_record.add_argument("--audit", type=Path, required=True)
    quality_record.add_argument("--finding", action="append", required=True)
    quality_record.add_argument("--apply", action="store_true")
    quality_record.add_argument(
        "--config", type=Path, default=Path("vault-pr.local.toml")
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
    plan_init.add_argument("--summary", required=True)
    plan_init.add_argument(
        "--authorization-kind",
        choices=("maintenance_request", "autonomous_repair_policy"),
        default="maintenance_request",
    )
    plan_init.add_argument("--authorization-summary", required=True)
    plan_init.add_argument("--source-id", action="append", default=[])
    plan_init.add_argument("--request-ref")
    plan_init.add_argument("--request-sha256")
    plan_init.add_argument("--config", type=Path, default=Path("vault-pr.local.toml"))
    plan_check = plan_subparsers.add_parser("check", help="validate a plan")
    plan_check.add_argument("plan", type=Path)
    plan_check.add_argument("--config", type=Path, default=Path("vault-pr.local.toml"))
    plan_apply = plan_subparsers.add_parser("apply", help="apply a validated plan")
    plan_apply.add_argument("plan", type=Path)
    plan_apply.add_argument("--config", type=Path, default=Path("vault-pr.local.toml"))

    review_parser = subparsers.add_parser(
        "review", help="validate and record a structured semantic assessment"
    )
    review_parser.add_argument("plan", type=Path)
    review_parser.add_argument("--assessment", type=Path, required=True)
    review_parser.add_argument(
        "--config", type=Path, default=Path("vault-pr.local.toml")
    )

    recover_parser = subparsers.add_parser(
        "recover", help="recover or finalize one interrupted transaction"
    )
    recover_parser.add_argument(
        "--config", type=Path, default=Path("vault-pr.local.toml")
    )
    finalize_parser = subparsers.add_parser(
        "finalize", help="verify a committed transaction and remove its active journal"
    )
    finalize_parser.add_argument("transaction_id")
    finalize_parser.add_argument(
        "--config", type=Path, default=Path("vault-pr.local.toml")
    )
    commit_parser = subparsers.add_parser(
        "commit", help="commit exact applied transaction paths and finalize the journal"
    )
    commit_parser.add_argument("transaction_id")
    commit_parser.add_argument("--message", required=True)
    commit_parser.add_argument(
        "--config", type=Path, default=Path("vault-pr.local.toml")
    )
    return parser


def _exception_code(exc: Exception) -> str:
    message = str(exc)
    if message.startswith("maintenance_request_required"):
        return "maintenance_request_required"
    if isinstance(exc, PlanError) and "reviewer approval record is required" in message:
        return "review_required"
    if isinstance(exc, PlanError) and "revision limit" in message:
        return "review_revision_exhausted"
    if isinstance(exc, ConfigError):
        return "invalid_config"
    if isinstance(exc, GitSafetyError):
        return "git_safety_error"
    if isinstance(exc, PlanError):
        return "invalid_plan"
    if isinstance(exc, RecoveryError):
        return "recovery_error"
    if isinstance(exc, IndexUpdateError):
        return "index_error"
    if isinstance(exc, IngestError):
        return "ingest_error"
    if isinstance(exc, RetrievalError):
        return "retrieval_error"
    if isinstance(exc, QualityAuditError):
        return "quality_error"
    if isinstance(exc, QualityRecordError):
        if str(exc) in {
            "quality_record_refresh_required",
            "quality_finding_already_satisfied",
        }:
            return str(exc)
        return "quality_record_error"
    if isinstance(exc, RagEvaluationError):
        return "rag_evaluation_error"
    if isinstance(exc, MigrationError):
        return "migration_error"
    if isinstance(exc, CanaryError):
        return "canary_error"
    return "execution_error"


def _emit_failure(command: str, exc: Exception) -> int:
    code = _exception_code(exc)
    if code == "quality_finding_already_satisfied":
        emit(
            success_envelope(
                command,
                "already_satisfied",
                {"finding_status": code},
            )
        )
        return EXIT_OK
    emit(error_envelope(command, code, str(exc)), stream=sys.stderr)
    if code in {
        "maintenance_request_required",
        "review_required",
        "review_revision_exhausted",
        "quality_record_refresh_required",
    }:
        return EXIT_NOT_ACHIEVED
    return EXIT_ERROR


def _emit_success(
    command: str,
    result: dict[str, object],
    *,
    status: str | None = None,
    achieved: bool = True,
) -> int:
    resolved_status = status or str(result.get("status", "ok"))
    emit(
        success_envelope(
            command,
            resolved_status,
            {key: value for key, value in result.items() if key != "status"},
        )
    )
    return EXIT_OK if achieved else EXIT_NOT_ACHIEVED


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI."""
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        return doctor(args.config)
    if args.command == "ingest":
        command = "ingest"
        try:
            config = load_config(args.config)
            result = ingest_source(
                config=config,
                source_reference=args.source_reference,
                apply=args.apply,
                title_override=args.title,
                snapshot_path=args.snapshot,
                adapter=args.adapter,
                logical_name=args.logical_name,
                supersedes=args.supersedes,
            )
        except (ConfigError, GitSafetyError, IngestError) as exc:
            return _emit_failure(command, exc)
        return _emit_success(command, result)
    if args.command == "index":
        command = "index.check" if args.check else "index.update"
        try:
            config = load_config(args.config)
            result = update_qmd_index(config, check_only=args.check)
        except (ConfigError, GitSafetyError, IndexUpdateError) as exc:
            return _emit_failure(command, exc)
        return _emit_success(
            command,
            result,
            achieved=result["status"] in {"indexed", "aligned"},
        )
    if args.command == "migrate":
        command = f"migrate.{args.migrate_command.replace('-', '_')}"
        try:
            if args.migrate_command != "captures-v3":
                raise AssertionError(
                    f"unhandled migrate command: {args.migrate_command}"
                )
            result = migrate_captures_v3(
                load_config(args.config),
                apply=args.apply,
            )
        except (ConfigError, GitSafetyError, MigrationError) as exc:
            return _emit_failure(command, exc)
        return _emit_success(
            command,
            result,
            achieved=result.get("status") != "dedupe_conflict",
        )
    if args.command == "canary":
        command = "canary"
        try:
            result = run_capture_route_canary(
                url=args.url,
                adapter=args.adapter,
                snapshot_path=args.snapshot,
                title=args.title,
            )
        except CanaryError as exc:
            return _emit_failure(command, exc)
        return _emit_success(command, result)
    if args.command == "quality":
        command = f"quality.{args.quality_command.replace('-', '_')}"
        try:
            config = load_config(args.config)
            if args.quality_command == "graph":
                result = audit_graph(config)
            elif args.quality_command == "rag":
                result = evaluate_rag(
                    config,
                    cases_path=args.cases,
                    answers_path=args.answers,
                    limit=args.limit,
                )
            elif args.quality_command == "rag-accept":
                result = accept_rag_baseline(
                    config,
                    evaluation_path=args.evaluation,
                    assessment_path=args.assessment,
                    apply=args.apply,
                )
            elif args.quality_command == "record-repair-evidence":
                result = record_quality_findings(
                    config,
                    audit_path=args.audit,
                    finding_ids=args.finding,
                    apply=args.apply,
                )
            else:
                raise AssertionError(
                    f"unhandled quality command: {args.quality_command}"
                )
        except (
            ConfigError,
            QualityAuditError,
            QualityRecordError,
            RagEvaluationError,
        ) as exc:
            return _emit_failure(command, exc)
        return _emit_success(command, result)
    if args.command == "retrieve":
        command = "retrieve"
        try:
            result = retrieve_related_notes(
                load_config(args.config), args.source_id, limit=args.limit
            )
        except (ConfigError, RetrievalError) as exc:
            return _emit_failure(command, exc)
        return _emit_success(command, result)
    if args.command == "plan":
        command = f"plan.{args.plan_command}"
        try:
            config = load_config(args.config)
            if args.plan_command == "init":
                result = init_plan(
                    config,
                    args.summary,
                    args.authorization_kind,
                    args.authorization_summary,
                    source_ids=args.source_id,
                    request_ref=args.request_ref,
                    request_sha256=args.request_sha256,
                )
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
            return _emit_failure(command, exc)
        return _emit_success(command, result)
    if args.command == "review":
        command = "review"
        try:
            config = load_config(args.config)
            result = record_review(config, args.plan, args.assessment)
        except (ConfigError, PlanError) as exc:
            return _emit_failure(command, exc)
        status = result["assessment"]["status"]
        return _emit_success(
            command,
            result,
            status=f"review_{status}",
            achieved=status == "approve",
        )
    if args.command == "recover":
        command = "recover"
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
            return _emit_failure(command, exc)
        return _emit_success(command, result)
    if args.command == "finalize":
        command = "finalize"
        try:
            config = load_config(args.config)
            vault = load_vault_path(config)
            git_dir = run_git(vault, "rev-parse", "--absolute-git-dir")
            if git_dir.returncode != 0:
                raise RecoveryError("unable to resolve vault Git directory")
            result = finalize_applied_transaction(
                vault, Path(git_dir.stdout.strip()), args.transaction_id
            )
        except (ConfigError, GitSafetyError, RecoveryError) as exc:
            return _emit_failure(command, exc)
        return _emit_success(command, result)
    if args.command == "commit":
        command = "commit"
        try:
            config = load_config(args.config)
            vault = load_vault_path(config)
            git_dir = run_git(vault, "rev-parse", "--absolute-git-dir")
            if git_dir.returncode != 0:
                raise RecoveryError("unable to resolve vault Git directory")
            result = commit_applied_transaction(
                vault,
                Path(git_dir.stdout.strip()),
                args.transaction_id,
                args.message,
            )
        except (ConfigError, GitSafetyError, RecoveryError) as exc:
            return _emit_failure(command, exc)
        return _emit_success(command, result)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
