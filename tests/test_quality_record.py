from __future__ import annotations

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

    def test_rejects_tampered_graph_audit_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config, audit_path, finding_id = self._fixture(root)
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            audit["audit_sha256"] = "0" * 64
            audit_path.write_text(json.dumps(audit) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                QualityRecordError, "stale or does not match"
            ):
                record_quality_findings(
                    config,
                    audit_path=audit_path,
                    finding_ids=[finding_id],
                    apply=True,
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
