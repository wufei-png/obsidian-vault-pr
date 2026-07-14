from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from vault_pr.retrieve import RetrievalError, retrieve_related_notes


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
    def test_merges_lexical_and_vector_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            capture = (
                vault / ".vault-pr" / "sources" / "public" / "src_0123456789abcdef0123"
            )
            capture.mkdir(parents=True)
            (capture / "manifest.json").write_text(
                json.dumps(
                    {
                        "title": "LLM Wiki",
                        "domain": "tech",
                        "artifacts": {"source_note": "knowledgebase/sources/wiki.md"},
                    }
                )
            )
            (capture / "extracted.md").write_text(
                "# LLM Wiki\n\n## Retrieval\n\n" + "knowledge " * 100
            )
            (vault / "knowledgebase" / "sources").mkdir(parents=True)
            config = {
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

            result = retrieve_related_notes(
                config,
                "src_0123456789abcdef0123",
                runner=RetrievalRunner(),
                executable="qmd",
            )

            self.assertEqual(len(result["candidates"]), 2)
            self.assertEqual(result["candidates"][0]["methods"], ["search", "vsearch"])
            self.assertEqual(
                result["candidates"][0]["path"], "knowledgebase/ai/agent/rag.md"
            )
            self.assertEqual(result["collection"], "notebook-tech")

    def test_rejects_invalid_source_id(self) -> None:
        with self.assertRaisesRegex(RetrievalError, "invalid source id"):
            retrieve_related_notes({}, "bad", executable="qmd")


if __name__ == "__main__":
    unittest.main()
