from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vault_pr.config import ConfigError
from vault_pr.plan import PlanError, apply_plan, record_review, validate_plan
from vault_pr.recovery import prepare_active_transaction

from tests.capture_fixture import source_note_text, write_capture


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def note(title: str, body: str = "Body") -> str:
    return f"---\ntitle: {title}\ntype: concept\ntags: []\naliases: []\n---\n\n# {title}\n\n{body}\n"


def make_config(vault: Path, maximum: int = 15) -> dict[str, object]:
    return {
        "vault": {"path": str(vault)},
        "ingest": {"max_touched_notes": maximum},
        "sources": {"public_archive_dir": ".vault-pr/sources/public"},
        "domains": {
            "tech": {
                "root_dir": "knowledgebase",
                "source_notes_dir": "knowledgebase/sources",
                "qmd_collection": "notebook-tech",
            },
            "work": {
                "root_dir": "work",
                "source_notes_dir": "work/sources",
                "qmd_collection": "notebook-work",
            },
        },
        "review": {"independent_for": ["merge", "delete", "contradiction", "work"]},
    }


class PlanTests(unittest.TestCase):
    def make_vault(self, root: Path) -> tuple[Path, str]:
        vault = root / "vault"
        (vault / "knowledgebase" / "sources").mkdir(parents=True)
        (vault / "work" / "sources").mkdir(parents=True)
        source_id = write_capture(vault)
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
        return vault, source_id

    def write_plan(
        self,
        root: Path,
        source_id: str,
        operations: list[dict[str, object]],
        *,
        intent: str = "integrate",
        contradiction: bool = False,
    ) -> Path:
        plan_dir = root / "plan"
        files = plan_dir / "files"
        files.mkdir(parents=True, exist_ok=True)
        source_note = root / "vault" / "knowledgebase" / "sources" / "source.md"
        (files / "source.md").write_text(source_note_text(source_id, "Integrated"))
        source_operation = {
            "action": "update",
            "path": "knowledgebase/sources/source.md",
            "expected_sha256": sha256(source_note),
            "content_file": "files/source.md",
        }
        plan_path = plan_dir / "plan.json"
        plan_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "transaction_id": "txn_20260715T000000Z_fixture",
                    "source_id": source_id,
                    "summary": "Integrate fixture",
                    "intent": intent,
                    "contradiction": contradiction,
                    "operations": [source_operation, *operations],
                }
            )
            + "\n"
        )
        return plan_path

    def test_rejects_tampered_source_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            plan_path = self.write_plan(root, source_id, [])
            capture = vault / ".vault-pr" / "sources" / "public" / source_id
            (capture / "source.html").write_text("tampered\n")

            with self.assertRaisesRegex(PlanError, "capture integrity failed"):
                validate_plan(make_config(vault), plan_path)

    def test_source_note_update_must_preserve_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            plan_path = self.write_plan(root, source_id, [])
            (root / "plan" / "files" / "source.md").write_text(
                note("Source", "Drops provenance")
            )

            with self.assertRaisesRegex(PlanError, "preserve its source_id"):
                validate_plan(make_config(vault), plan_path)

    def test_rejects_more_than_configured_note_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            operations = []
            plan_dir = root / "plan" / "files"
            plan_dir.mkdir(parents=True)
            for index in range(16):
                content = plan_dir / f"{index}.md"
                content.write_text(note(f"Note {index}"))
                operations.append(
                    {
                        "action": "create",
                        "path": f"knowledgebase/note-{index}.md",
                        "content_file": f"files/{index}.md",
                    }
                )
            plan_path = self.write_plan(root, source_id, operations)

            with self.assertRaisesRegex(PlanError, "maximum is 15"):
                validate_plan(make_config(vault), plan_path)

    def test_work_change_requires_hash_bound_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            target = vault / "work" / "existing.md"
            target.write_text(note("Existing"))
            subprocess.run(["git", "-C", str(vault), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "note"], check=True
            )
            files = root / "plan" / "files"
            files.mkdir(parents=True)
            (files / "updated.md").write_text(note("Existing", "Updated"))
            plan_path = self.write_plan(
                root,
                source_id,
                [
                    {
                        "action": "update",
                        "path": "work/existing.md",
                        "expected_sha256": sha256(target),
                        "content_file": "files/updated.md",
                    }
                ],
            )

            checked = validate_plan(make_config(vault), plan_path)
            self.assertEqual(checked["risks"], ["work"])
            with self.assertRaisesRegex(PlanError, "reviewer approval"):
                apply_plan(make_config(vault), plan_path)

            record_review(plan_path, "reviewer-agent", "approve")
            checked = validate_plan(make_config(vault), plan_path)
            self.assertTrue(checked["approval_valid"])
            (files / "updated.md").write_text(note("Existing", "Changed after review"))
            checked = validate_plan(make_config(vault), plan_path)
            self.assertFalse(checked["approval_valid"])
            (files / "updated.md").write_text(note("Existing", "Updated"))
            record_review(plan_path, "reviewer-agent", "approve")
            plan_path.write_text(plan_path.read_text().replace("fixture", "changed", 1))
            checked = validate_plan(make_config(vault), plan_path)
            self.assertFalse(checked["approval_valid"])

    def test_apply_create_and_update_archives_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            existing = vault / "knowledgebase" / "existing.md"
            existing.write_text(note("Existing"))
            subprocess.run(["git", "-C", str(vault), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "note"], check=True
            )
            files = root / "plan" / "files"
            files.mkdir(parents=True)
            (files / "updated.md").write_text(note("Existing", "Updated"))
            (files / "created.md").write_text(note("Created"))
            plan_path = self.write_plan(
                root,
                source_id,
                [
                    {
                        "action": "update",
                        "path": "knowledgebase/existing.md",
                        "expected_sha256": sha256(existing),
                        "content_file": "files/updated.md",
                    },
                    {
                        "action": "create",
                        "path": "knowledgebase/created.md",
                        "content_file": "files/created.md",
                    },
                ],
            )

            result = apply_plan(make_config(vault), plan_path)

            self.assertEqual(result["status"], "applied")
            self.assertTrue(result["requires_commit"])
            self.assertIn("Updated", existing.read_text())
            self.assertTrue((vault / "knowledgebase" / "created.md").is_file())
            archive = (
                vault / ".vault-pr" / "transactions" / "txn_20260715T000000Z_fixture"
            )
            self.assertTrue((archive / "plan.json").is_file())
            self.assertTrue((archive / "files" / "updated.md").is_file())
            self.assertTrue((archive / "result.json").is_file())
            journal = (
                vault
                / ".git"
                / "vault-pr-active"
                / result["transaction_id"]
                / "journal.json"
            )
            self.assertEqual(json.loads(journal.read_text())["phase"], "applied")

    def test_apply_rejects_content_changed_after_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            plan_path = self.write_plan(root, source_id, [])
            proposed = root / "plan" / "files" / "source.md"
            real_validate = validate_plan

            def validate_then_mutate(
                config: dict[str, object], path: Path
            ) -> dict[str, object]:
                result = real_validate(config, path)
                proposed.write_text(note("Source", "Mutated after validation"))
                return result

            with patch("vault_pr.plan.validate_plan", side_effect=validate_then_mutate):
                with self.assertRaisesRegex(PlanError, "changed during validation"):
                    apply_plan(make_config(vault), plan_path)

            source_note = vault / "knowledgebase" / "sources" / "source.md"
            self.assertIn("Captured", source_note.read_text())
            status = subprocess.run(
                ["git", "-C", str(vault), "status", "--porcelain=v1"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout, "")
            self.assertFalse((vault / ".git" / "vault-pr-active").exists())

    def test_apply_rejects_target_changed_after_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            target = vault / "knowledgebase" / "existing.md"
            target.write_text(note("Existing"))
            subprocess.run(["git", "-C", str(vault), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "note"], check=True
            )
            files = root / "plan" / "files"
            files.mkdir(parents=True)
            (files / "updated.md").write_text(note("Existing", "Planned"))
            plan_path = self.write_plan(
                root,
                source_id,
                [
                    {
                        "action": "update",
                        "path": "knowledgebase/existing.md",
                        "expected_sha256": sha256(target),
                        "content_file": "files/updated.md",
                    }
                ],
            )
            real_validate = validate_plan

            def validate_then_mutate(
                config: dict[str, object], path: Path
            ) -> dict[str, object]:
                result = real_validate(config, path)
                target.write_text(note("Existing", "Concurrent edit"))
                return result

            with patch("vault_pr.plan.validate_plan", side_effect=validate_then_mutate):
                with self.assertRaisesRegex(PlanError, "target changed"):
                    apply_plan(make_config(vault), plan_path)

            self.assertIn("Concurrent edit", target.read_text())
            self.assertFalse((vault / ".git" / "vault-pr-active").exists())

    def test_prepare_rechecks_target_after_final_apply_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            target = vault / "knowledgebase" / "existing.md"
            target.write_text(note("Existing"))
            subprocess.run(["git", "-C", str(vault), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "note"], check=True
            )
            files = root / "plan" / "files"
            files.mkdir(parents=True)
            (files / "updated.md").write_text(note("Existing", "Planned"))
            plan_path = self.write_plan(
                root,
                source_id,
                [
                    {
                        "action": "update",
                        "path": "knowledgebase/existing.md",
                        "expected_sha256": sha256(target),
                        "content_file": "files/updated.md",
                    }
                ],
            )

            def mutate_during_prepare(**kwargs: object) -> Path:
                target.write_text(note("Existing", "Raced after final check"))
                return prepare_active_transaction(**kwargs)

            with patch(
                "vault_pr.plan.prepare_active_transaction",
                side_effect=mutate_during_prepare,
            ):
                with self.assertRaisesRegex(PlanError, "changed before transaction"):
                    apply_plan(make_config(vault), plan_path)

            self.assertIn("Raced after final check", target.read_text())
            active_root = vault / ".git" / "vault-pr-active"
            self.assertFalse(
                active_root.is_dir()
                and any(path.is_dir() for path in active_root.iterdir())
            )

    def test_rename_with_content_rolls_back_if_source_delete_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            source = (vault / "knowledgebase" / "old.md").resolve()
            source.write_text(note("Old", "Original"))
            subprocess.run(["git", "-C", str(vault), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "old note"], check=True
            )
            files = root / "plan" / "files"
            files.mkdir(parents=True)
            (files / "renamed.md").write_text(note("New", "Replacement"))
            plan_path = self.write_plan(
                root,
                source_id,
                [
                    {
                        "action": "rename",
                        "from": "knowledgebase/old.md",
                        "path": "knowledgebase/new.md",
                        "expected_sha256": sha256(source),
                        "content_file": "files/renamed.md",
                    }
                ],
                intent="rename",
            )
            original_unlink = Path.unlink
            failed = False

            def fail_source_once(path: Path, *args: object, **kwargs: object) -> None:
                nonlocal failed
                if path == source and not failed:
                    failed = True
                    raise OSError("injected source delete failure")
                original_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", new=fail_source_once):
                with self.assertRaisesRegex(OSError, "injected source delete"):
                    apply_plan(make_config(vault), plan_path)

            self.assertTrue(source.is_file())
            self.assertIn("Original", source.read_text())
            self.assertFalse((vault / "knowledgebase" / "new.md").exists())
            status = subprocess.run(
                ["git", "-C", str(vault), "status", "--porcelain=v1"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout, "")

    def test_rejects_duplicate_note_operations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            files = root / "plan" / "files"
            files.mkdir(parents=True)
            (files / "one.md").write_text(note("One"))
            (files / "two.md").write_text(note("Two"))
            plan_path = self.write_plan(
                root,
                source_id,
                [
                    {
                        "action": "create",
                        "path": "knowledgebase/duplicate.md",
                        "content_file": "files/one.md",
                    },
                    {
                        "action": "create",
                        "path": "knowledgebase/duplicate.md",
                        "content_file": "files/two.md",
                    },
                ],
            )

            with self.assertRaisesRegex(PlanError, "duplicates"):
                validate_plan(make_config(vault), plan_path)

    def test_rejects_invalid_yaml_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            files = root / "plan" / "files"
            files.mkdir(parents=True)
            (files / "invalid.md").write_text("---\ntags: [broken\n---\nBody\n")
            plan_path = self.write_plan(
                root,
                source_id,
                [
                    {
                        "action": "create",
                        "path": "knowledgebase/invalid.md",
                        "content_file": "files/invalid.md",
                    }
                ],
            )

            with self.assertRaisesRegex(PlanError, "invalid frontmatter"):
                validate_plan(make_config(vault), plan_path)

    def test_rejects_transaction_id_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            files = root / "plan" / "files"
            files.mkdir(parents=True)
            (files / "created.md").write_text(note("Created"))
            plan_path = self.write_plan(
                root,
                source_id,
                [
                    {
                        "action": "create",
                        "path": "knowledgebase/created.md",
                        "content_file": "files/created.md",
                    }
                ],
            )
            plan = json.loads(plan_path.read_text())
            plan["transaction_id"] = "txn_/../../outside"
            plan_path.write_text(json.dumps(plan) + "\n")

            with self.assertRaisesRegex(PlanError, "invalid transaction_id"):
                validate_plan(make_config(vault), plan_path)

    def test_requires_source_note_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            files = root / "plan" / "files"
            files.mkdir(parents=True)
            (files / "created.md").write_text(note("Created"))
            plan_path = self.write_plan(
                root,
                source_id,
                [
                    {
                        "action": "create",
                        "path": "knowledgebase/created.md",
                        "content_file": "files/created.md",
                    }
                ],
            )
            plan = json.loads(plan_path.read_text())
            plan["operations"] = plan["operations"][1:]
            plan_path.write_text(json.dumps(plan) + "\n")

            with self.assertRaisesRegex(PlanError, "must update the captured source"):
                validate_plan(make_config(vault), plan_path)

    def test_governance_limits_cannot_be_relaxed_by_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            files = root / "plan" / "files"
            files.mkdir(parents=True)
            (files / "created.md").write_text(note("Created"))
            plan_path = self.write_plan(
                root,
                source_id,
                [
                    {
                        "action": "create",
                        "path": "knowledgebase/created.md",
                        "content_file": "files/created.md",
                    }
                ],
            )

            with self.assertRaisesRegex(ConfigError, "hard limit of 15"):
                validate_plan(make_config(vault, maximum=16), plan_path)

            config = make_config(vault)
            config["review"] = {"independent_for": []}
            with self.assertRaisesRegex(ConfigError, "cannot omit mandatory"):
                validate_plan(config, plan_path)


if __name__ == "__main__":
    unittest.main()
