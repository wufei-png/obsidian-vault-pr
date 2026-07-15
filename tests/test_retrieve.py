from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from vault_pr.retrieve import RetrievalError, _qmd_handelize, retrieve_related_notes

from tests.capture_fixture import write_capture


class RetrievalRunner:
    def __call__(
        self, command: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        method = command[3]
        if method == "search":
            results = [
                {
                    "file": "qmd://fixture-vault/knowledgebase/ai/agent/rag.md?index=test",
                    "title": "RAG",
                    "score": 0.9,
                    "snippet": "retrieval",
                }
            ]
        elif method == "vsearch":
            results = [
                {
                    "file": "qmd://fixture-vault/knowledgebase/ai/agent/rag.md?index=test",
                    "title": "RAG",
                    "score": 0.7,
                    "snippet": "semantic",
                },
                {
                    "file": "qmd://fixture-vault/knowledgebase/ai/agent/agents.md?index=test",
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
            "index": {
                "name": "test",
                "collection": "fixture-vault",
            },
        }

    def test_merges_lexical_and_vector_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            source_id = write_capture(
                vault, source_note="knowledgebase/sources/wiki.md"
            )
            notes = vault / "knowledgebase" / "ai" / "agent"
            notes.mkdir(parents=True)
            (notes / "rag.md").write_text("# RAG\n")
            (notes / "agents.md").write_text("# Agents\n")
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
            self.assertEqual(result["collection"], "fixture-vault")

    def test_matches_qmd_virtual_path_normalization(self) -> None:
        cases = {
            "ai/llms/Attention Residuals.md": "ai/llms/Attention-Residuals.md",
            ("sources/Kimi新架构让马斯克叹服!17岁高中生作者一战成名--0cf261d9.md"): (
                "sources/Kimi新架构让马斯克叹服-17岁高中生作者一战成名-0cf261d9.md"
            ),
            "animals/🐘 note.md": "animals/1f418-note.md",
            "nested___note.md": "nested/note.md",
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(_qmd_handelize(original), expected)

    def test_resolves_qmd_normalized_path_to_exact_vault_filename(self) -> None:
        class NormalizedPathRunner:
            def __call__(
                self, command: list[str], **_: object
            ) -> subprocess.CompletedProcess[str]:
                result = [
                    {
                        "file": (
                            "qmd://fixture-vault/knowledgebase/ai/llms/"
                            "Attention-Residuals.md?index=test"
                        ),
                        "title": "Attention Residuals",
                        "score": 0.8,
                    }
                ]
                return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            source_id = write_capture(vault)
            notes = vault / "knowledgebase" / "ai" / "llms"
            notes.mkdir(parents=True)
            (notes / "Attention Residuals.md").write_text("# Attention Residuals\n")

            result = retrieve_related_notes(
                self.config(vault),
                source_id,
                runner=NormalizedPathRunner(),
                executable="qmd",
            )

            self.assertEqual(
                result["candidates"][0]["path"],
                "knowledgebase/ai/llms/Attention Residuals.md",
            )

    def test_rejects_ambiguous_qmd_normalized_paths(self) -> None:
        class AmbiguousPathRunner:
            def __call__(
                self, command: list[str], **_: object
            ) -> subprocess.CompletedProcess[str]:
                result = [
                    {
                        "file": "qmd://fixture-vault/knowledgebase/ai/A-B.md?index=test",
                        "title": "A B",
                        "score": 0.8,
                    }
                ]
                return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            source_id = write_capture(vault)
            notes = vault / "knowledgebase" / "ai"
            notes.mkdir(parents=True)
            (notes / "A B.md").write_text("# A B\n")
            (notes / "A-B.md").write_text("# A-B\n")

            with self.assertRaisesRegex(RetrievalError, "ambiguous"):
                retrieve_related_notes(
                    self.config(vault),
                    source_id,
                    runner=AmbiguousPathRunner(),
                    executable="qmd",
                )

    def test_excludes_source_note_after_resolving_normalized_path(self) -> None:
        class SourceNoteRunner:
            def __call__(
                self, command: list[str], **_: object
            ) -> subprocess.CompletedProcess[str]:
                result = [
                    {
                        "file": (
                            "qmd://fixture-vault/knowledgebase/sources/"
                            "Article-title-a1b2c3d4.md?index=test"
                        ),
                        "title": "Article title",
                        "score": 0.8,
                    }
                ]
                return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            source_id = write_capture(
                vault,
                source_note="knowledgebase/sources/Article title--a1b2c3d4.md",
            )

            result = retrieve_related_notes(
                self.config(vault),
                source_id,
                runner=SourceNoteRunner(),
                executable="qmd",
            )

            self.assertEqual(result["candidates"], [])

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

    def test_rejects_tampered_capture_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            source_id = write_capture(vault)
            manifest_path = (
                vault / ".vault-pr" / "sources" / "public" / source_id / "manifest.json"
            )
            manifest = json.loads(manifest_path.read_text())
            manifest["final_url"] = "https://example.com/tampered"
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
