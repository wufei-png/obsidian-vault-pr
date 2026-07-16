from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from vault_pr.mutation import (
    MutationError,
    execute_journaled_mutation,
    inspect_journaled_mutation,
)
from vault_pr.recovery import (
    commit_applied_transaction,
    prepare_active_transaction,
    recover_interrupted_transaction,
)


def _git(vault: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        check=False,
        capture_output=True,
        text=True,
    )


class MutationExecutorTests(unittest.TestCase):
    def _vault(self, root: Path) -> tuple[Path, Path]:
        vault = (root / "vault").resolve()
        vault.mkdir()
        _git(vault, "init", "-q")
        _git(vault, "config", "user.email", "fixture@example.com")
        _git(vault, "config", "user.name", "Fixture")
        seed = vault / "seed.md"
        seed.write_text("seed\n", encoding="utf-8")
        _git(vault, "add", "seed.md")
        _git(vault, "commit", "-qm", "fixture")
        git_dir = Path(
            _git(vault, "rev-parse", "--absolute-git-dir").stdout.strip()
        )
        return vault, git_dir

    def test_write_failure_rolls_back_and_is_wrapped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, git_dir = self._vault(Path(temp_dir))
            destination = vault / "created.md"
            content = b"created\n"

            def write(_active_dir: Path) -> None:
                destination.write_bytes(content)
                raise OSError("injected failure")

            with self.assertRaisesRegex(MutationError, "mutation failed"):
                execute_journaled_mutation(
                    vault=vault,
                    git_dir=git_dir,
                    transaction_id="txn_mutation_fixture",
                    transaction_sha256=hashlib.sha256(content).hexdigest(),
                    operation="fixture",
                    receipt_data={"path": "created.md"},
                    affected={destination},
                    expected_original_sha256={destination: None},
                    expected_original_mode={destination: None},
                    final_sha256={
                        destination: hashlib.sha256(content).hexdigest()
                    },
                    final_mode={destination: "100644"},
                    write=write,
                )

            self.assertFalse(destination.exists())
            self.assertFalse(_git(vault, "status", "--porcelain=v1").stdout)
            active = git_dir / "vault-pr-active"
            self.assertFalse(active.exists() and any(active.iterdir()))

    def test_active_and_committed_retries_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, git_dir = self._vault(Path(temp_dir))
            destination = vault / "created.md"
            content = b"created\n"
            digest = hashlib.sha256(content).hexdigest()
            writes = 0

            def write(_active_dir: Path) -> None:
                nonlocal writes
                writes += 1
                destination.write_bytes(content)

            arguments = {
                "vault": vault,
                "git_dir": git_dir,
                "transaction_id": "txn_mutation_idempotent",
                "transaction_sha256": digest,
                "operation": "fixture",
                "receipt_data": {"path": "created.md"},
                "affected": {destination},
                "expected_original_sha256": {destination: None},
                "expected_original_mode": {destination: None},
                "final_sha256": {destination: digest},
                "final_mode": {destination: "100644"},
                "write": write,
            }
            first = execute_journaled_mutation(**arguments)
            retry = execute_journaled_mutation(**arguments)

            self.assertEqual(first["status"], "applied")
            self.assertEqual(retry["status"], "already_active")
            self.assertEqual(retry["next_action"], "commit")
            self.assertTrue(retry["requires_commit"])
            self.assertEqual(writes, 1)

            commit_applied_transaction(
                vault,
                git_dir,
                "txn_mutation_idempotent",
                "Commit idempotent fixture",
            )
            committed_retry = execute_journaled_mutation(**arguments)
            self.assertEqual(committed_retry["status"], "already_committed")
            self.assertFalse(committed_retry["requires_commit"])
            self.assertEqual(writes, 1)

    def test_same_transaction_id_with_other_digest_is_integrity_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, git_dir = self._vault(Path(temp_dir))
            destination = vault / "created.md"
            content = b"created\n"
            digest = hashlib.sha256(content).hexdigest()

            execute_journaled_mutation(
                vault=vault,
                git_dir=git_dir,
                transaction_id="txn_mutation_conflict",
                transaction_sha256=digest,
                operation="fixture",
                receipt_data={"path": "created.md"},
                affected={destination},
                expected_original_sha256={destination: None},
                expected_original_mode={destination: None},
                final_sha256={destination: digest},
                final_mode={destination: "100644"},
                write=lambda _active_dir: destination.write_bytes(content),
            )

            with self.assertRaisesRegex(MutationError, "digest conflict"):
                execute_journaled_mutation(
                    vault=vault,
                    git_dir=git_dir,
                    transaction_id="txn_mutation_conflict",
                    transaction_sha256="f" * 64,
                    operation="fixture",
                    receipt_data={"path": "created.md"},
                    affected={destination},
                    expected_original_sha256={destination: None},
                    expected_original_mode={destination: None},
                    final_sha256={destination: "f" * 64},
                    final_mode={destination: "100644"},
                    write=lambda _active_dir: None,
                )

            commit_applied_transaction(
                vault,
                git_dir,
                "txn_mutation_conflict",
                "Commit conflict fixture",
            )
            with self.assertRaisesRegex(MutationError, "digest conflict"):
                execute_journaled_mutation(
                    vault=vault,
                    git_dir=git_dir,
                    transaction_id="txn_mutation_conflict",
                    transaction_sha256="f" * 64,
                    operation="fixture",
                    receipt_data={"path": "created.md"},
                    affected={destination},
                    expected_original_sha256={destination: None},
                    expected_original_mode={destination: None},
                    final_sha256={destination: "f" * 64},
                    final_mode={destination: "100644"},
                    write=lambda _active_dir: None,
                )

    def test_prepared_retry_requires_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, git_dir = self._vault(Path(temp_dir))
            destination = vault / "created.md"
            digest = hashlib.sha256(b"created\n").hexdigest()
            prepare_active_transaction(
                vault=vault,
                git_dir=git_dir,
                transaction_id="txn_mutation_prepared",
                transaction_sha256=digest,
                operation="fixture",
                affected={destination},
                expected_original_sha256={destination: None},
                expected_original_mode={destination: None},
                final_sha256={destination: digest},
                final_mode={destination: "100644"},
            )

            outcome = inspect_journaled_mutation(
                vault=vault,
                git_dir=git_dir,
                operation="fixture",
                transaction_id="txn_mutation_prepared",
                transaction_sha256=digest,
            )

            self.assertIsNotNone(outcome)
            assert outcome is not None
            self.assertEqual(outcome["status"], "already_active")
            self.assertEqual(outcome["next_action"], "recover")
            self.assertFalse(outcome["requires_commit"])

    def test_committed_but_unfinalized_retry_requires_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, git_dir = self._vault(Path(temp_dir))
            destination = vault / "created.md"
            content = b"created\n"
            digest = hashlib.sha256(content).hexdigest()
            arguments = {
                "vault": vault,
                "git_dir": git_dir,
                "transaction_id": "txn_mutation_unfinalized",
                "transaction_sha256": digest,
                "operation": "fixture",
                "receipt_data": {"path": "created.md"},
                "affected": {destination},
                "expected_original_sha256": {destination: None},
                "expected_original_mode": {destination: None},
                "final_sha256": {destination: digest},
                "final_mode": {destination: "100644"},
                "write": lambda _active_dir: destination.write_bytes(content),
            }
            execute_journaled_mutation(**arguments)
            _git(vault, "add", "--all")
            committed = _git(vault, "commit", "-m", "Commit without finalizing")
            self.assertEqual(committed.returncode, 0, committed.stderr)

            retry = execute_journaled_mutation(**arguments)

            self.assertEqual(retry["status"], "already_active")
            self.assertEqual(retry["next_action"], "recover")
            self.assertFalse(retry["requires_commit"])
            recovered = recover_interrupted_transaction(vault, git_dir)
            self.assertEqual(recovered["status"], "finalized")
            finalized_retry = execute_journaled_mutation(**arguments)
            self.assertEqual(finalized_retry["status"], "already_committed")


if __name__ == "__main__":
    unittest.main()
