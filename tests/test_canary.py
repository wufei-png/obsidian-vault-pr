from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vault_pr.canary import CanaryError, _probe_source_note, run_capture_route_canary


class CaptureRouteCanaryTests(unittest.TestCase):
    def test_snapshot_route_commits_and_disposes_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = Path(temp_dir) / "article.md"
            snapshot.write_text(
                "# Canary article\n\nA stable synthetic snapshot.\n",
                encoding="utf-8",
            )
            cleaned: list[tuple[str, str]] = []

            result = run_capture_route_canary(
                url="https://example.com/canary",
                adapter="auto",
                snapshot_path=snapshot,
                indexer=lambda _config: {"status": "indexed"},
                retriever=lambda _config, _source_id: {
                    "candidates": [{"path": "fixture-concept.md"}]
                },
                cleaner=lambda index, collection: cleaned.append((index, collection)),
                probe=lambda _config, _source_id, _source_note: 1,
            )

            self.assertEqual(result["status"], "capture_route_passed")
            self.assertEqual(result["adapter"], "authenticated")
            self.assertEqual(result["indexed_source_hits"], 1)
            self.assertEqual(result["related_candidate_count"], 1)
            self.assertFalse(result["fixture_retained"])
            self.assertTrue(result["semantic_outcome_required"])
            self.assertEqual(len(cleaned), 1)

    def test_failed_retrieval_still_cleans_qmd_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = Path(temp_dir) / "article.md"
            snapshot.write_text("# Canary\n", encoding="utf-8")
            cleaned: list[tuple[str, str]] = []

            with self.assertRaisesRegex(CanaryError, "retrieval fault"):
                run_capture_route_canary(
                    url="https://example.com/canary",
                    adapter="authenticated",
                    snapshot_path=snapshot,
                    indexer=lambda _config: {"status": "indexed"},
                    retriever=lambda _config, _source_id: (_ for _ in ()).throw(
                        RuntimeError("retrieval fault")
                    ),
                    cleaner=lambda index, collection: cleaned.append(
                        (index, collection)
                    ),
                    probe=lambda _config, _source_id, _source_note: 1,
                )

            self.assertEqual(len(cleaned), 1)

    def test_empty_source_note_probe_fails_and_cleans_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = Path(temp_dir) / "article.md"
            snapshot.write_text("# Canary\n", encoding="utf-8")
            cleaned: list[tuple[str, str]] = []

            with self.assertRaisesRegex(CanaryError, "committed source note"):
                run_capture_route_canary(
                    url="https://example.com/canary",
                    adapter="authenticated",
                    snapshot_path=snapshot,
                    indexer=lambda _config: {"status": "indexed"},
                    retriever=lambda _config, _source_id: {"candidates": []},
                    cleaner=lambda index, collection: cleaned.append(
                        (index, collection)
                    ),
                    probe=lambda _config, _source_id, _source_note: 0,
                )

            self.assertEqual(len(cleaned), 1)

    def test_unrelated_source_probe_hit_does_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            (vault / "target.md").write_text("# Target\n", encoding="utf-8")
            (vault / "unrelated.md").write_text("# Unrelated\n", encoding="utf-8")
            result = subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(
                    [{"file": "qmd://fixture/unrelated.md", "score": 0.9}]
                ),
                stderr="",
            )
            config = {
                "vault": {"path": str(vault)},
                "index": {"name": "fixture", "collection": "fixture"},
            }

            with (
                patch("vault_pr.canary.shutil.which", return_value="qmd"),
                patch("vault_pr.canary.subprocess.run", return_value=result),
            ):
                hits = _probe_source_note(config, "src_fixture", "target.md")

            self.assertEqual(hits, 0)


if __name__ == "__main__":
    unittest.main()
