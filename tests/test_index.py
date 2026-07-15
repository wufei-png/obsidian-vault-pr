from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from vault_pr.config import ConfigError
from vault_pr.index import IndexUpdateError, reconcile_qmd_collection, update_qmd_index


def make_config(vault: Path, *, max_retries: int = 3) -> dict[str, object]:
    return {
        "vault": {"path": str(vault)},
        "index": {
            "name": "test-notebook",
            "collection": "notebook-tech",
            "mask": "**/*.md",
            "context": "Technical notes.",
            "embed": True,
            "max_docs_per_batch": 64,
            "max_retries": max_retries,
            "retry_backoff_seconds": [0, 0, 0, 0],
        },
    }


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


class IndexTests(unittest.TestCase):
    def make_vault(self, root: Path) -> Path:
        vault = root / "vault"
        (vault / "knowledgebase" / "sources").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(vault)], check=True)
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
            self.assertEqual(runner.collection_path, vault.resolve())
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
            )

            self.assertEqual(status["status"], "indexed")
            self.assertEqual(status["attempts"], 3)
            self.assertEqual(runner.update_calls, 3)
            self.assertTrue(any("embed" in call for call in runner.calls))

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


if __name__ == "__main__":
    unittest.main()
