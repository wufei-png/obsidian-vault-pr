from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vault_pr.quality import audit_graph
from vault_pr.mutation import MutationError
from vault_pr.quality_record import (
    QualityRecordError,
    load_quality_record,
    record_quality_findings,
    revalidate_quality_record,
)
from vault_pr.recovery import commit_applied_transaction


def _git(vault: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        check=False,
        capture_output=True,
        text=True,
    )


class QualityRecordTests(unittest.TestCase):
    def _fixture(
        self, root: Path
    ) -> tuple[Path, dict[str, object], Path, str]:
        vault = root / "vault"
        vault.mkdir()
        _git(vault, "init", "-q")
        _git(vault, "config", "user.email", "fixture@example.com")
        _git(vault, "config", "user.name", "Fixture")
        (vault / "owner.md").write_text(
            "---\ntype: concept\n---\n\n# Owner\n\n[[Missing]]\n",
            encoding="utf-8",
        )
        _git(vault, "add", "--all")
        _git(vault, "commit", "-qm", "quality fixture")
        config: dict[str, object] = {
            "vault": {"path": str(vault)},
            "notes": {"include": ["**/*.md"]},
        }
        audit = audit_graph(config)
        finding = next(
            item
            for item in audit["findings"]
            if item["rule_id"] == "broken_wikilink"
        )
        audit_path = root / "audit.json"
        audit_path.write_text(json.dumps(audit) + "\n", encoding="utf-8")
        return vault, config, audit_path, finding["finding_id"]

    def test_records_and_commits_selected_autonomous_repair_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, config, audit_path, finding_id = self._fixture(root)

            planned = record_quality_findings(
                config,
                audit_path=audit_path,
                finding_ids=[finding_id],
            )
            applied = record_quality_findings(
                config,
                audit_path=audit_path,
                finding_ids=[finding_id],
                apply=True,
            )

            self.assertEqual(planned["record_id"], applied["record_id"])
            record, path = load_quality_record(vault, applied["record_id"])
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["assessment"]["input_sha256"], audit["audit_sha256"]
            )
            self.assertEqual(record["schema_version"], 2)
            self.assertEqual(
                record["assessment"]["graph_audit_schema_version"],
                audit["schema_version"],
            )
            self.assertEqual(record["findings"][0]["finding_id"], finding_id)
            self.assertTrue(path.is_file())
            git_dir = Path(
                _git(vault, "rev-parse", "--absolute-git-dir").stdout.strip()
            )
            committed = commit_applied_transaction(
                vault,
                git_dir,
                applied["transaction_id"],
                "Record quality repair evidence",
            )
            self.assertEqual(committed["status"], "committed_and_finalized")
            self.assertFalse(_git(vault, "status", "--porcelain=v1").stdout)
            selected = revalidate_quality_record(
                config,
                record,
                [finding_id],
            )
            self.assertEqual(selected[0]["finding_id"], finding_id)

    def test_rejects_forged_receipt_that_does_not_bind_record_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, config, audit_path, finding_id = self._fixture(root)
            applied = record_quality_findings(
                config,
                audit_path=audit_path,
                finding_ids=[finding_id],
                apply=True,
            )
            git_dir = Path(
                _git(vault, "rev-parse", "--absolute-git-dir").stdout.strip()
            )
            commit_applied_transaction(
                vault,
                git_dir,
                applied["transaction_id"],
                "Record quality repair evidence",
            )
            record, _path = load_quality_record(vault, applied["record_id"])
            record["assessment"]["input_sha256"] = "f" * 64
            core = {
                "assessment": record["assessment"],
                "findings": record["findings"],
            }
            digest = hashlib.sha256(
                json.dumps(
                    core,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            forged_id = f"quality_{digest[:20]}"
            record["record_id"] = forged_id
            forged_path = (
                vault
                / ".vault-pr"
                / "quality"
                / "records"
                / f"{forged_id}.json"
            )
            forged_path.write_text(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            _git(vault, "add", str(forged_path.relative_to(vault)))
            _git(vault, "commit", "-qm", "forged record fixture")
            record_sha256 = hashlib.sha256(
                forged_path.read_bytes()
            ).hexdigest()
            transaction_id = f"txn_quality_{digest[:16]}"
            receipt_path = (
                vault
                / ".vault-pr"
                / "transaction-receipts"
                / f"{transaction_id}.json"
            )
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_type": "vault_pr_transaction_receipt",
                        "operation": "quality_record",
                        "transaction_id": transaction_id,
                        "transaction_sha256": digest,
                        "request_sha256": digest,
                        "completion_path": str(
                            forged_path.relative_to(vault)
                        ),
                        "result": {
                            "record_id": forged_id,
                            "path": str(forged_path.relative_to(vault)),
                            "record_sha256": record_sha256,
                            "finding_ids": [finding_id],
                        },
                        "files": [],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            _git(vault, "add", str(receipt_path.relative_to(vault)))
            _git(vault, "commit", "-qm", "forged receipt fixture")

            forged, _path = load_quality_record(vault, forged_id)
            with self.assertRaisesRegex(
                QualityRecordError,
                "does not bind its content",
            ):
                revalidate_quality_record(config, forged, [finding_id])

    def test_rejects_open_or_incomplete_assessment_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, config, audit_path, finding_id = self._fixture(root)
            applied = record_quality_findings(
                config,
                audit_path=audit_path,
                finding_ids=[finding_id],
                apply=True,
            )
            record, path = load_quality_record(vault, applied["record_id"])
            del record["assessment"]["graph_audit_schema_version"]
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                QualityRecordError,
                "assessment schema",
            ):
                load_quality_record(vault, applied["record_id"])

    def test_rejects_tampered_graph_audit_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config, audit_path, finding_id = self._fixture(root)
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            audit["audit_sha256"] = "0" * 64
            audit_path.write_text(json.dumps(audit) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                QualityRecordError, "digest does not match"
            ):
                record_quality_findings(
                    config,
                    audit_path=audit_path,
                    finding_ids=[finding_id],
                    apply=True,
                )

    def test_rejects_tampered_graph_audit_content_with_original_digest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config, audit_path, finding_id = self._fixture(root)
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            audit["metrics"]["findings"] += 1
            audit_path.write_text(json.dumps(audit) + "\n", encoding="utf-8")

            for apply in (False, True):
                with self.subTest(apply=apply):
                    with self.assertRaisesRegex(
                        QualityRecordError,
                        "digest does not match",
                    ):
                        record_quality_findings(
                            config,
                            audit_path=audit_path,
                            finding_ids=[finding_id],
                            apply=apply,
                        )

    def test_rechecks_graph_audit_after_acquiring_mutation_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, config, audit_path, finding_id = self._fixture(root)
            from vault_pr import quality_record as quality_record_module

            real_execute = quality_record_module.execute_journaled_mutation

            def commit_graph_change_before_lock(**kwargs: object) -> object:
                owner = vault / "owner.md"
                owner.write_text(
                    owner.read_text(encoding="utf-8") + "\nChanged\n",
                    encoding="utf-8",
                )
                _git(vault, "add", "owner.md")
                _git(vault, "commit", "-qm", "concurrent graph change")
                return real_execute(**kwargs)

            with patch(
                "vault_pr.quality_record.execute_journaled_mutation",
                side_effect=commit_graph_change_before_lock,
            ):
                with self.assertRaisesRegex(
                    QualityRecordError,
                    "graph audit is stale",
                ):
                    record_quality_findings(
                        config,
                        audit_path=audit_path,
                        finding_ids=[finding_id],
                        apply=True,
                    )

            self.assertFalse(
                (
                    vault
                    / ".vault-pr"
                    / "quality"
                    / "records"
                ).exists()
            )

    def test_dry_run_rejects_graph_audit_from_previous_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, config, audit_path, finding_id = self._fixture(root)
            owner = vault / "owner.md"
            owner.write_text(
                owner.read_text(encoding="utf-8") + "\nChanged\n",
                encoding="utf-8",
            )
            _git(vault, "add", "owner.md")
            _git(vault, "commit", "-qm", "change graph after audit")

            with self.assertRaisesRegex(
                QualityRecordError,
                "graph audit is stale",
            ):
                record_quality_findings(
                    config,
                    audit_path=audit_path,
                    finding_ids=[finding_id],
                    apply=False,
                )

    def test_write_failure_rolls_back_and_uses_domain_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config, audit_path, finding_id = self._fixture(root)

            with patch(
                "vault_pr.quality_record.execute_journaled_mutation",
                side_effect=MutationError("mutation failed: injected failure"),
            ):
                with self.assertRaisesRegex(QualityRecordError, "mutation failed"):
                    record_quality_findings(
                        config,
                        audit_path=audit_path,
                        finding_ids=[finding_id],
                        apply=True,
                    )

    def test_retries_return_active_then_committed_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, config, audit_path, finding_id = self._fixture(root)

            first = record_quality_findings(
                config,
                audit_path=audit_path,
                finding_ids=[finding_id],
                apply=True,
            )
            active_retry = record_quality_findings(
                config,
                audit_path=audit_path,
                finding_ids=[finding_id],
                apply=True,
            )

            self.assertEqual(active_retry["status"], "already_active")
            self.assertEqual(active_retry["transaction_id"], first["transaction_id"])
            self.assertEqual(active_retry["next_action"], "commit")
            self.assertTrue(active_retry["requires_commit"])

            git_dir = Path(
                _git(vault, "rev-parse", "--absolute-git-dir").stdout.strip()
            )
            commit_applied_transaction(
                vault,
                git_dir,
                first["transaction_id"],
                "Record quality repair evidence",
            )
            committed_retry = record_quality_findings(
                config,
                audit_path=audit_path,
                finding_ids=[finding_id],
                apply=True,
            )
            self.assertEqual(committed_retry["status"], "already_committed")
            self.assertEqual(
                committed_retry["transaction_id"], first["transaction_id"]
            )
            self.assertFalse(committed_retry["requires_commit"])


if __name__ == "__main__":
    unittest.main()
