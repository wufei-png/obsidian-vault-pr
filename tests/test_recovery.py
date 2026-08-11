from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import vault_pr.recovery as recovery_module
from vault_pr.recovery import (
    RecoveryError,
    commit_applied_transaction,
    finalize_applied_transaction,
    mark_active_applied,
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
            expected_original_sha256={
                existing: digest(b"original\n"),
                created: None,
            },
            expected_original_mode={existing: "100644", created: None},
            final_sha256={
                existing: digest(b"updated\n"),
                created: digest(b"created\n"),
            },
            final_mode={existing: "100644", created: "100644"},
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

    def test_prepare_writes_only_schema_v3_entry_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, _existing, _created, active = self.prepare_interrupted(
                Path(temp_dir)
            )

            journal = json.loads((active / "journal.json").read_text())

            self.assertEqual(journal["schema_version"], 3)
            self.assertEqual(journal["phase"], "prepared")
            self.assertRegex(journal["head_ref"], r"^refs/heads/")
            self.assertNotIn("vault", journal)
            self.assertRegex(journal["vault_id"], r"^[0-9a-f]{64}$")
            self.assertNotIn(str(vault), json.dumps(journal))
            for entry in journal["entries"]:
                self.assertEqual(
                    set(entry),
                    {
                        "path",
                        "original_sha256",
                        "original_mode",
                        "final_sha256",
                        "final_mode",
                        "backup",
                    },
                )

    def test_recovery_rejects_v1_and_v2_journals_without_mutation(self) -> None:
        for schema_version in (1, 2):
            with self.subTest(schema_version=schema_version):
                with tempfile.TemporaryDirectory() as temp_dir:
                    vault, existing, created, active = self.prepare_interrupted(
                        Path(temp_dir)
                    )
                    journal_path = active / "journal.json"
                    journal = json.loads(journal_path.read_text())
                    journal["schema_version"] = schema_version
                    journal_path.write_text(json.dumps(journal) + "\n")

                    with self.assertRaisesRegex(
                        RecoveryError, "unsupported schema version"
                    ):
                        recover_interrupted_transaction(vault, vault / ".git")

                    self.assertEqual(existing.read_bytes(), b"updated\n")
                    self.assertEqual(created.read_bytes(), b"created\n")
                    self.assertTrue(active.exists())

    def test_recovery_requires_explicit_v3_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing, created, active = self.prepare_interrupted(Path(temp_dir))
            journal_path = active / "journal.json"
            journal = json.loads(journal_path.read_text())
            journal.pop("phase")
            journal_path.write_text(json.dumps(journal) + "\n")

            with self.assertRaisesRegex(RecoveryError, "invalid phase"):
                recover_interrupted_transaction(vault, vault / ".git")

            self.assertEqual(existing.read_bytes(), b"updated\n")
            self.assertEqual(created.read_bytes(), b"created\n")
            self.assertTrue(active.exists())

    def test_mark_applied_rejects_v2_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _vault, _existing, _created, active = self.prepare_interrupted(
                Path(temp_dir)
            )
            journal_path = active / "journal.json"
            journal = json.loads(journal_path.read_text())
            journal["schema_version"] = 2
            journal_path.write_text(json.dumps(journal) + "\n")

            with self.assertRaisesRegex(RecoveryError, "unsupported schema version"):
                mark_active_applied(active)

            self.assertEqual(json.loads(journal_path.read_text())["phase"], "prepared")

    def test_prepare_rejects_path_changed_from_expected_original(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing = self.make_vault(Path(temp_dir))
            existing.write_bytes(b"concurrent edit\n")

            with self.assertRaisesRegex(RecoveryError, "changed before transaction"):
                prepare_active_transaction(
                    vault=vault,
                    git_dir=vault / ".git",
                    transaction_id="txn_prepare_race_fixture",
                    transaction_sha256="e" * 64,
                    affected={existing},
                    expected_original_sha256={existing: digest(b"original\n")},
                    expected_original_mode={existing: "100644"},
                    final_sha256={existing: digest(b"updated\n")},
                    final_mode={existing: "100644"},
                )

            active_root = vault / ".git" / "vault-pr-active"
            self.assertFalse(
                active_root.is_dir()
                and any(path.is_dir() for path in active_root.iterdir())
            )

    def test_prepare_rejects_symlinked_active_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, existing = self.make_vault(root)
            outside = root / "outside"
            outside.mkdir()
            active_root = vault / ".git" / "vault-pr-active"
            active_root.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(RecoveryError, "not a regular directory"):
                prepare_active_transaction(
                    vault=vault,
                    git_dir=vault / ".git",
                    transaction_id="txn_active_root_symlink_fixture",
                    transaction_sha256="5" * 64,
                    affected={existing},
                    expected_original_sha256={existing: digest(b"original\n")},
                    expected_original_mode={existing: "100644"},
                    final_sha256={existing: digest(b"updated\n")},
                    final_mode={existing: "100644"},
                )

            self.assertEqual(list(outside.iterdir()), [])

    def test_prepare_rejects_symlinked_transaction_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, existing = self.make_vault(root)
            outside = root / "outside"
            outside.mkdir()
            active_root = vault / ".git" / "vault-pr-active"
            active_root.mkdir()
            (active_root / "txn_symlink_entry_fixture").symlink_to(
                outside, target_is_directory=True
            )

            with self.assertRaisesRegex(RecoveryError, "invalid entry"):
                prepare_active_transaction(
                    vault=vault,
                    git_dir=vault / ".git",
                    transaction_id="txn_new_after_symlink_fixture",
                    transaction_sha256="6" * 64,
                    affected={existing},
                    expected_original_sha256={existing: digest(b"original\n")},
                    expected_original_mode={existing: "100644"},
                    final_sha256={existing: digest(b"updated\n")},
                    final_mode={existing: "100644"},
                )

            self.assertEqual(list(outside.iterdir()), [])

    def test_recovery_restores_original_executable_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing = self.make_vault(Path(temp_dir))
            existing.chmod(0o755)
            subprocess.run(["git", "-C", str(vault), "add", str(existing)], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "executable fixture"],
                check=True,
            )
            active = prepare_active_transaction(
                vault=vault,
                git_dir=vault / ".git",
                transaction_id="txn_executable_recovery_fixture",
                transaction_sha256="1" * 64,
                affected={existing},
                expected_original_sha256={existing: digest(b"original\n")},
                expected_original_mode={existing: "100755"},
                final_sha256={existing: digest(b"updated\n")},
                final_mode={existing: "100644"},
            )
            existing.write_bytes(b"updated\n")
            existing.chmod(0o644)

            recover_interrupted_transaction(vault, vault / ".git")

            self.assertEqual(existing.read_bytes(), b"original\n")
            self.assertEqual(existing.stat().st_mode & 0o777, 0o755)
            self.assertFalse(active.exists())

    def test_recovery_refuses_unexpected_mode_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing = self.make_vault(Path(temp_dir))
            active = prepare_active_transaction(
                vault=vault,
                git_dir=vault / ".git",
                transaction_id="txn_unexpected_mode_fixture",
                transaction_sha256="2" * 64,
                affected={existing},
                expected_original_sha256={existing: digest(b"original\n")},
                expected_original_mode={existing: "100644"},
                final_sha256={existing: digest(b"updated\n")},
                final_mode={existing: "100644"},
            )
            existing.write_bytes(b"updated\n")
            existing.chmod(0o755)

            with self.assertRaisesRegex(RecoveryError, "unexpected state"):
                recover_interrupted_transaction(vault, vault / ".git")

            self.assertTrue(active.exists())
            self.assertEqual(existing.stat().st_mode & 0o777, 0o755)

    def test_recovery_rejects_journal_path_replaced_by_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing = self.make_vault(Path(temp_dir))
            other = vault / "knowledgebase" / "other.md"
            other.write_bytes(b"updated\n")
            subprocess.run(["git", "-C", str(vault), "add", str(other)], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "other fixture"],
                check=True,
            )
            active = prepare_active_transaction(
                vault=vault,
                git_dir=vault / ".git",
                transaction_id="txn_symlink_recovery_fixture",
                transaction_sha256="4" * 64,
                affected={existing},
                expected_original_sha256={existing: digest(b"original\n")},
                expected_original_mode={existing: "100644"},
                final_sha256={existing: digest(b"updated\n")},
                final_mode={existing: "100644"},
            )
            existing.unlink()
            existing.symlink_to(other)

            with self.assertRaisesRegex(RecoveryError, "symbolic link"):
                recover_interrupted_transaction(vault, vault / ".git")

            self.assertEqual(other.read_bytes(), b"updated\n")
            self.assertTrue(existing.is_symlink())
            self.assertTrue(active.exists())

    def test_recovery_removes_directories_created_by_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, _existing = self.make_vault(Path(temp_dir))
            created = vault / "new-root" / "nested" / "created.md"
            active = prepare_active_transaction(
                vault=vault,
                git_dir=vault / ".git",
                transaction_id="txn_created_directories_fixture",
                transaction_sha256="b" * 64,
                affected={created},
                expected_original_sha256={created: None},
                expected_original_mode={created: None},
                final_sha256={created: digest(b"created\n")},
                final_mode={created: "100644"},
            )
            created.parent.mkdir(parents=True)
            created.write_bytes(b"created\n")

            recover_interrupted_transaction(vault, vault / ".git")

            self.assertFalse((vault / "new-root").exists())
            self.assertFalse(active.exists())

    def test_recovery_refuses_undeclared_ignored_archive_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing = self.make_vault(Path(temp_dir))
            audit = (
                vault
                / ".vault-pr"
                / "transactions"
                / "txn_archive_fixture"
                / "result.json"
            )
            active = prepare_active_transaction(
                vault=vault,
                git_dir=vault / ".git",
                transaction_id="txn_archive_fixture",
                transaction_sha256="d" * 64,
                affected={existing, audit},
                expected_original_sha256={
                    existing: digest(b"original\n"),
                    audit: None,
                },
                expected_original_mode={existing: "100644", audit: None},
                final_sha256={
                    existing: digest(b"updated\n"),
                    audit: digest(b"audit\n"),
                },
                final_mode={existing: "100644", audit: "100644"},
            )
            existing.write_bytes(b"updated\n")
            audit.parent.mkdir(parents=True)
            audit.write_bytes(b"audit\n")
            unexpected = audit.parent / "unexpected.tmp"
            unexpected.write_bytes(b"must survive\n")
            exclude = vault / ".git" / "info" / "exclude"
            with exclude.open("a", encoding="utf-8") as file:
                file.write("\n.vault-pr/transactions/**/unexpected.tmp\n")

            with self.assertRaisesRegex(
                RecoveryError, "undeclared transaction archive"
            ):
                recover_interrupted_transaction(vault, vault / ".git")

            self.assertEqual(existing.read_bytes(), b"updated\n")
            self.assertEqual(audit.read_bytes(), b"audit\n")
            self.assertEqual(unexpected.read_bytes(), b"must survive\n")
            self.assertTrue(active.exists())

    def test_recovery_preserves_preexisting_ignored_fixed_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing = self.make_vault(Path(temp_dir))
            fixed_temp = existing.with_name(existing.name + ".vault-pr-tmp")
            exclude = vault / ".git" / "info" / "exclude"
            with exclude.open("a", encoding="utf-8") as file:
                file.write("\nknowledgebase/existing.md.vault-pr-tmp\n")
            fixed_temp.write_bytes(b"unrelated temp\n")
            active = prepare_active_transaction(
                vault=vault,
                git_dir=vault / ".git",
                transaction_id="txn_preserve_temp_fixture",
                transaction_sha256="f" * 64,
                affected={existing},
                expected_original_sha256={existing: digest(b"original\n")},
                expected_original_mode={existing: "100644"},
                final_sha256={existing: digest(b"updated\n")},
                final_mode={existing: "100644"},
            )
            existing.write_bytes(b"updated\n")

            result = recover_interrupted_transaction(vault, vault / ".git")

            self.assertEqual(result["status"], "recovered")
            self.assertEqual(existing.read_bytes(), b"original\n")
            self.assertEqual(fixed_temp.read_bytes(), b"unrelated temp\n")
            self.assertFalse(active.exists())

    def test_finalize_requires_commit_then_removes_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing, created, active = self.prepare_interrupted(Path(temp_dir))
            mark_active_applied(active)

            with self.assertRaisesRegex(RecoveryError, "must be committed"):
                finalize_applied_transaction(
                    vault, vault / ".git", "txn_recovery_fixture"
                )

            subprocess.run(["git", "-C", str(vault), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "apply fixture"],
                check=True,
            )
            result = finalize_applied_transaction(
                vault, vault / ".git", "txn_recovery_fixture"
            )

            self.assertEqual(result["status"], "finalized")
            self.assertEqual(existing.read_bytes(), b"updated\n")
            self.assertEqual(created.read_bytes(), b"created\n")
            self.assertFalse(active.exists())

    def test_commit_stages_exact_paths_and_finalizes_under_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing, created, active = self.prepare_interrupted(Path(temp_dir))
            mark_active_applied(active)

            result = commit_applied_transaction(
                vault,
                vault / ".git",
                "txn_recovery_fixture",
                "Apply recovery fixture",
            )

            self.assertEqual(result["status"], "committed_and_finalized")
            self.assertEqual(existing.read_bytes(), b"updated\n")
            self.assertEqual(created.read_bytes(), b"created\n")
            self.assertFalse(active.exists())
            subject = subprocess.run(
                ["git", "-C", str(vault), "log", "-1", "--format=%s"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(subject.stdout.strip(), "Apply recovery fixture")

    def test_commit_records_declared_mode_only_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing = self.make_vault(Path(temp_dir))
            active = prepare_active_transaction(
                vault=vault,
                git_dir=vault / ".git",
                transaction_id="txn_mode_only_fixture",
                transaction_sha256="3" * 64,
                affected={existing},
                expected_original_sha256={existing: digest(b"original\n")},
                expected_original_mode={existing: "100644"},
                final_sha256={existing: digest(b"original\n")},
                final_mode={existing: "100755"},
            )
            existing.chmod(0o755)
            mark_active_applied(active)

            result = commit_applied_transaction(
                vault,
                vault / ".git",
                "txn_mode_only_fixture",
                "Record declared executable mode",
            )

            self.assertEqual(result["status"], "committed_and_finalized")
            mode = subprocess.run(
                [
                    "git",
                    "-C",
                    str(vault),
                    "ls-tree",
                    "HEAD",
                    "--",
                    "knowledgebase/existing.md",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.split()[0]
            self.assertEqual(mode, "100755")

    def test_commit_supports_transaction_as_first_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(vault), "config", "user.name", "Test"],
                check=True,
            )
            created = vault / "knowledgebase" / "created.md"
            active = prepare_active_transaction(
                vault=vault,
                git_dir=vault / ".git",
                transaction_id="txn_first_commit_cas_fixture",
                transaction_sha256="a" * 64,
                affected={created},
                expected_original_sha256={created: None},
                expected_original_mode={created: None},
                final_sha256={created: digest(b"created\n")},
                final_mode={created: "100644"},
            )
            created.parent.mkdir(parents=True)
            created.write_bytes(b"created\n")
            mark_active_applied(active)

            result = commit_applied_transaction(
                vault,
                vault / ".git",
                "txn_first_commit_cas_fixture",
                "First governed commit",
            )

            self.assertEqual(result["status"], "committed_and_finalized")
            self.assertFalse(active.exists())
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(vault), "log", "-1", "--format=%s"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                "First governed commit",
            )

    def test_commit_validates_content_through_configured_clean_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, _existing = self.make_vault(root)
            filter_script = root / "filter.py"
            filter_script.write_text(
                """import sys

payload = sys.stdin.buffer.read()
prefix = b"filtered-content\\n"
if sys.argv[1] == "clean":
    sys.stdout.buffer.write(prefix + payload)
elif payload.startswith(prefix):
    sys.stdout.buffer.write(payload[len(prefix):])
else:
    raise SystemExit(2)
"""
            )
            command = f"{shlex.quote(sys.executable)} {shlex.quote(str(filter_script))}"
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(vault),
                    "config",
                    "filter.fixture.clean",
                    f"{command} clean",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(vault),
                    "config",
                    "filter.fixture.smudge",
                    f"{command} smudge",
                ],
                check=True,
            )
            attributes = vault / ".gitattributes"
            attributes.write_text("*.asset filter=fixture\n")
            subprocess.run(
                ["git", "-C", str(vault), "add", ".gitattributes"], check=True
            )
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "add fixture filter"],
                check=True,
            )

            created = vault / "knowledgebase" / "capture.asset"
            content = b"raw capture bytes\n"
            active = prepare_active_transaction(
                vault=vault,
                git_dir=vault / ".git",
                transaction_id="txn_filtered_content_fixture",
                transaction_sha256="4" * 64,
                affected={created},
                expected_original_sha256={created: None},
                expected_original_mode={created: None},
                final_sha256={created: digest(content)},
                final_mode={created: "100644"},
            )
            created.parent.mkdir(parents=True, exist_ok=True)
            created.write_bytes(content)
            mark_active_applied(active)

            result = commit_applied_transaction(
                vault,
                vault / ".git",
                "txn_filtered_content_fixture",
                "Commit filtered capture",
            )

            committed = subprocess.run(
                ["git", "-C", str(vault), "show", "HEAD:knowledgebase/capture.asset"],
                check=True,
                capture_output=True,
            )
            self.assertEqual(result["status"], "committed_and_finalized")
            self.assertEqual(created.read_bytes(), content)
            self.assertEqual(committed.stdout, b"filtered-content\n" + content)

    def test_commit_refuses_unrelated_dirty_path_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, _existing, _created, active = self.prepare_interrupted(
                Path(temp_dir)
            )
            mark_active_applied(active)
            (vault / "unrelated.md").write_text("human edit\n")

            with self.assertRaisesRegex(RecoveryError, "unrelated vault changes"):
                commit_applied_transaction(
                    vault,
                    vault / ".git",
                    "txn_recovery_fixture",
                    "Apply recovery fixture",
                )

            staged = subprocess.run(
                ["git", "-C", str(vault), "diff", "--cached", "--name-only"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(staged.stdout, "")
            self.assertTrue(active.exists())

    def test_finalize_rejects_unrelated_committed_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, _existing, _created, active = self.prepare_interrupted(
                Path(temp_dir)
            )
            mark_active_applied(active)
            unrelated = vault / "unrelated.md"
            unrelated.write_text("unrelated\n")
            subprocess.run(["git", "-C", str(vault), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "mixed commit"],
                check=True,
            )

            with self.assertRaisesRegex(RecoveryError, "does not match journal paths"):
                finalize_applied_transaction(
                    vault, vault / ".git", "txn_recovery_fixture"
                )

            self.assertTrue(active.exists())

    def test_finalize_supports_transaction_in_first_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(vault), "config", "user.name", "Test"],
                check=True,
            )
            created = vault / "knowledgebase" / "created.md"
            active = prepare_active_transaction(
                vault=vault,
                git_dir=vault / ".git",
                transaction_id="txn_first_commit_fixture",
                transaction_sha256="c" * 64,
                affected={created},
                expected_original_sha256={created: None},
                expected_original_mode={created: None},
                final_sha256={created: digest(b"created\n")},
                final_mode={created: "100644"},
            )
            created.parent.mkdir(parents=True)
            created.write_bytes(b"created\n")
            mark_active_applied(active)
            subprocess.run(["git", "-C", str(vault), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "first transaction"],
                check=True,
            )

            result = finalize_applied_transaction(
                vault, vault / ".git", "txn_first_commit_fixture"
            )

            self.assertEqual(result["status"], "finalized")
            self.assertFalse(active.exists())

    def test_recover_auto_finalizes_committed_applied_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing, created, active = self.prepare_interrupted(Path(temp_dir))
            mark_active_applied(active)
            subprocess.run(["git", "-C", str(vault), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "apply fixture"],
                check=True,
            )

            result = recover_interrupted_transaction(vault, vault / ".git")

            self.assertEqual(result["status"], "finalized")
            self.assertEqual(existing.read_bytes(), b"updated\n")
            self.assertEqual(created.read_bytes(), b"created\n")
            self.assertFalse(active.exists())

    def test_recover_rolls_back_uncommitted_applied_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing, created, active = self.prepare_interrupted(Path(temp_dir))
            mark_active_applied(active)

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

    def test_refuses_incompletely_staged_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing, created, active = self.prepare_interrupted(Path(temp_dir))
            subprocess.run(["git", "-C", str(vault), "add", str(existing)], check=True)

            with self.assertRaisesRegex(
                RecoveryError, "staged paths do not match"
            ):
                recover_interrupted_transaction(vault, vault / ".git")

            self.assertEqual(existing.read_bytes(), b"updated\n")
            self.assertTrue(created.exists())
            self.assertTrue(active.exists())

    def test_recovers_exact_transaction_staged_before_commit_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing, created, active = self.prepare_interrupted(Path(temp_dir))
            mark_active_applied(active)
            subprocess.run(
                ["git", "-C", str(vault), "add", "--", str(existing), str(created)],
                check=True,
            )

            result = recover_interrupted_transaction(vault, vault / ".git")

            self.assertEqual(result["status"], "recovered")
            self.assertEqual(existing.read_bytes(), b"original\n")
            self.assertFalse(created.exists())
            self.assertFalse(active.exists())
            status = subprocess.run(
                ["git", "-C", str(vault), "status", "--porcelain=v1"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout, "")

    def test_recovers_staged_rename_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, source = self.make_vault(Path(temp_dir))
            destination = source.with_name("renamed.md")
            active = prepare_active_transaction(
                vault=vault,
                git_dir=vault / ".git",
                transaction_id="txn_staged_rename_fixture",
                transaction_sha256="b" * 64,
                affected={source, destination},
                expected_original_sha256={
                    source: digest(b"original\n"),
                    destination: None,
                },
                expected_original_mode={source: "100644", destination: None},
                final_sha256={
                    source: None,
                    destination: digest(b"original\n"),
                },
                final_mode={source: None, destination: "100644"},
            )
            source.rename(destination)
            mark_active_applied(active)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(vault),
                    "add",
                    "-A",
                    "--",
                    str(source),
                    str(destination),
                ],
                check=True,
            )

            result = recover_interrupted_transaction(vault, vault / ".git")

            self.assertEqual(result["status"], "recovered")
            self.assertEqual(source.read_bytes(), b"original\n")
            self.assertFalse(destination.exists())
            status = subprocess.run(
                ["git", "-C", str(vault), "status", "--porcelain=v1"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout, "")

    def test_refused_recovery_preserves_exact_transaction_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing, created, active = self.prepare_interrupted(Path(temp_dir))
            mark_active_applied(active)
            subprocess.run(
                ["git", "-C", str(vault), "add", "--", str(existing), str(created)],
                check=True,
            )
            (vault / "unrelated.md").write_text("human edit\n")

            with self.assertRaisesRegex(RecoveryError, "unrelated vault changes"):
                recover_interrupted_transaction(vault, vault / ".git")

            staged = subprocess.run(
                ["git", "-C", str(vault), "diff", "--cached", "--name-only"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                set(staged.stdout.splitlines()),
                {"knowledgebase/existing.md", "knowledgebase/created.md"},
            )
            self.assertTrue(active.exists())

    def test_commit_bypasses_non_governance_commit_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, _existing, _created, active = self.prepare_interrupted(
                Path(temp_dir)
            )
            mark_active_applied(active)
            marker = vault / "hook-ran"
            for name in ("pre-commit", "commit-msg", "post-commit"):
                hook = vault / ".git" / "hooks" / name
                hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
                hook.chmod(0o755)

            result = commit_applied_transaction(
                vault,
                vault / ".git",
                "txn_recovery_fixture",
                "Governed fixture",
            )

            self.assertEqual(result["status"], "committed_and_finalized")
            self.assertFalse(marker.exists())
            self.assertFalse(active.exists())

    def test_post_commit_hook_is_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, _existing, _created, active = self.prepare_interrupted(
                Path(temp_dir)
            )
            mark_active_applied(active)
            marker = vault / "post-commit-ran"
            hook = vault / ".git" / "hooks" / "post-commit"
            hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
            hook.chmod(0o755)

            result = commit_applied_transaction(
                vault,
                vault / ".git",
                "txn_recovery_fixture",
                "Commit without running hooks",
            )

            self.assertEqual(result["status"], "committed_and_finalized")
            self.assertNotIn("post_commit_hook", result)
            self.assertFalse(marker.exists())
            self.assertFalse(active.exists())

    def test_repository_routing_environment_cannot_redirect_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, _existing, _created, active = self.prepare_interrupted(root)
            mark_active_applied(active)
            other = root / "other"
            other.mkdir()
            subprocess.run(["git", "init", "-q", str(other)], check=True)
            subprocess.run(
                ["git", "-C", str(other), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(other), "config", "user.name", "Test"],
                check=True,
            )
            (other / "other.md").write_text("other\n")
            subprocess.run(["git", "-C", str(other), "add", "other.md"], check=True)
            subprocess.run(
                ["git", "-C", str(other), "commit", "-qm", "Other base"],
                check=True,
            )
            vault_base = recovery_module.run_git(
                vault, "rev-parse", "HEAD"
            ).stdout.strip()
            other_base = subprocess.run(
                ["git", "-C", str(other), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with patch.dict(
                os.environ,
                {
                    "GIT_DIR": str(other / ".git"),
                    "GIT_WORK_TREE": str(vault),
                    "GIT_INDEX_FILE": str(other / ".git" / "index"),
                    "GIT_OBJECT_DIRECTORY": str(other / ".git" / "objects"),
                },
            ):
                result = commit_applied_transaction(
                    vault,
                    vault / ".git",
                    "txn_recovery_fixture",
                    "Environment-isolated commit",
                )

            self.assertEqual(result["status"], "committed_and_finalized")
            self.assertNotEqual(
                recovery_module.run_git(vault, "rev-parse", "HEAD").stdout.strip(),
                vault_base,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(other), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                other_base,
            )

    def test_commit_rejects_switch_to_same_base_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, _existing, _created, active = self.prepare_interrupted(
                Path(temp_dir)
            )
            mark_active_applied(active)
            original_branch = recovery_module.run_git(
                vault, "symbolic-ref", "--short", "HEAD"
            ).stdout.strip()
            base_commit = recovery_module.run_git(
                vault, "rev-parse", "HEAD"
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", str(vault), "switch", "-qc", "alternate"],
                check=True,
            )

            with self.assertRaisesRegex(RecoveryError, "HEAD changed since"):
                commit_applied_transaction(
                    vault,
                    vault / ".git",
                    "txn_recovery_fixture",
                    "Wrong branch must not advance",
                )

            self.assertEqual(
                recovery_module.run_git(
                    vault, "rev-parse", f"refs/heads/{original_branch}"
                ).stdout.strip(),
                base_commit,
            )
            self.assertEqual(
                recovery_module.run_git(
                    vault, "rev-parse", "refs/heads/alternate"
                ).stdout.strip(),
                base_commit,
            )
            self.assertTrue(active.exists())

    def test_detached_head_commit_updates_only_detached_head(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, existing = self.make_vault(root)
            original_branch = recovery_module.run_git(
                vault, "symbolic-ref", "--short", "HEAD"
            ).stdout.strip()
            base_commit = recovery_module.run_git(
                vault, "rev-parse", "HEAD"
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", str(vault), "switch", "--detach", "-q"],
                check=True,
            )
            active = prepare_active_transaction(
                vault=vault,
                git_dir=vault / ".git",
                transaction_id="txn_detached_fixture",
                transaction_sha256="d" * 64,
                affected={existing},
                expected_original_sha256={existing: digest(b"original\n")},
                expected_original_mode={existing: "100644"},
                final_sha256={existing: digest(b"detached update\n")},
                final_mode={existing: "100644"},
            )
            existing.write_bytes(b"detached update\n")
            mark_active_applied(active)

            result = commit_applied_transaction(
                vault,
                vault / ".git",
                "txn_detached_fixture",
                "Detached governed commit",
            )

            self.assertEqual(result["status"], "committed_and_finalized")
            self.assertEqual(
                recovery_module.run_git(
                    vault, "symbolic-ref", "-q", "HEAD"
                ).returncode,
                1,
            )
            self.assertNotEqual(
                recovery_module.run_git(vault, "rev-parse", "HEAD").stdout.strip(),
                base_commit,
            )
            self.assertEqual(
                recovery_module.run_git(
                    vault, "rev-parse", f"refs/heads/{original_branch}"
                ).stdout.strip(),
                base_commit,
            )

    def test_commit_ref_cas_never_overwrites_concurrent_head(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, _existing, _created, active = self.prepare_interrupted(
                Path(temp_dir)
            )
            mark_active_applied(active)
            real_run_git = recovery_module.run_git
            base_commit = real_run_git(vault, "rev-parse", "HEAD").stdout.strip()
            raced = False

            def race_update_ref(path: Path, *args: str):
                nonlocal raced
                if (
                    not raced
                    and len(args) > 1
                    and args[0] == "update-ref"
                    and args[1].startswith("refs/heads/")
                ):
                    raced = True
                    tree = real_run_git(
                        path, "rev-parse", f"{base_commit}^{{tree}}"
                    ).stdout.strip()
                    concurrent = real_run_git(
                        path,
                        "commit-tree",
                        tree,
                        "-p",
                        base_commit,
                        "-m",
                        "Concurrent commit",
                    ).stdout.strip()
                    advanced = real_run_git(
                        path, "update-ref", args[1], concurrent, base_commit
                    )
                    self.assertEqual(advanced.returncode, 0)
                return real_run_git(path, *args)

            with patch("vault_pr.recovery.run_git", side_effect=race_update_ref):
                with self.assertRaisesRegex(RecoveryError, "commit CAS"):
                    commit_applied_transaction(
                        vault,
                        vault / ".git",
                        "txn_recovery_fixture",
                        "Losing transaction commit",
                    )

            subject = real_run_git(
                vault, "log", "-1", "--format=%s"
            ).stdout.strip()
            self.assertEqual(subject, "Concurrent commit")
            self.assertTrue(active.exists())
            self.assertFalse((active / "commit-index").exists())

    def test_commit_rollback_preserves_concurrent_worktree_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing, _created, active = self.prepare_interrupted(
                Path(temp_dir)
            )
            mark_active_applied(active)
            real_run_git = recovery_module.run_git
            base_commit = real_run_git(vault, "rev-parse", "HEAD").stdout.strip()
            edited = False

            def edit_after_cas(path: Path, *args: str):
                nonlocal edited
                result = real_run_git(path, *args)
                if (
                    not edited
                    and len(args) > 1
                    and args[0] == "update-ref"
                    and args[1].startswith("refs/heads/")
                    and result.returncode == 0
                ):
                    edited = True
                    existing.write_bytes(b"external edit\n")
                return result

            with patch("vault_pr.recovery.run_git", side_effect=edit_after_cas):
                with self.assertRaisesRegex(
                    RecoveryError, "index synchronization failed"
                ):
                    commit_applied_transaction(
                        vault,
                        vault / ".git",
                        "txn_recovery_fixture",
                        "Transaction racing a worktree edit",
                    )

            self.assertEqual(existing.read_bytes(), b"external edit\n")
            self.assertEqual(
                real_run_git(vault, "rev-parse", "HEAD").stdout.strip(),
                base_commit,
            )
            self.assertEqual(
                real_run_git(
                    vault, "diff", "--cached", "--name-only"
                ).stdout,
                "",
            )
            self.assertTrue(active.exists())

    def test_recovery_finishes_index_sync_after_crash_following_ref_cas(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, existing, created, active = self.prepare_interrupted(
                Path(temp_dir)
            )
            mark_active_applied(active)
            real_run_git = recovery_module.run_git
            base_commit = real_run_git(vault, "rev-parse", "HEAD").stdout.strip()

            def crash_after_cas(path: Path, *args: str):
                result = real_run_git(path, *args)
                if (
                    len(args) > 1
                    and args[0] == "update-ref"
                    and args[1].startswith("refs/heads/")
                    and result.returncode == 0
                ):
                    raise SystemExit("simulated process death after ref CAS")
                return result

            with patch("vault_pr.recovery.run_git", side_effect=crash_after_cas):
                with self.assertRaisesRegex(SystemExit, "after ref CAS"):
                    commit_applied_transaction(
                        vault,
                        vault / ".git",
                        "txn_recovery_fixture",
                        "Commit interrupted after ref CAS",
                    )

            committed_head = real_run_git(
                vault, "rev-parse", "HEAD"
            ).stdout.strip()
            self.assertNotEqual(committed_head, base_commit)
            self.assertEqual(existing.read_bytes(), b"updated\n")
            self.assertEqual(created.read_bytes(), b"created\n")
            self.assertEqual(
                set(
                    real_run_git(
                        vault, "diff", "--cached", "--name-only"
                    ).stdout.splitlines()
                ),
                {"knowledgebase/existing.md", "knowledgebase/created.md"},
            )
            self.assertTrue(active.exists())

            result = recover_interrupted_transaction(vault, vault / ".git")

            self.assertEqual(result["status"], "finalized")
            self.assertEqual(result["commit"], committed_head)
            self.assertFalse(active.exists())
            self.assertEqual(
                real_run_git(vault, "status", "--porcelain=v1").stdout,
                "",
            )

    def test_post_cas_recovery_refuses_changed_index_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, _existing, _created, active = self.prepare_interrupted(
                Path(temp_dir)
            )
            mark_active_applied(active)
            real_run_git = recovery_module.run_git

            def crash_after_cas(path: Path, *args: str):
                result = real_run_git(path, *args)
                if (
                    len(args) > 1
                    and args[0] == "update-ref"
                    and args[1].startswith("refs/heads/")
                    and result.returncode == 0
                ):
                    raise SystemExit("simulated process death after ref CAS")
                return result

            with patch("vault_pr.recovery.run_git", side_effect=crash_after_cas):
                with self.assertRaises(SystemExit):
                    commit_applied_transaction(
                        vault,
                        vault / ".git",
                        "txn_recovery_fixture",
                        "Commit interrupted before index synchronization",
                    )

            external_blob = subprocess.run(
                ["git", "-C", str(vault), "hash-object", "-w", "--stdin"],
                input="external staged content\n",
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(vault),
                    "update-index",
                    "--cacheinfo",
                    "100644",
                    external_blob,
                    "knowledgebase/existing.md",
                ],
                check=True,
            )

            with self.assertRaisesRegex(RecoveryError, "does not match its base"):
                recover_interrupted_transaction(vault, vault / ".git")

            self.assertEqual(
                recovery_module._git_index_file_oid(
                    vault, "knowledgebase/existing.md"
                ),
                external_blob,
            )
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
