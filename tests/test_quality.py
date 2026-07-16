from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from vault_pr.quality import audit_graph


def _git(vault: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(vault), *args],
        check=True,
        capture_output=True,
        text=True,
    )


class GraphQualityTests(unittest.TestCase):
    def test_reports_role_specific_findings_and_graph_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            _git(vault, "init", "-q")
            _git(vault, "config", "user.name", "Fixture")
            _git(vault, "config", "user.email", "fixture@example.com")
            (vault / "concept.md").write_text(
                "---\ntype: concept\n---\n\n"
                "# Concept\n\n[[sources/source]]\n\n[[missing]]\n",
                encoding="utf-8",
            )
            source = vault / "sources" / "source.md"
            source.parent.mkdir()
            source.write_text(
                "---\ntype: source\n---\n\n# Source\n",
                encoding="utf-8",
            )
            (vault / "index.md").write_text(
                "---\ntype: index\n---\n\n# Index\n\n[[concept]]\n",
                encoding="utf-8",
            )
            (vault / "isolated.md").write_text(
                "---\ntype: note\n---\n\n# Isolated\n",
                encoding="utf-8",
            )
            (vault / "unknown.md").write_text("# Unknown\n", encoding="utf-8")
            (vault / "examples.md").write_text(
                "---\ntype: concept\n---\n\n"
                "`[[inline-example]]`\n\n```sh\necho '[[fenced-example]]'\n```\n"
                "if [[ -z \"${NODES}\" ]]; then\n"
                "Citation [[35]](https://example.com/reference).\n",
                encoding="utf-8",
            )
            (vault / "prompt.md").write_text(
                "---\ntype: prompt-template\n---\n\n[[isolated]]\n",
                encoding="utf-8",
            )
            _git(vault, "add", "--all")
            _git(vault, "commit", "-qm", "Seed graph fixture")
            config = {
                "vault": {"path": str(vault)},
                "notes": {"include": ["**/*.md"]},
            }

            result = audit_graph(config)

            self.assertTrue(result["git_clean"])
            self.assertEqual(result["metrics"]["tracked_notes"], 7)
            self.assertEqual(result["metrics"]["resolved_edges"], 2)
            finding_ids = {finding["rule_id"] for finding in result["findings"]}
            self.assertIn("broken_wikilink", finding_ids)
            self.assertIn("isolated_concept", finding_ids)
            self.assertIn("unclassified_note_role", finding_ids)
            self.assertNotIn("source_without_synthesis_link", finding_ids)
            broken = next(
                finding
                for finding in result["findings"]
                if finding["rule_id"] == "broken_wikilink"
            )
            self.assertEqual(broken["blocking_scope"], "owning_transaction")
            packet = result["quality_assessor_packet"]
            payload = packet["payload"]
            self.assertEqual(len(payload["note_inventory"]), 7)
            broken_targets = {
                finding.get("target")
                for finding in result["findings"]
                if finding["rule_id"] == "broken_wikilink"
            }
            self.assertNotIn("inline-example", broken_targets)
            self.assertNotIn("fenced-example", broken_targets)
            self.assertNotIn('-z "${NODES}"', broken_targets)
            self.assertNotIn("35", broken_targets)
            evidence = {
                item["kind"]: item["sha256"] for item in payload["evidence"]
            }
            findings_sha256 = hashlib.sha256(
                json.dumps(
                    result["findings"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            self.assertEqual(evidence["graph_findings"], findings_sha256)
            packet_sha256 = hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            self.assertEqual(packet["input_sha256"], packet_sha256)
            self.assertEqual(packet["packet_id"], f"pkt_{packet_sha256[:20]}")

    def test_source_requires_an_incoming_synthesis_link(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            _git(vault, "init", "-q")
            _git(vault, "config", "user.name", "Fixture")
            _git(vault, "config", "user.email", "fixture@example.com")
            (vault / "concept.md").write_text(
                "---\ntype: concept\n---\n\n# Concept\n",
                encoding="utf-8",
            )
            source = vault / "sources" / "source.md"
            source.parent.mkdir()
            source.write_text(
                "---\ntype: source\n---\n\n# Source\n\n[[../concept]]\n",
                encoding="utf-8",
            )
            _git(vault, "add", "--all")
            _git(vault, "commit", "-qm", "Seed graph fixture")

            result = audit_graph(
                {
                    "vault": {"path": str(vault)},
                    "notes": {"include": ["**/*.md"]},
                }
            )

            finding = next(
                item
                for item in result["findings"]
                if item["rule_id"] == "source_without_synthesis_link"
            )
            self.assertEqual(finding["path"], "sources/source.md")

    def test_dirty_graph_audit_cannot_become_a_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            _git(vault, "init", "-q")
            _git(vault, "config", "user.name", "Fixture")
            _git(vault, "config", "user.email", "fixture@example.com")
            note = vault / "concept.md"
            note.write_text("---\ntype: concept\n---\n", encoding="utf-8")
            _git(vault, "add", "--all")
            _git(vault, "commit", "-qm", "Seed graph fixture")
            note.write_text("---\ntype: concept\n---\n\nDirty.\n", encoding="utf-8")

            result = audit_graph(
                {"vault": {"path": str(vault)}, "notes": {"include": ["**/*.md"]}}
            )

            self.assertFalse(result["git_clean"])


if __name__ == "__main__":
    unittest.main()
