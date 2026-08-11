from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from vault_pr.mutation import execute_journaled_mutation
from vault_pr.recovery import (
    commit_applied_transaction,
    prepare_active_transaction,
)
from vault_pr.transaction import (
    TransactionError,
    diff_transaction,
    list_transactions,
    show_transaction,
)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class TransactionObservabilityTests(unittest.TestCase):
    def _vault(self, root: Path) -> tuple[Path, Path, dict[str, object]]:
        vault = root / "vault"
        vault.mkdir()
        vault = vault.resolve()
        note = vault / "knowledgebase" / "note.md"
        note.parent.mkdir()
        note.write_bytes(b"original\n")
        subprocess.run(["git", "init", "-q", str(vault)], check=True)
        subprocess.run(
            ["git", "-C", str(vault), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(vault), "config", "user.name", "Test"],
            check=True,
        )
        subprocess.run(["git", "-C", str(vault), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(vault), "commit", "-qm", "Initial"],
            check=True,
        )
        return vault, note, {"vault": {"path": str(vault)}}

    def _apply_mutation(self, vault: Path, note: Path) -> dict[str, object]:
        updated = b"updated\n"
        return execute_journaled_mutation(
            vault=vault,
            git_dir=vault / ".git",
            transaction_id="txn_observability_fixture",
            transaction_sha256="a" * 64,
            request_sha256="b" * 64,
            operation="fixture_update",
            receipt_data={"kind": "fixture"},
            affected={note},
            expected_original_sha256={note: _digest(b"original\n")},
            expected_original_mode={note: "100644"},
            final_sha256={note: _digest(updated)},
            final_mode={note: "100644"},
            write=lambda _active_dir: note.write_bytes(updated),
        )

    def test_lists_shows_and_diffs_active_then_finalized_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, note, config = self._vault(Path(temp_dir))
            self._apply_mutation(vault, note)

            listed = list_transactions(config)
            self.assertEqual(len(listed["transactions"]), 1)
            self.assertEqual(listed["transactions"][0]["state"], "APPLIED")
            self.assertEqual(
                listed["transactions"][0]["allowed_actions"],
                ["commit", "recover"],
            )
            shown = show_transaction(config, "txn_observability_fixture")
            self.assertEqual(shown["basis"], "active_journal")
            self.assertEqual(shown["phase"], "applied")
            active_diff = diff_transaction(config, "txn_observability_fixture")
            note_change = next(
                item
                for item in active_diff["files"]
                if item["path"] == "knowledgebase/note.md"
            )
            self.assertTrue(note_change["original_state_available"])
            self.assertEqual(
                note_change["original"]["sha256"], _digest(b"original\n")
            )
            self.assertEqual(note_change["final"]["sha256"], _digest(b"updated\n"))

            commit_applied_transaction(
                vault,
                vault / ".git",
                "txn_observability_fixture",
                "Commit observable fixture",
            )

            finalized = show_transaction(config, "txn_observability_fixture")
            self.assertEqual(finalized["state"], "FINALIZED")
            self.assertEqual(finalized["allowed_actions"], [])
            self.assertEqual(finalized["basis"], "committed_receipt")
            finalized_diff = diff_transaction(config, "txn_observability_fixture")
            note_change = next(
                item
                for item in finalized_diff["files"]
                if item["path"] == "knowledgebase/note.md"
            )
            self.assertFalse(note_change["original_state_available"])
            self.assertEqual(note_change["final"]["sha256"], _digest(b"updated\n"))

    def test_prepared_transaction_reports_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, note, config = self._vault(Path(temp_dir))
            prepare_active_transaction(
                vault=vault,
                git_dir=vault / ".git",
                transaction_id="txn_prepared_observability",
                transaction_sha256="c" * 64,
                request_sha256="d" * 64,
                operation="fixture_prepare",
                affected={note},
                expected_original_sha256={note: _digest(b"original\n")},
                expected_original_mode={note: "100644"},
                final_sha256={note: _digest(b"updated\n")},
                final_mode={note: "100644"},
            )

            shown = show_transaction(config, "txn_prepared_observability")

            self.assertEqual(shown["state"], "RECOVERY_REQUIRED")
            self.assertEqual(shown["allowed_actions"], ["recover"])
            self.assertEqual(
                shown["blocking_reasons"], ["transaction_not_applied"]
            )

    def test_head_change_with_dirty_applied_state_requires_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, note, config = self._vault(Path(temp_dir))
            self._apply_mutation(vault, note)
            base = subprocess.run(
                ["git", "-C", str(vault), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            tree = subprocess.run(
                ["git", "-C", str(vault), "rev-parse", f"{base}^{{tree}}"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            concurrent = subprocess.run(
                [
                    "git",
                    "-C",
                    str(vault),
                    "commit-tree",
                    tree,
                    "-p",
                    base,
                    "-m",
                    "Concurrent",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", str(vault), "update-ref", "HEAD", concurrent, base],
                check=True,
            )

            shown = show_transaction(config, "txn_observability_fixture")

            self.assertEqual(shown["state"], "RECOVERY_REQUIRED")
            self.assertEqual(shown["allowed_actions"], ["recover"])
            self.assertEqual(
                shown["blocking_reasons"],
                ["vault_not_clean_after_head_change"],
            )

    def test_exact_manual_commit_reports_committed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, note, config = self._vault(Path(temp_dir))
            self._apply_mutation(vault, note)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(vault),
                    "add",
                    "--",
                    "knowledgebase/note.md",
                    ".vault-pr/transaction-receipts/txn_observability_fixture.json",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "Exact transaction"],
                check=True,
            )

            shown = show_transaction(config, "txn_observability_fixture")

            self.assertEqual(shown["state"], "COMMITTED")
            self.assertEqual(shown["allowed_actions"], ["finalize", "recover"])
            self.assertEqual(shown["blocking_reasons"], [])

    def test_commit_with_unrelated_path_requires_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, note, config = self._vault(Path(temp_dir))
            self._apply_mutation(vault, note)
            unrelated = vault / "unrelated.md"
            unrelated.write_text("unrelated\n")
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(vault),
                    "add",
                    "--",
                    "knowledgebase/note.md",
                    ".vault-pr/transaction-receipts/txn_observability_fixture.json",
                    "unrelated.md",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "Mixed commit"],
                check=True,
            )

            shown = show_transaction(config, "txn_observability_fixture")

            self.assertEqual(shown["state"], "RECOVERY_REQUIRED")
            self.assertEqual(shown["allowed_actions"], ["recover"])
            self.assertEqual(
                shown["blocking_reasons"],
                ["commit_does_not_match_transaction"],
            )

    def test_show_rejects_invalid_transaction_id_before_inventory(self) -> None:
        with self.assertRaisesRegex(TransactionError, "invalid transaction id"):
            show_transaction({"vault": {"path": "/unused"}}, "../escape")


if __name__ == "__main__":
    unittest.main()
