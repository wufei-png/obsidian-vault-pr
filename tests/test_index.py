from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from vault_pr.config import ConfigError
from vault_pr.index import (
    IndexUpdateError,
    load_index_status,
    reconcile_qmd_collection,
    relevant_index_config_sha256,
    update_qmd_index,
    validate_qmd_index_freshness,
)


def make_config(vault: Path, *, max_retries: int = 3) -> dict[str, object]:
    return {
        "vault": {"path": str(vault)},
        "index": {
            "name": "test-notebook",
            "collection": "notebook-tech",
            "mask": "**/*.md",
            "context": "Technical notes.",
            "retrieval_mode": "hybrid",
            "max_docs_per_batch": 64,
            "max_retries": max_retries,
            "retry_backoff_seconds": [0, 0, 0, 0],
        },
    }


def qmd_snapshot(
    version: str = "qmd test",
    *,
    retrieval_mode: str = "hybrid",
    model: str = "model-fixture",
    extra_capabilities: dict[str, bool] | None = None,
) -> dict[str, object]:
    capabilities = {
        "index_selection": True,
        "collection_inspection": True,
        "context_inspection": True,
        "incremental_update": True,
        "lexical_search": True,
    }
    if retrieval_mode == "hybrid":
        capabilities.update(
            {
                "bounded_embedding_batch": True,
                "structured_hybrid_query": True,
            }
        )
    capabilities.update(extra_capabilities or {})
    capability_identity = {
        "schema_version": 2,
        "tool": "qmd",
        "capabilities": capabilities,
    }
    capability_sha256 = hashlib.sha256(
        json.dumps(
            capability_identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    runtime_identity = {
        **capability_identity,
        "version": version,
        "capability_sha256": capability_sha256,
        "retrieval_mode": retrieval_mode,
        "embedding_model_sha256": (
            hashlib.sha256(model.encode("utf-8")).hexdigest()
            if retrieval_mode == "hybrid"
            else None
        ),
        "chunk_strategy": "regex" if retrieval_mode == "hybrid" else None,
    }
    return {
        **runtime_identity,
        "runtime_identity_sha256": hashlib.sha256(
            json.dumps(
                runtime_identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }


def capability_probe(
    _executable: str,
    *,
    index_name: str,
    retrieval_mode: str,
) -> dict[str, object]:
    if index_name != "test-notebook":
        raise AssertionError(f"unexpected qmd index name: {index_name}")
    return qmd_snapshot(retrieval_mode=retrieval_mode)


class QmdRunner:
    def __init__(
        self,
        vault: Path,
        *,
        collection_path: Path | None = None,
        mask: str = "**/*.md",
        refresh_codes: list[int] | None = None,
    ) -> None:
        self.vault = vault
        self.collection_path = collection_path
        self.mask = mask
        self.context = "Technical notes." if collection_path else None
        self.refresh_codes = iter(refresh_codes or [0, 0])
        self.calls: list[list[str]] = []
        self.update_calls = 0

    def __call__(
        self, command: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "qmd test", "")
        args = command[3:]
        if args == ["context", "list"]:
            output = (
                f"notebook-tech\n  / (root)\n    {self.context}\n"
                if self.context
                else "No contexts configured.\n"
            )
            return subprocess.CompletedProcess(command, 0, output, "")
        if args[:3] == ["collection", "show", "notebook-tech"]:
            if self.collection_path is None:
                return subprocess.CompletedProcess(
                    command, 1, "", "Collection not found: notebook-tech"
                )
            output = (
                "Collection: notebook-tech\n"
                f"  Path:     {self.collection_path}\n"
                f"  Pattern:  {self.mask}\n"
                "  Include:  yes (default)\n"
            )
            return subprocess.CompletedProcess(command, 0, output, "")
        if args[:3] == ["collection", "remove", "notebook-tech"]:
            self.collection_path = None
            self.context = None
            return subprocess.CompletedProcess(command, 0, "removed", "")
        if args[:2] == ["collection", "add"]:
            self.collection_path = Path(args[2]).resolve()
            self.mask = args[args.index("--mask") + 1]
            return subprocess.CompletedProcess(command, 0, "added", "")
        if args[:2] == ["context", "rm"]:
            self.context = None
            return subprocess.CompletedProcess(command, 0, "removed", "")
        if args[:2] == ["context", "add"]:
            self.context = args[-1]
            return subprocess.CompletedProcess(command, 0, "added", "")
        if args and args[0] in {"update", "embed"}:
            if args[0] == "update":
                self.update_calls += 1
            code = next(self.refresh_codes)
            return subprocess.CompletedProcess(
                command,
                code,
                "updated" if code == 0 else "",
                "temporary failure" if code else "",
            )
        raise AssertionError(f"unexpected qmd command: {command}")


class FailingQmdRunner(QmdRunner):
    def __init__(
        self,
        vault: Path,
        *,
        collection_path: Path | None = None,
        fail_collection_paths: set[Path] | None = None,
        fail_context_values: set[str] | None = None,
    ) -> None:
        super().__init__(vault, collection_path=collection_path)
        self.fail_collection_paths = {
            path.resolve() for path in (fail_collection_paths or set())
        }
        self.fail_context_values = fail_context_values or set()

    def __call__(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        args = command[3:]
        if (
            args[:2] == ["collection", "add"]
            and Path(args[2]).resolve() in self.fail_collection_paths
        ):
            self.calls.append(command)
            self.collection_path = Path(args[2]).resolve()
            self.mask = args[args.index("--mask") + 1]
            return subprocess.CompletedProcess(
                command, 1, "", f"injected add failure: {args[2]}"
            )
        if (
            args[:2] == ["context", "add"]
            and args[-1] in self.fail_context_values
        ):
            self.calls.append(command)
            return subprocess.CompletedProcess(
                command, 1, "", f"injected context failure: {args[-1]}"
            )
        return super().__call__(command, **kwargs)


class IndexTests(unittest.TestCase):
    def make_vault(self, root: Path) -> Path:
        vault = root / "vault"
        (vault / "knowledgebase" / "sources").mkdir(parents=True)
        (vault / "knowledgebase" / "sources" / ".keep").write_text("")
        subprocess.run(["git", "init", "-q", str(vault)], check=True)
        subprocess.run(
            ["git", "-C", str(vault), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(vault), "config", "user.name", "Test"], check=True
        )
        subprocess.run(["git", "-C", str(vault), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(vault), "commit", "-qm", "fixture"], check=True
        )
        return vault

    def test_check_reports_aligned_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            runner = QmdRunner(vault, collection_path=vault)

            result = reconcile_qmd_collection(
                make_config(vault), check_only=True, runner=runner, executable="qmd"
            )

            self.assertEqual(result["status"], "aligned")
            self.assertEqual(result["actions"], [])

    def test_reconcile_creates_missing_collection_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            runner = QmdRunner(vault)

            result = reconcile_qmd_collection(
                make_config(vault), runner=runner, executable="qmd"
            )

            self.assertEqual(result["status"], "drift")
            assert runner.collection_path is not None
            self.assertEqual(runner.collection_path.resolve(), vault.resolve())
            self.assertEqual(runner.context, "Technical notes.")

    def test_reconcile_replaces_wrong_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            wrong = vault / "wrong"
            wrong.mkdir()
            runner = QmdRunner(vault, collection_path=wrong)

            result = reconcile_qmd_collection(
                make_config(vault), runner=runner, executable="qmd"
            )

            self.assertEqual(result["actions"][0]["action"], "replace")
            self.assertEqual(runner.collection_path, vault.resolve())
            self.assertEqual(runner.context, "Technical notes.")

    def test_reconcile_updates_drifted_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            runner = QmdRunner(vault, collection_path=vault)
            runner.context = "Old context."

            result = reconcile_qmd_collection(
                make_config(vault), runner=runner, executable="qmd"
            )

            self.assertEqual(result["actions"][0]["action"], "update_context")
            self.assertEqual(runner.context, "Technical notes.")

    def test_failed_collection_replace_restores_original_definition(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            wrong = vault / "wrong"
            wrong.mkdir()
            runner = FailingQmdRunner(
                vault,
                collection_path=wrong,
                fail_collection_paths={vault},
            )

            with self.assertRaisesRegex(IndexUpdateError, "injected add failure"):
                reconcile_qmd_collection(
                    make_config(vault), runner=runner, executable="qmd"
                )

            self.assertEqual(runner.collection_path, wrong.resolve())
            self.assertEqual(runner.mask, "**/*.md")
            self.assertEqual(runner.context, "Technical notes.")

    def test_failed_collection_create_removes_partial_definition(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            runner = FailingQmdRunner(
                vault,
                fail_collection_paths={vault},
            )

            with self.assertRaisesRegex(IndexUpdateError, "injected add failure"):
                reconcile_qmd_collection(
                    make_config(vault), runner=runner, executable="qmd"
                )

            self.assertIsNone(runner.collection_path)
            self.assertIsNone(runner.context)
            self.assertTrue(
                any(
                    call[3:6] == ["collection", "remove", "notebook-tech"]
                    for call in runner.calls
                )
            )

    def test_failed_context_replace_restores_original_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            runner = FailingQmdRunner(
                vault,
                collection_path=vault,
                fail_context_values={"Technical notes."},
            )
            runner.context = "Old context."

            with self.assertRaisesRegex(IndexUpdateError, "injected context failure"):
                reconcile_qmd_collection(
                    make_config(vault), runner=runner, executable="qmd"
                )

            assert runner.collection_path is not None
            self.assertEqual(runner.collection_path.resolve(), vault.resolve())
            self.assertEqual(runner.context, "Old context.")

    def test_failed_new_collection_context_removes_partial_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            runner = FailingQmdRunner(
                vault,
                fail_context_values={"Technical notes."},
            )

            with self.assertRaisesRegex(IndexUpdateError, "injected context failure"):
                reconcile_qmd_collection(
                    make_config(vault), runner=runner, executable="qmd"
                )

            self.assertIsNone(runner.collection_path)
            self.assertIsNone(runner.context)

    def test_failed_replacement_context_restores_original_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            wrong = vault / "wrong"
            wrong.mkdir()
            runner = FailingQmdRunner(
                vault,
                collection_path=wrong,
                fail_context_values={"Technical notes."},
            )
            runner.context = "Old context."

            with self.assertRaisesRegex(IndexUpdateError, "injected context failure"):
                reconcile_qmd_collection(
                    make_config(vault), runner=runner, executable="qmd"
                )

            self.assertEqual(runner.collection_path, wrong.resolve())
            self.assertEqual(runner.context, "Old context.")

    def test_reports_primary_and_compensation_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            wrong = vault / "wrong"
            wrong.mkdir()
            runner = FailingQmdRunner(
                vault,
                collection_path=wrong,
                fail_collection_paths={vault, wrong},
            )

            with self.assertRaisesRegex(IndexUpdateError, "rollback failed"):
                reconcile_qmd_collection(
                    make_config(vault), runner=runner, executable="qmd"
                )

    def test_reconcile_adds_missing_context_without_removing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            runner = QmdRunner(vault, collection_path=vault)
            runner.context = None

            result = reconcile_qmd_collection(
                make_config(vault), runner=runner, executable="qmd"
            )

            self.assertEqual(result["actions"][0]["action"], "update_context")
            self.assertEqual(runner.context, "Technical notes.")
            self.assertFalse(
                any(call[3:5] == ["context", "rm"] for call in runner.calls)
            )

    def test_context_match_is_exact_not_substring(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            runner = QmdRunner(vault, collection_path=vault)
            runner.context = "Technical notes. Old suffix."

            result = reconcile_qmd_collection(
                make_config(vault), check_only=True, runner=runner, executable="qmd"
            )

            self.assertEqual(result["status"], "drift")
            self.assertEqual(result["actions"][0]["action"], "update_context")

    def test_retries_then_succeeds_and_embeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            runner = QmdRunner(
                vault,
                collection_path=vault,
                refresh_codes=[1, 1, 0, 0],
            )

            status = update_qmd_index(
                make_config(vault),
                runner=runner,
                sleeper=lambda _: None,
                executable="qmd",
                capability_probe=capability_probe,
            )

            self.assertEqual(status["status"], "indexed")
            self.assertEqual(status["attempts"], 3)
            self.assertEqual(runner.update_calls, 3)
            self.assertTrue(any("embed" in call for call in runner.calls))
            embed_call = next(call for call in runner.calls if call[3] == "embed")
            self.assertIn("-f", embed_call)
            self.assertEqual(
                embed_call[embed_call.index("-c") + 1], "notebook-tech"
            )
            self.assertEqual(
                embed_call[embed_call.index("--chunk-strategy") + 1],
                "regex",
            )

    def test_matching_indexed_runtime_uses_incremental_embedding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            config = make_config(vault)
            first_runner = QmdRunner(vault, collection_path=vault)
            update_qmd_index(
                config,
                runner=first_runner,
                sleeper=lambda _: None,
                executable="qmd",
                capability_probe=capability_probe,
            )
            second_runner = QmdRunner(vault, collection_path=vault)

            update_qmd_index(
                config,
                runner=second_runner,
                sleeper=lambda _: None,
                executable="qmd",
                capability_probe=capability_probe,
            )

            embed_call = next(
                call for call in second_runner.calls if call[3] == "embed"
            )
            self.assertNotIn("-f", embed_call)
            self.assertNotIn("-c", embed_call)

    def test_changed_model_identity_forces_collection_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            config = make_config(vault)
            update_qmd_index(
                config,
                runner=QmdRunner(vault, collection_path=vault),
                sleeper=lambda _: None,
                executable="qmd",
                capability_probe=capability_probe,
            )
            changed_runner = QmdRunner(vault, collection_path=vault)

            update_qmd_index(
                config,
                runner=changed_runner,
                sleeper=lambda _: None,
                executable="qmd",
                capability_probe=(
                    lambda _executable, *, index_name, retrieval_mode: qmd_snapshot(
                        retrieval_mode=retrieval_mode,
                        model="replacement-model-bytes",
                    )
                ),
            )

            embed_call = next(
                call for call in changed_runner.calls if call[3] == "embed"
            )
            self.assertIn("-f", embed_call)
            self.assertEqual(
                embed_call[embed_call.index("-c") + 1], "notebook-tech"
            )

    def test_refuses_fresh_status_when_qmd_identity_changes_during_refresh(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            runner = QmdRunner(vault, collection_path=vault)
            snapshots = iter(
                [
                    qmd_snapshot(model="first-model"),
                    qmd_snapshot(model="second-model"),
                ]
            )

            with self.assertRaisesRegex(
                IndexUpdateError,
                "runtime identity changed",
            ):
                update_qmd_index(
                    make_config(vault),
                    runner=runner,
                    sleeper=lambda _: None,
                    executable="qmd",
                    capability_probe=lambda _executable, **_kwargs: next(snapshots),
                )

            self.assertFalse(
                (vault / ".git" / "vault-pr-index-status.json").exists()
            )

    def test_accepts_first_run_default_model_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            runner = QmdRunner(vault, collection_path=vault)
            snapshots = iter(
                [
                    qmd_snapshot(model="qmd-version-default:qmd test"),
                    qmd_snapshot(model="materialized-default-model"),
                ]
            )

            status = update_qmd_index(
                make_config(vault),
                runner=runner,
                sleeper=lambda _: None,
                executable="qmd",
                capability_probe=lambda _executable, **_kwargs: next(snapshots),
            )

            self.assertEqual(status["status"], "indexed")
            self.assertEqual(
                status["qmd"]["embedding_model_sha256"],
                hashlib.sha256(b"materialized-default-model").hexdigest(),
            )

    def test_lexical_mode_updates_without_embedding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            config = make_config(vault)
            config["index"]["retrieval_mode"] = "lexical"
            runner = QmdRunner(vault, collection_path=vault, refresh_codes=[0])

            status = update_qmd_index(
                config,
                runner=runner,
                sleeper=lambda _: None,
                executable="qmd",
                capability_probe=capability_probe,
            )

            self.assertEqual(status["status"], "indexed")
            self.assertEqual(runner.update_calls, 1)
            self.assertFalse(any(call[3] == "embed" for call in runner.calls))

    def test_refuses_to_index_before_active_transaction_is_finalized(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            active = vault / ".git" / "vault-pr-active" / "txn_fixture"
            active.mkdir(parents=True)
            runner = QmdRunner(vault, collection_path=vault)

            with self.assertRaisesRegex(IndexUpdateError, "must be finalized"):
                update_qmd_index(
                    make_config(vault),
                    runner=runner,
                    sleeper=lambda _: None,
                    executable="qmd",
                    capability_probe=capability_probe,
                )

            self.assertEqual(runner.calls, [])

    def test_rechecks_active_transaction_after_acquiring_index_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            active = vault / ".git" / "vault-pr-active" / "txn_race_fixture"
            runner = QmdRunner(vault, collection_path=vault)

            @contextmanager
            def raced_lock(_vault: Path):
                active.mkdir(parents=True)
                yield

            with patch("vault_pr.index.vault_write_lock", new=raced_lock):
                with self.assertRaisesRegex(IndexUpdateError, "must be finalized"):
                    update_qmd_index(
                        make_config(vault),
                        runner=runner,
                        sleeper=lambda _: None,
                        executable="qmd",
                        capability_probe=capability_probe,
                    )

            self.assertEqual(runner.calls, [])

    def test_stops_after_maximum_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            runner = QmdRunner(
                vault,
                collection_path=vault,
                refresh_codes=[1, 1, 1, 1],
            )

            status = update_qmd_index(
                make_config(vault),
                runner=runner,
                sleeper=lambda _: None,
                executable="qmd",
                capability_probe=capability_probe,
            )

            self.assertEqual(status["status"], "index_failed")
            self.assertEqual(status["attempts"], 4)
            status_file = vault / ".git" / "vault-pr-index-status.json"
            self.assertEqual(
                json.loads(status_file.read_text())["status"], "index_failed"
            )

    def test_requires_one_delay_per_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            config = make_config(vault, max_retries=5)
            with self.assertRaisesRegex(ConfigError, "hard limit of 3"):
                update_qmd_index(config, executable="qmd")

    def test_relevant_config_hash_excludes_execution_and_unrelated_settings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            first = make_config(vault)
            second = make_config(vault)
            second["index"]["max_retries"] = 0
            second["index"]["retry_backoff_seconds"] = []
            second["index"]["max_docs_per_batch"] = 1
            second["review"] = {"review_all_semantic_transactions": False}

            self.assertEqual(
                relevant_index_config_sha256(first),
                relevant_index_config_sha256(second),
            )

            second["index"]["mask"] = "knowledgebase/**/*.md"
            self.assertNotEqual(
                relevant_index_config_sha256(first),
                relevant_index_config_sha256(second),
            )

    def test_freshness_requires_exact_head_config_and_qmd_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            config = make_config(vault)
            runner = QmdRunner(vault, collection_path=vault)
            update_qmd_index(
                config,
                runner=runner,
                sleeper=lambda _: None,
                executable="qmd",
                capability_probe=capability_probe,
            )

            fresh = validate_qmd_index_freshness(
                config,
                runner=runner,
                executable="qmd",
                capability_probe=capability_probe,
            )
            self.assertTrue(fresh["eligible"])
            self.assertEqual(fresh["reasons"], [])

            changed = vault / "knowledgebase" / "changed.md"
            changed.write_text("changed\n")
            subprocess.run(["git", "-C", str(vault), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "change"], check=True
            )
            stale = validate_qmd_index_freshness(
                config,
                runner=runner,
                executable="qmd",
                capability_probe=capability_probe,
            )
            self.assertFalse(stale["eligible"])
            self.assertIn("commit_matches", stale["reasons"])

    def test_freshness_rejects_changed_qmd_capability_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            config = make_config(vault)
            runner = QmdRunner(vault, collection_path=vault)
            update_qmd_index(
                config,
                runner=runner,
                sleeper=lambda _: None,
                executable="qmd",
                capability_probe=capability_probe,
            )

            def changed_probe(
                _executable: str,
                *,
                index_name: str,
                retrieval_mode: str,
            ) -> dict[str, object]:
                return qmd_snapshot(
                    retrieval_mode=retrieval_mode,
                    extra_capabilities={"new_read_only_feature": True},
                )

            stale = validate_qmd_index_freshness(
                config,
                runner=runner,
                executable="qmd",
                capability_probe=changed_probe,
            )
            self.assertFalse(stale["eligible"])
            self.assertIn("qmd_runtime_matches", stale["reasons"])

    def test_freshness_requires_exact_relevant_config_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            config = make_config(vault)
            runner = QmdRunner(vault, collection_path=vault)
            update_qmd_index(
                config,
                runner=runner,
                sleeper=lambda _: None,
                executable="qmd",
                capability_probe=capability_probe,
            )
            changed = make_config(vault)
            changed["index"]["context"] = "Changed context."
            runner.context = "Changed context."

            stale = validate_qmd_index_freshness(
                changed,
                runner=runner,
                executable="qmd",
                capability_probe=capability_probe,
            )

            self.assertFalse(stale["eligible"])
            self.assertIn("config_matches", stale["reasons"])
            self.assertNotIn("collection_aligned", stale["reasons"])

    def test_freshness_rejects_qmd_version_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            config = make_config(vault)
            runner = QmdRunner(vault, collection_path=vault)
            update_qmd_index(
                config,
                runner=runner,
                sleeper=lambda _: None,
                executable="qmd",
                capability_probe=capability_probe,
            )

            stale = validate_qmd_index_freshness(
                config,
                runner=runner,
                executable="qmd",
                capability_probe=lambda _executable, *, index_name, retrieval_mode: qmd_snapshot(
                    "qmd compatible upgrade",
                    retrieval_mode=retrieval_mode,
                ),
            )
            self.assertFalse(stale["eligible"])
            self.assertIn("qmd_runtime_matches", stale["reasons"])

    def test_freshness_rejects_embedding_model_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            config = make_config(vault)
            runner = QmdRunner(vault, collection_path=vault)
            update_qmd_index(
                config,
                runner=runner,
                sleeper=lambda _: None,
                executable="qmd",
                capability_probe=capability_probe,
            )

            stale = validate_qmd_index_freshness(
                config,
                runner=runner,
                executable="qmd",
                capability_probe=lambda _executable, *, index_name, retrieval_mode: qmd_snapshot(
                    retrieval_mode=retrieval_mode,
                    model="different-model",
                ),
            )

            self.assertFalse(stale["eligible"])
            self.assertIn("qmd_runtime_matches", stale["reasons"])

    def test_index_status_schema_rejects_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            config = make_config(vault)
            runner = QmdRunner(vault, collection_path=vault)
            update_qmd_index(
                config,
                runner=runner,
                sleeper=lambda _: None,
                executable="qmd",
                capability_probe=capability_probe,
            )
            status_path = vault / ".git" / "vault-pr-index-status.json"
            status = json.loads(status_path.read_text())
            status["legacy_config_sha256"] = "a" * 64
            status_path.write_text(json.dumps(status))

            with self.assertRaisesRegex(
                IndexUpdateError, "unknown or missing fields"
            ):
                load_index_status(vault)


if __name__ == "__main__":
    unittest.main()
