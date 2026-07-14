from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from vault_pr.recovery import (
    RecoveryError,
    prepare_active_transaction,
    recover_interrupted_transaction,
)


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class RecoveryTests(unittest.TestCase):
    def make_vault(self, root: Path) -> tuple[Path, Path]:
        vault = root / "vault"
        vault.mkdir()
        existing = vault / "knowledgebase" / "existing.md"
        existing.parent.mkdir()
        existing.write_bytes(b"original\n")
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
        return vault, existing

    def prepare_interrupted(self, root: Path) -> tuple[Path, Path, Path, Path]:
        vault, existing = self.make_vault(root)
        created = vault / "knowledgebase" / "created.md"
        active = prepare_active_transaction(
            vault=vault,
            git_dir=vault / ".git",
            transaction_id="txn_recovery_fixture",
            transaction_sha256="a" * 64,
            affected={existing, created},
            final_sha256={
                existing: digest(b"updated\n"),
                created: digest(b"created\n"),
            },
        )
        existing.write_bytes(b"updated\n")
        created.write_bytes(b"created\n")
        return vault, existing, created, active

    def test_recovers_known_partial_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing, created, active = self.prepare_interrupted(Path(temp_dir))

            result = recover_interrupted_transaction(vault, vault / ".git")

            self.assertEqual(result["status"], "recovered")
            self.assertEqual(existing.read_bytes(), b"original\n")
            self.assertFalse(created.exists())
            self.assertFalse(active.exists())

    def test_refuses_unrelated_dirty_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing, created, active = self.prepare_interrupted(Path(temp_dir))
            (vault / "unrelated.md").write_text("human edit\n")

            with self.assertRaisesRegex(RecoveryError, "unrelated vault changes"):
                recover_interrupted_transaction(vault, vault / ".git")

            self.assertEqual(existing.read_bytes(), b"updated\n")
            self.assertTrue(created.exists())
            self.assertTrue(active.exists())

    def test_refuses_tampered_backup_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing, created, active = self.prepare_interrupted(Path(temp_dir))
            journal = json.loads((active / "journal.json").read_text())
            backup = next(
                entry["backup"] for entry in journal["entries"] if entry["backup"]
            )
            (active / backup).write_bytes(b"tampered\n")

            with self.assertRaisesRegex(RecoveryError, "backup integrity failed"):
                recover_interrupted_transaction(vault, vault / ".git")

            self.assertEqual(existing.read_bytes(), b"updated\n")
            self.assertTrue(created.exists())
            self.assertTrue(active.exists())

    def test_refuses_staged_affected_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing, created, active = self.prepare_interrupted(Path(temp_dir))
            subprocess.run(["git", "-C", str(vault), "add", str(existing)], check=True)

            with self.assertRaisesRegex(RecoveryError, "staged vault changes"):
                recover_interrupted_transaction(vault, vault / ".git")

            self.assertEqual(existing.read_bytes(), b"updated\n")
            self.assertTrue(created.exists())
            self.assertTrue(active.exists())

    def test_removes_incomplete_prepare_only_when_vault_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, _existing = self.make_vault(Path(temp_dir))
            active = (
                vault / ".git" / "vault-pr-active" / "txn_incomplete_prepare_fixture"
            )
            (active / "backups").mkdir(parents=True)

            result = recover_interrupted_transaction(vault, vault / ".git")

            self.assertEqual(result["status"], "abandoned_prepare_removed")
            self.assertFalse(active.exists())

    def test_rejects_journal_transaction_id_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing, created, active = self.prepare_interrupted(Path(temp_dir))
            journal_path = active / "journal.json"
            journal = json.loads(journal_path.read_text())
            journal["transaction_id"] = "txn_other"
            journal_path.write_text(json.dumps(journal) + "\n")

            with self.assertRaisesRegex(RecoveryError, "invalid transaction id"):
                recover_interrupted_transaction(vault, vault / ".git")

            self.assertEqual(existing.read_bytes(), b"updated\n")
            self.assertTrue(created.exists())
