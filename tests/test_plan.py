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
from vault_pr.recovery import commit_applied_transaction, prepare_active_transaction
from vault_pr.review import REQUIRED_JUDGMENTS

from tests.capture_fixture import source_note_text, write_capture


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def note(title: str, body: str = "Body") -> str:
    return f"---\ntitle: {title}\ntype: concept\ntags: []\naliases: []\n---\n\n# {title}\n\n{body}\n"


def make_config(
    vault: Path,
    maximum: int = 15,
    *,
    review_all: bool = False,
    max_revision_rounds: int = 2,
) -> dict[str, object]:
    return {
        "vault": {"path": str(vault)},
        "assets": {"directory": "attachments"},
        "semantic": {"max_touched_notes": maximum},
        "sources": {
            "capture_archive_dir": ".vault-pr/sources/public",
            "visible_notes_dir": "knowledgebase/sources",
        },
        "notes": {
            "include": ["**/*.md"],
            "exclude": [".git/**", ".obsidian/**", ".vault-pr/**"],
        },
        "review": {
            "review_all_semantic_transactions": review_all,
            "max_revision_rounds": max_revision_rounds,
        },
    }


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        freshness = patch(
            "vault_pr.plan.validate_qmd_index_freshness",
            return_value={
                "schema_version": 1,
                "eligible": True,
                "reasons": [],
            },
        )
        freshness.start()
        self.addCleanup(freshness.stop)

    def make_vault(
        self,
        root: Path,
        *,
        secondary_files: dict[str, tuple[bytes, str, str]] | None = None,
    ) -> tuple[Path, str]:
        vault = root / "vault"
        (vault / "knowledgebase" / "sources").mkdir(parents=True)
        (vault / "work" / "sources").mkdir(parents=True)
        source_id = write_capture(vault, secondary_files=secondary_files)
        (vault / "knowledgebase" / "concept.md").write_text(
            note("Concept", "Existing synthesis"),
            encoding="utf-8",
        )
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
        schema_version: int = 2,
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
        concept_note = root / "vault" / "knowledgebase" / "concept.md"
        (files / "synthesis.md").write_text(
            note("Concept", "Integrated from [[sources/source]]"),
            encoding="utf-8",
        )
        synthesis_operation = {
            "action": "update",
            "path": "knowledgebase/concept.md",
            "expected_sha256": sha256(concept_note),
            "content_file": "files/synthesis.md",
        }
        plan_path = plan_dir / "plan.json"
        plan_path.write_text(
            json.dumps(
                {
                    "schema_version": schema_version,
                    "transaction_id": "txn_20260715T000000Z_fixture",
                    "summary": "Integrate fixture",
                    "authorization": {
                        "kind": "maintenance_request",
                        "summary": "Integrate the fixture source",
                    },
                    "evidence": [
                        {"kind": "source_capture", "source_id": source_id}
                    ],
                    "intent": intent,
                    "retrieval": {"required": True},
                    "contradiction": contradiction,
                    "operations": [
                        source_operation,
                        synthesis_operation,
                        *operations,
                    ],
                }
            )
            + "\n"
        )
        return plan_path

    def record_assessment(
        self,
        config: dict[str, object],
        plan_path: Path,
        instance_id: str,
        status: str,
        *,
        reason: str = "Revise the transaction",
    ) -> dict[str, object]:
        checked = validate_plan(config, plan_path)
        packet = checked["semantic_review_packet"]
        evidence_refs = ["plan", "authorization"]
        if status == "approve":
            judgments = {
                name: {
                    "status": "pass",
                    "reason": "Checked",
                    "evidence_refs": evidence_refs,
                }
                for name in REQUIRED_JUDGMENTS
            }
            reasons: list[dict[str, object]] = []
            unassessed: list[str] = []
        else:
            judgments = {}
            reasons = [
                {
                    "code": "revision_required",
                    "message": reason,
                    "evidence_refs": ["plan"],
                }
            ]
            unassessed = list(REQUIRED_JUDGMENTS)
        assessment = {
            "schema_version": 2,
            "packet_type": "semantic_review",
            "packet_id": packet["packet_id"],
            "input_sha256": packet["input_sha256"],
            "instance_id": instance_id,
            "status": status,
            "judgments": judgments,
            "reasons": reasons,
            "unassessed": unassessed,
        }
        assessment_path = plan_path.parent / f"{instance_id}.json"
        assessment_path.write_text(json.dumps(assessment) + "\n")
        return record_review(config, plan_path, assessment_path)

    def _asset_plan(
        self,
        root: Path,
        source_id: str,
        content: bytes,
        *,
        target_stem: str = "chart",
        referenced: bool = True,
    ) -> tuple[Path, str]:
        digest = hashlib.sha256(content).hexdigest()
        target = f"attachments/{target_stem}--{digest[:12]}.png"
        plan_path = self.write_plan(
            root,
            source_id,
            [
                {
                    "action": "promote_asset",
                    "source_id": source_id,
                    "source_path": "images/chart.png",
                    "path": target,
                }
            ],
            schema_version=3,
        )
        if referenced:
            (root / "plan" / "files" / "synthesis.md").write_text(
                note(
                    "Concept",
                    f"Integrated from [[sources/source]]\n\n![[{target}]]",
                ),
                encoding="utf-8",
            )
        return plan_path, target

    def test_promotes_referenced_capture_asset_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            content = b"\x89PNG\r\n\x1a\nasset fixture"
            vault, source_id = self.make_vault(
                root,
                secondary_files={
                    "images/chart.png": (content, "image/png", "asset")
                },
            )
            plan_path, target = self._asset_plan(root, source_id, content)

            result = apply_plan(make_config(vault), plan_path)

            self.assertEqual((vault / target).read_bytes(), content)
            self.assertEqual(result["asset_promotions"][0]["status"], "created")
            self.assertEqual(result["schema_version"], 3)

    def test_reuses_existing_asset_with_identical_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            content = b"\x89PNG\r\n\x1a\nasset fixture"
            vault, source_id = self.make_vault(
                root,
                secondary_files={
                    "images/chart.png": (content, "image/png", "asset")
                },
            )
            plan_path, target = self._asset_plan(root, source_id, content)
            (vault / target).parent.mkdir()
            (vault / target).write_bytes(content)
            subprocess.run(
                ["git", "-C", str(vault), "add", target],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "existing asset"],
                check=True,
            )

            result = apply_plan(make_config(vault), plan_path)

            self.assertEqual(result["asset_promotions"][0]["status"], "reused")

    def test_rejects_supplement_asset_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            content = b"\x89PNG\r\n\x1a\nsupplement fixture"
            vault, source_id = self.make_vault(
                root,
                secondary_files={
                    "images/chart.png": (
                        content,
                        "image/png",
                        "supplement",
                    )
                },
            )
            plan_path, _target = self._asset_plan(root, source_id, content)

            with self.assertRaisesRegex(PlanError, "not a promotable"):
                validate_plan(make_config(vault), plan_path)

    def test_rejects_unreferenced_asset_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            content = b"\x89PNG\r\n\x1a\nasset fixture"
            vault, source_id = self.make_vault(
                root,
                secondary_files={
                    "images/chart.png": (content, "image/png", "asset")
                },
            )
            plan_path, _target = self._asset_plan(
                root,
                source_id,
                content,
                referenced=False,
            )

            with self.assertRaisesRegex(PlanError, "must be referenced"):
                validate_plan(make_config(vault), plan_path)

    def test_bounds_promoted_asset_bytes_per_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            content = b"\x89PNG\r\n\x1a\nasset fixture"
            vault, source_id = self.make_vault(
                root,
                secondary_files={
                    "images/chart.png": (content, "image/png", "asset")
                },
            )
            plan_path, _target = self._asset_plan(root, source_id, content)
            config = make_config(vault)
            config["sources"]["max_asset_bytes"] = len(content) - 1

            with self.assertRaisesRegex(PlanError, "asset bytes"):
                validate_plan(config, plan_path)

    def test_requires_reuse_of_existing_identical_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            content = b"\x89PNG\r\n\x1a\nasset fixture"
            vault, source_id = self.make_vault(
                root,
                secondary_files={
                    "images/chart.png": (content, "image/png", "asset")
                },
            )
            digest = hashlib.sha256(content).hexdigest()
            existing = f"attachments/existing--{digest[:12]}.png"
            (vault / existing).parent.mkdir()
            (vault / existing).write_bytes(content)
            subprocess.run(
                ["git", "-C", str(vault), "add", existing],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "existing asset"],
                check=True,
            )
            plan_path, _target = self._asset_plan(root, source_id, content)

            with self.assertRaisesRegex(
                PlanError,
                f"asset_reuse_required: {existing}",
            ):
                validate_plan(make_config(vault), plan_path)

    def test_never_overwrites_existing_asset_with_different_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            content = b"\x89PNG\r\n\x1a\nasset fixture"
            vault, source_id = self.make_vault(
                root,
                secondary_files={
                    "images/chart.png": (content, "image/png", "asset")
                },
            )
            plan_path, target = self._asset_plan(root, source_id, content)
            (vault / target).parent.mkdir()
            (vault / target).write_bytes(b"\x89PNG\r\n\x1a\ndifferent")

            with self.assertRaisesRegex(PlanError, "different content"):
                validate_plan(make_config(vault), plan_path)

    def test_rejects_symlinked_asset_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            content = b"\x89PNG\r\n\x1a\nasset fixture"
            vault, source_id = self.make_vault(
                root,
                secondary_files={
                    "images/chart.png": (content, "image/png", "asset")
                },
            )
            (vault / "real-attachments").mkdir()
            (vault / "attachments").symlink_to("real-attachments")
            plan_path, _target = self._asset_plan(root, source_id, content)

            with self.assertRaisesRegex(ConfigError, "must not traverse"):
                validate_plan(make_config(vault), plan_path)

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

            with self.assertRaisesRegex(PlanError, "preserve source_id"):
                validate_plan(make_config(vault), plan_path)

    def test_integration_requires_modified_synthesis_note_linking_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            plan_path = self.write_plan(root, source_id, [])
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["operations"] = plan["operations"][:1]
            plan_path.write_text(json.dumps(plan) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                PlanError,
                "modified synthesis note",
            ):
                validate_plan(make_config(vault), plan_path)

    def test_each_source_requires_a_modified_synthesis_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, first_source_id = self.make_vault(root)
            second_source_id = write_capture(
                vault,
                source_note="knowledgebase/sources/second.md",
                requested_url="https://example.com/second",
                title="Second Source",
            )
            subprocess.run(["git", "-C", str(vault), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "second source"],
                check=True,
            )
            plan_path = self.write_plan(root, first_source_id, [])
            files = root / "plan" / "files"
            second_note = vault / "knowledgebase" / "sources" / "second.md"
            (files / "second.md").write_text(
                source_note_text(second_source_id, "Integrated"),
                encoding="utf-8",
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["evidence"].append(
                {"kind": "source_capture", "source_id": second_source_id}
            )
            plan["operations"].append(
                {
                    "action": "update",
                    "path": "knowledgebase/sources/second.md",
                    "expected_sha256": sha256(second_note),
                    "content_file": "files/second.md",
                }
            )
            plan_path.write_text(json.dumps(plan) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                PlanError,
                second_source_id,
            ):
                validate_plan(make_config(vault), plan_path)

            (files / "synthesis.md").write_text(
                note(
                    "Concept",
                    "Integrated from [[sources/source]] and [[sources/second]]",
                ),
                encoding="utf-8",
            )
            checked = validate_plan(make_config(vault), plan_path)
            self.assertEqual(
                set(checked["source_ids"]),
                {first_source_id, second_source_id},
            )

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

    def test_review_all_policy_is_vault_wide_not_folder_based(self) -> None:
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

            self.assertEqual(validate_plan(make_config(vault), plan_path)["risks"], [])
            restricted_config = make_config(vault, review_all=True)
            checked = validate_plan(restricted_config, plan_path)
            self.assertEqual(checked["risks"], ["vault_policy"])
            with self.assertRaisesRegex(PlanError, "reviewer approval"):
                apply_plan(restricted_config, plan_path)

            self.record_assessment(
                restricted_config, plan_path, "reviewer-agent", "approve"
            )
            checked = validate_plan(restricted_config, plan_path)
            self.assertTrue(checked["approval_valid"])
            (files / "updated.md").write_text(note("Existing", "Changed after review"))
            checked = validate_plan(make_config(vault), plan_path)
            self.assertFalse(checked["approval_valid"])
            (files / "updated.md").write_text(note("Existing", "Updated"))
            self.record_assessment(
                restricted_config, plan_path, "reviewer-agent-2", "approve"
            )
            plan_path.write_text(plan_path.read_text().replace("fixture", "changed", 1))
            checked = validate_plan(restricted_config, plan_path)
            self.assertFalse(checked["approval_valid"])

    def test_split_requires_review_even_when_review_all_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            plan_path = self.write_plan(root, source_id, [], intent="split")

            checked = validate_plan(
                make_config(vault, review_all=False),
                plan_path,
            )

            self.assertIn("split", checked["risks"])
            self.assertTrue(checked["approval_required"])

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
            self.assertFalse((archive / "files" / "updated.md").exists())
            self.assertTrue((archive / "result.json").is_file())
            journal = (
                vault
                / ".git"
                / "vault-pr-active"
                / result["transaction_id"]
                / "journal.json"
            )
            self.assertEqual(json.loads(journal.read_text())["phase"], "applied")

    def test_apply_retry_returns_active_then_committed_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            plan_path = self.write_plan(root, source_id, [])
            config = make_config(vault)

            first = apply_plan(config, plan_path)
            active_retry = apply_plan(config, plan_path)

            self.assertEqual(active_retry["status"], "already_active")
            self.assertEqual(active_retry["transaction_id"], first["transaction_id"])
            self.assertEqual(active_retry["next_action"], "commit")
            self.assertTrue(active_retry["requires_commit"])

            commit_applied_transaction(
                vault,
                vault / ".git",
                first["transaction_id"],
                "Apply semantic plan",
            )
            committed_retry = apply_plan(config, plan_path)
            self.assertEqual(committed_retry["status"], "already_committed")
            self.assertEqual(
                committed_retry["transaction_id"], first["transaction_id"]
            )
            self.assertFalse(committed_retry["requires_commit"])

    def test_review_revision_rounds_are_bounded_and_require_changed_content(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            plan_path = self.write_plan(root, source_id, [])
            config = make_config(vault, review_all=True, max_revision_rounds=1)
            proposed = root / "plan" / "files" / "source.md"

            first = self.record_assessment(
                config, plan_path, "reviewer-1", "reject", reason="revise"
            )
            self.assertEqual(first["remaining_revision_rounds"], 1)
            with self.assertRaisesRegex(PlanError, "must change after rejection"):
                self.record_assessment(
                    config, plan_path, "reviewer-2", "approve"
                )

            proposed.write_text(source_note_text(source_id, "Revision one"))
            self.record_assessment(
                config, plan_path, "reviewer-2", "reject"
            )
            proposed.write_text(source_note_text(source_id, "Revision two"))
            with self.assertRaisesRegex(PlanError, "revision limit exhausted"):
                self.record_assessment(
                    config, plan_path, "reviewer-3", "approve"
                )

    def test_apply_rechecks_revision_limit_with_the_vault_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            plan_path = self.write_plan(root, source_id, [])
            proposed = root / "plan" / "files" / "source.md"
            permissive = make_config(vault, review_all=True, max_revision_rounds=5)

            for revision in range(3):
                self.record_assessment(
                    permissive,
                    plan_path,
                    f"reviewer-{revision}",
                    "reject",
                )
                proposed.write_text(
                    source_note_text(source_id, f"Revision {revision + 1}")
                )
            self.record_assessment(
                permissive, plan_path, "reviewer-final", "approve"
            )

            configured = make_config(vault, review_all=True, max_revision_rounds=2)
            with self.assertRaisesRegex(PlanError, "revision limit exceeded"):
                apply_plan(configured, plan_path)

    def test_apply_rejects_approval_for_unchanged_rejected_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            plan_path = self.write_plan(root, source_id, [])
            config = make_config(vault, review_all=True)
            rejected = self.record_assessment(
                config, plan_path, "reviewer-1", "reject"
            )
            history_path = Path(rejected["review_history"])
            approval_path = Path(rejected["assessment_path"])
            history = json.loads(history_path.read_text())
            forged_assessment = {
                **history[-1]["assessment"],
                "status": "approve",
                "instance_id": "reviewer-2",
                "judgments": {
                    name: {
                        "status": "pass",
                        "reason": "Checked",
                        "evidence_refs": ["plan"],
                    }
                    for name in REQUIRED_JUDGMENTS
                },
                "reasons": [],
                "unassessed": [],
            }
            forged = {**history[-1], "assessment": forged_assessment}
            history.append(forged)
            history_path.write_text(json.dumps(history) + "\n")
            approval_path.write_text(json.dumps(forged_assessment) + "\n")

            with self.assertRaisesRegex(PlanError, "already rejected"):
                apply_plan(config, plan_path)

    def test_rejection_requires_approval_even_when_review_is_not_policy_mandated(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            plan_path = self.write_plan(root, source_id, [])
            config = make_config(vault)
            self.record_assessment(config, plan_path, "reviewer-1", "reject")

            checked = validate_plan(config, plan_path)
            self.assertEqual(checked["risks"], ["review_history"])
            with self.assertRaisesRegex(PlanError, "reviewer approval"):
                apply_plan(config, plan_path)

    def test_apply_archives_the_complete_review_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            plan_path = self.write_plan(root, source_id, [])
            config = make_config(vault, review_all=True)
            self.record_assessment(config, plan_path, "reviewer-1", "approve")

            result = apply_plan(config, plan_path)

            archive = vault / ".vault-pr" / "transactions" / result["transaction_id"]
            history = json.loads((archive / "review-history.json").read_text())
            self.assertEqual(history[0]["assessment"]["instance_id"], "reviewer-1")
            self.assertEqual(history[0]["assessment"]["status"], "approve")

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
                "vault_pr.mutation.prepare_active_transaction",
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
                with self.assertRaisesRegex(PlanError, "injected source delete"):
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

    def test_rejects_unknown_normal_operation_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            plan_path = self.write_plan(root, source_id, [])
            plan = json.loads(plan_path.read_text())
            plan["operations"][0]["ignored"] = True
            plan_path.write_text(json.dumps(plan) + "\n")

            with self.assertRaisesRegex(PlanError, "unknown or missing fields"):
                validate_plan(make_config(vault), plan_path)

    def test_autonomous_repair_cannot_update_unrelated_note(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, _source_id = self.make_vault(root)
            owner = vault / "knowledgebase" / "owner.md"
            unrelated = vault / "knowledgebase" / "unrelated.md"
            owner.write_text(note("Owner", "Broken [[Missing]]."))
            unrelated.write_text(note("Unrelated"))
            subprocess.run(["git", "-C", str(vault), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "quality fixture"],
                check=True,
            )
            plan_dir = root / "autonomous-plan"
            files = plan_dir / "files"
            files.mkdir(parents=True)
            (files / "unrelated.md").write_text(note("Unrelated", "Changed"))
            record_id = "quality_11111111111111111111"
            record_path = root / f"{record_id}.json"
            record_path.write_text("{}\n")
            plan_path = plan_dir / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "transaction_id": "txn_autonomous_scope",
                        "summary": "Repair selected quality finding",
                        "authorization": {
                            "kind": "autonomous_repair_policy",
                            "summary": "Apply deterministic quality repair",
                        },
                        "evidence": [
                            {
                                "kind": "quality_record",
                                "record_id": record_id,
                                "finding_ids": ["finding_fixture"],
                            }
                        ],
                        "intent": "maintenance",
                        "retrieval": {
                            "required": False,
                            "reason": "quality record identifies the owner",
                        },
                        "contradiction": False,
                        "operations": [
                            {
                                "action": "update",
                                "path": "knowledgebase/unrelated.md",
                                "expected_sha256": sha256(unrelated),
                                "content_file": "files/unrelated.md",
                            }
                        ],
                    }
                )
                + "\n"
            )
            finding = {
                "finding_id": "finding_fixture",
                "rule_id": "broken_wikilink",
                "severity": "error",
                "path": "knowledgebase/owner.md",
                "owner_sha256": sha256(owner),
                "target": "Missing",
                "blocking_scope": "owning_transaction",
                "autonomous_capability": "repair_broken_wikilink",
            }
            config = make_config(vault)
            config["autonomous_repair"] = {"enabled": True}
            with (
                patch(
                    "vault_pr.plan.load_quality_record",
                    return_value=({}, record_path),
                ),
                patch(
                    "vault_pr.plan.revalidate_quality_record",
                    return_value=[finding],
                ),
            ):
                with self.assertRaisesRegex(
                    PlanError, "only update a selected finding owner"
                ):
                    validate_plan(config, plan_path)

    def test_autonomous_repair_can_add_incoming_source_synthesis_link(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            source = vault / "knowledgebase" / "sources" / "source.md"
            concept = vault / "knowledgebase" / "concept.md"
            plan_dir = root / "autonomous-source-plan"
            files = plan_dir / "files"
            files.mkdir(parents=True)
            (files / "concept.md").write_text(
                note("Concept", "Synthesized from [[sources/source]]"),
                encoding="utf-8",
            )
            record_id = "quality_22222222222222222222"
            record_path = root / f"{record_id}.json"
            record_path.write_text("{}\n", encoding="utf-8")
            plan_path = plan_dir / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "transaction_id": "txn_autonomous_source_link",
                        "summary": "Link source from synthesis",
                        "authorization": {
                            "kind": "autonomous_repair_policy",
                            "summary": "Apply deterministic quality repair",
                        },
                        "evidence": [
                            {
                                "kind": "quality_record",
                                "record_id": record_id,
                                "finding_ids": ["finding_source"],
                            }
                        ],
                        "intent": "maintenance",
                        "retrieval": {
                            "required": False,
                            "reason": "quality record identifies the missing link",
                        },
                        "contradiction": False,
                        "operations": [
                            {
                                "action": "update",
                                "path": "knowledgebase/concept.md",
                                "expected_sha256": sha256(concept),
                                "content_file": "files/concept.md",
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            finding = {
                "finding_id": "finding_source",
                "rule_id": "source_without_synthesis_link",
                "severity": "warning",
                "path": "knowledgebase/sources/source.md",
                "owner_sha256": sha256(source),
                "blocking_scope": "none",
                "autonomous_capability": "repair_source_synthesis_link",
            }
            config = make_config(vault)
            config["autonomous_repair"] = {"enabled": True}
            with (
                patch(
                    "vault_pr.plan.load_quality_record",
                    return_value=({}, record_path),
                ),
                patch(
                    "vault_pr.plan.revalidate_quality_record",
                    return_value=[finding],
                ),
            ):
                checked = validate_plan(config, plan_path)

            self.assertEqual(
                checked["operations"][0]["path"],
                concept.resolve(),
            )

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

            with self.assertRaisesRegex(PlanError, "must update source note"):
                validate_plan(make_config(vault), plan_path)

    def test_maintenance_source_evidence_does_not_require_source_note_update(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            files = root / "plan" / "files"
            files.mkdir(parents=True)
            (files / "created.md").write_text(note("Created"), encoding="utf-8")
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
                intent="maintenance",
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["retrieval"] = {
                "required": False,
                "reason": "The maintenance request already identifies the target",
            }
            plan["operations"] = plan["operations"][1:]
            plan_path.write_text(json.dumps(plan) + "\n", encoding="utf-8")

            result = validate_plan(make_config(vault), plan_path)

            self.assertEqual(result["plan"]["intent"], "maintenance")

    def test_rejects_retrieval_note_as_persistent_transaction_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            plan_path = self.write_plan(root, source_id, [])
            retrieval_note = vault / "knowledgebase" / "sources" / "source.md"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["evidence"].append(
                {
                    "kind": "retrieval_note",
                    "path": "knowledgebase/sources/source.md",
                    "sha256": sha256(retrieval_note),
                }
            )
            plan_path.write_text(json.dumps(plan) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(PlanError, "unsupported kind"):
                validate_plan(make_config(vault), plan_path)

    def test_purge_capture_requires_review_and_deletes_capture_and_source_note(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            plan_dir = root / "purge-plan"
            plan_dir.mkdir()
            plan_path = plan_dir / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "transaction_id": "txn_purge_fixture",
                        "summary": "Purge obsolete capture",
                        "authorization": {
                            "kind": "maintenance_request",
                            "summary": "Remove the obsolete capture and Source Note",
                        },
                        "evidence": [],
                        "intent": "maintenance",
                        "retrieval": {
                            "required": False,
                            "reason": "purge safety is checked by exact references",
                        },
                        "contradiction": False,
                        "operations": [
                            {
                                "action": "purge_capture",
                                "source_id": source_id,
                            }
                        ],
                    }
                )
                + "\n"
            )
            checked = validate_plan(make_config(vault), plan_path)
            self.assertIn("purge_capture", checked["risks"])
            self.assertTrue(checked["approval_required"])
            self.record_assessment(
                make_config(vault), plan_path, "purge-reviewer", "approve"
            )

            applied = apply_plan(make_config(vault), plan_path)

            self.assertEqual(applied["status"], "applied")
            self.assertFalse(
                (
                    vault / ".vault-pr" / "sources" / "public" / source_id
                ).exists()
            )
            self.assertFalse(
                (vault / "knowledgebase" / "sources" / "source.md").exists()
            )

    def test_purge_capture_blocks_remaining_source_id_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, source_id = self.make_vault(root)
            related = vault / "knowledgebase" / "related.md"
            related.write_text(f"Provenance: {source_id}\n")
            subprocess.run(["git", "-C", str(vault), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "Add reference"],
                check=True,
            )
            plan_dir = root / "purge-plan"
            plan_dir.mkdir()
            plan_path = plan_dir / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "transaction_id": "txn_purge_blocked",
                        "summary": "Purge referenced capture",
                        "authorization": {
                            "kind": "maintenance_request",
                            "summary": "Remove obsolete source",
                        },
                        "evidence": [],
                        "intent": "maintenance",
                        "retrieval": {
                            "required": False,
                            "reason": "reference scan is deterministic",
                        },
                        "contradiction": False,
                        "operations": [
                            {
                                "action": "purge_capture",
                                "source_id": source_id,
                            }
                        ],
                    }
                )
                + "\n"
            )
            with self.assertRaisesRegex(PlanError, "remaining source_id references"):
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

            config = make_config(vault, max_revision_rounds=6)
            with self.assertRaisesRegex(ConfigError, "hard limit of 5"):
                validate_plan(config, plan_path)


if __name__ == "__main__":
    unittest.main()
