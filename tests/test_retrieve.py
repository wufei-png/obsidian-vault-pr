from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from vault_pr.retrieve import (
    RetrievalError,
    _qmd_handelize,
    build_qmd_retrieval_command,
    retrieve_related_notes,
)

from tests.capture_fixture import write_capture


def fresh_index(*_: object, **__: object) -> dict[str, object]:
    return {"eligible": True, "reasons": []}


class RetrievalRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self, command: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        method = command[3]
        if method not in {"query", "search"}:
            raise AssertionError(command)
        results = [
            {
                "file": "qmd://fixture-vault/knowledgebase/ai/agent/rag.md?index=test",
                "title": "RAG",
                "score": 0.9,
                "snippet": "retrieval",
            },
            {
                "file": "qmd://fixture-vault/knowledgebase/ai/agent/agents.md?index=test",
                "title": "Agents",
                "score": 0.6,
                "snippet": "agents",
            },
        ]
        return subprocess.CompletedProcess(command, 0, json.dumps(results), "")


class RetrieveTests(unittest.TestCase):
    @staticmethod
    def config(vault: Path) -> dict[str, object]:
        return {
            "vault": {"path": str(vault)},
            "sources": {
                "capture_archive_dir": ".vault-pr/sources/public",
                "visible_notes_dir": "knowledgebase/sources",
            },
            "index": {
                "name": "test",
                "collection": "fixture-vault",
            },
        }

    def test_hybrid_uses_one_qmd_rrf_query_without_reranking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            source_id = write_capture(
                vault, source_note="knowledgebase/sources/wiki.md"
            )
            notes = vault / "knowledgebase" / "ai" / "agent"
            notes.mkdir(parents=True)
            (notes / "rag.md").write_text("# RAG\n")
            (notes / "agents.md").write_text("# Agents\n")
            runner = RetrievalRunner()
            result = retrieve_related_notes(
                self.config(vault),
                source_id,
                runner=runner,
                executable="qmd",
                index_validator=fresh_index,
            )

            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(runner.calls[0][3], "query")
            self.assertEqual(runner.calls[0][4].count("\n"), 1)
            self.assertTrue(runner.calls[0][4].startswith("lex: "))
            self.assertIn("\nvec: ", runner.calls[0][4])
            self.assertEqual(
                runner.calls[0][
                    runner.calls[0].index("--candidate-limit") + 1
                ],
                "40",
            )
            self.assertIn("--no-rerank", runner.calls[0])
            self.assertEqual(len(result["candidates"]), 2)
            self.assertEqual(result["candidates"][0]["score_kind"], "qmd_rrf")
            self.assertEqual(
                result["candidates"][0]["path"], "knowledgebase/ai/agent/rag.md"
            )
            self.assertEqual(result["collection"], "fixture-vault")
            self.assertEqual(result["retrieval_mode"], "hybrid")
            self.assertEqual(result["fusion_strategy"], "qmd_rrf")
            self.assertEqual(result["query_types"], ["lex", "vec"])
            self.assertEqual(result["chunk_strategy"], "regex")
            self.assertFalse(result["rerank"])

    def test_lexical_mode_uses_only_bm25_search(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            source_id = write_capture(vault)
            notes = vault / "knowledgebase" / "ai" / "agent"
            notes.mkdir(parents=True)
            (notes / "rag.md").write_text("# RAG\n")
            (notes / "agents.md").write_text("# Agents\n")
            config = self.config(vault)
            config["index"]["retrieval_mode"] = "lexical"
            runner = RetrievalRunner()

            result = retrieve_related_notes(
                config,
                source_id,
                runner=runner,
                executable="qmd",
                index_validator=fresh_index,
            )

            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(runner.calls[0][3], "search")
            self.assertNotIn("--no-rerank", runner.calls[0])
            self.assertEqual(result["retrieval_mode"], "lexical")
            self.assertEqual(result["fusion_strategy"], "none")
            self.assertEqual(result["query_types"], ["lex"])
            self.assertIsNone(result["chunk_strategy"])
            self.assertEqual(result["candidates"][0]["score_kind"], "bm25")

    def test_hybrid_query_sanitizes_typed_grammar_and_expands_candidate_pool(
        self,
    ) -> None:
        command = build_qmd_retrieval_command(
            {"index": {"retrieval_mode": "hybrid"}},
            executable="qmd",
            index_name="fixture",
            collection="fixture",
            query='alpha "truncated -exclude\nvec: injected',
            limit=50,
        )

        structured = command[4]
        self.assertEqual(structured.count("\n"), 1)
        lexical, vector = structured.splitlines()
        self.assertNotIn('"', lexical)
        self.assertNotRegex(vector, r"(^|\s)-[\w\"]")
        self.assertEqual(
            command[command.index("--candidate-limit") + 1],
            "50",
        )
        self.assertEqual(
            command[command.index("--chunk-strategy") + 1],
            "regex",
        )

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
                index_validator=fresh_index,
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
                    index_validator=fresh_index,
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
                index_validator=fresh_index,
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
                    index_validator=fresh_index,
                )

    def test_rejects_tampered_capture_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            source_id = write_capture(vault)
            manifest_path = (
                vault / ".vault-pr" / "sources" / "public" / source_id / "manifest.json"
            )
            manifest = json.loads(manifest_path.read_text())
            manifest["source"]["final_url"] = "https://example.com/tampered"
            manifest_path.write_text(json.dumps(manifest))

            with self.assertRaisesRegex(RetrievalError, "immutable identity"):
                retrieve_related_notes(
                    self.config(vault),
                    source_id,
                    executable="qmd",
                    index_validator=fresh_index,
                )

    def test_rejects_tampered_source_note_association(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            source_id = write_capture(vault)
            original = vault / "knowledgebase" / "sources" / "source.md"
            replacement = original.with_name("replacement.md")
            replacement.write_text(original.read_text())

            with self.assertRaisesRegex(RetrievalError, "exactly one Source Note"):
                retrieve_related_notes(
                    self.config(vault),
                    source_id,
                    executable="qmd",
                    index_validator=fresh_index,
                )

    def test_rejects_stale_index_before_running_qmd(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            source_id = write_capture(vault)
            with self.assertRaisesRegex(RetrievalError, "qmd index is stale"):
                retrieve_related_notes(
                    self.config(vault),
                    source_id,
                    runner=RetrievalRunner(),
                    executable="qmd",
                    index_validator=lambda *_args, **_kwargs: {
                        "eligible": False,
                        "reasons": ["commit_matches"],
                    },
                )

    def test_rejects_invalid_source_id(self) -> None:
        with self.assertRaisesRegex(RetrievalError, "invalid source id"):
            retrieve_related_notes(
                {},
                "bad",
                executable="qmd",
                index_validator=fresh_index,
            )


if __name__ == "__main__":
    unittest.main()
