from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from vault_pr.retrieve import RetrievalError, retrieve_related_notes

from tests.capture_fixture import write_capture


class RetrievalRunner:
    def __call__(
        self, command: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        method = command[3]
        if method == "search":
            results = [
                {
                    "file": "qmd://notebook-tech/ai/agent/rag.md?index=test",
                    "title": "RAG",
                    "score": 0.9,
                    "snippet": "retrieval",
                }
            ]
        elif method == "vsearch":
            results = [
                {
                    "file": "qmd://notebook-tech/ai/agent/rag.md?index=test",
                    "title": "RAG",
                    "score": 0.7,
                    "snippet": "semantic",
                },
                {
                    "file": "qmd://notebook-tech/ai/agent/agents.md?index=test",
                    "title": "Agents",
                    "score": 0.6,
                    "snippet": "agents",
                },
            ]
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(results), "")


class RetrieveTests(unittest.TestCase):
    @staticmethod
    def config(vault: Path) -> dict[str, object]:
        return {
            "vault": {"path": str(vault)},
            "sources": {"public_archive_dir": ".vault-pr/sources/public"},
            "domains": {
                "tech": {
                    "root_dir": "knowledgebase",
                    "source_notes_dir": "knowledgebase/sources",
                    "qmd_collection": "notebook-tech",
                }
            },
            "index": {"name": "test"},
        }

    def test_merges_lexical_and_vector_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            source_id = write_capture(
                vault, source_note="knowledgebase/sources/wiki.md"
            )
            result = retrieve_related_notes(
                self.config(vault),
                source_id,
                runner=RetrievalRunner(),
                executable="qmd",
            )

            self.assertEqual(len(result["candidates"]), 2)
            self.assertEqual(result["candidates"][0]["methods"], ["search", "vsearch"])
            self.assertEqual(
                result["candidates"][0]["path"], "knowledgebase/ai/agent/rag.md"
            )
            self.assertEqual(result["collection"], "notebook-tech")

    def test_rejects_tampered_capture_before_querying_qmd(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            source_id = write_capture(vault)
            capture = vault / ".vault-pr" / "sources" / "public" / source_id
            (capture / "extracted.md").write_text("tampered\n")

            with self.assertRaisesRegex(RetrievalError, "integrity failed"):
                retrieve_related_notes(
                    self.config(vault),
                    source_id,
                    runner=RetrievalRunner(),
                    executable="qmd",
                )

    def test_rejects_tampered_capture_domain_routing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            source_id = write_capture(vault)
            manifest_path = (
                vault / ".vault-pr" / "sources" / "public" / source_id / "manifest.json"
            )
            manifest = json.loads(manifest_path.read_text())
            manifest["domain"] = "work"
            manifest_path.write_text(json.dumps(manifest))

            with self.assertRaisesRegex(RetrievalError, "immutable identity"):
                retrieve_related_notes(self.config(vault), source_id, executable="qmd")

    def test_rejects_tampered_source_note_association(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            source_id = write_capture(vault)
            capture = vault / ".vault-pr" / "sources" / "public" / source_id
            manifest_path = capture / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            original = vault / manifest["artifacts"]["source_note"]
            replacement = original.with_name("replacement.md")
            replacement.write_text(original.read_text())
            manifest["artifacts"]["source_note"] = str(replacement.relative_to(vault))
            manifest_path.write_text(json.dumps(manifest))

            with self.assertRaisesRegex(RetrievalError, "immutable identity"):
                retrieve_related_notes(self.config(vault), source_id, executable="qmd")

    def test_rejects_invalid_source_id(self) -> None:
        with self.assertRaisesRegex(RetrievalError, "invalid source id"):
            retrieve_related_notes({}, "bad", executable="qmd")


if __name__ == "__main__":
    unittest.main()
