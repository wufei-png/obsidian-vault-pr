from __future__ import annotations

import json
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vault_pr.config import ConfigError, vault_relative_path
from vault_pr.ingest import (
    FetchedSource,
    IngestError,
    canonicalize_url,
    extract_source,
    ingest_url,
    normalize_title,
    require_public_url,
    safe_note_filename,
    source_note_path,
)


def make_config(vault: Path) -> dict[str, object]:
    return {
        "vault": {"path": str(vault)},
        "sources": {
            "public_archive_dir": ".vault-pr/sources/public",
            "timeout_seconds": 5,
            "max_download_bytes": 1_000_000,
        },
        "domains": {
            "tech": {"source_notes_dir": "knowledgebase/sources"},
        },
    }


def markdown_fetch() -> FetchedSource:
    return FetchedSource(
        requested_url="https://example.com/article.md",
        final_url="https://example.com/article.md",
        status=200,
        headers={"content-type": "text/markdown; charset=utf-8"},
        body=b"# Example Source\n\nA stable source body.\n",
        captured_at="2026-07-15T00:00:00+00:00",
    )


class UrlSafetyTests(unittest.TestCase):
    def test_canonicalize_removes_fragment_and_default_port(self) -> None:
        self.assertEqual(
            canonicalize_url("HTTPS://Example.COM:443/a?q=1#section"),
            "https://example.com/a?q=1",
        )

    def test_rejects_private_address(self) -> None:
        private_result = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))
        ]
        with patch("vault_pr.ingest.socket.getaddrinfo", return_value=private_result):
            with self.assertRaisesRegex(IngestError, "non-public"):
                require_public_url("http://example.test/")

    def test_rejects_sensitive_query_parameters(self) -> None:
        with self.assertRaisesRegex(IngestError, "sensitive parameters"):
            canonicalize_url("https://example.com/article?token=secret")

    def test_rejects_invalid_port(self) -> None:
        with self.assertRaisesRegex(IngestError, "invalid port"):
            canonicalize_url("https://example.com:invalid/article")

    def test_rejects_vault_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir).resolve()
            with self.assertRaisesRegex(ConfigError, "escapes"):
                vault_relative_path(vault, "../outside", "test.path")


class ExtractionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("defuddle"), "defuddle is not installed")
    def test_defuddle_extracts_local_html(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            body = (
                "<html><head><title>Test Article</title></head><body><main>"
                "<h1>Test Article</h1><p>" + "useful content " * 30 + "</p>"
                "</main></body></html>"
            ).encode()
            fetched = FetchedSource(
                requested_url="https://example.com/article",
                final_url="https://example.com/article",
                status=200,
                headers={"content-type": "text/html"},
                body=body,
                captured_at="2026-07-15T00:00:00+00:00",
            )

            extracted = extract_source(fetched, output_dir)

            self.assertEqual(extracted.extractor, "defuddle")
            self.assertEqual(extracted.title, "Test Article")
            self.assertIn("useful content", (output_dir / "extracted.md").read_text())


class IngestTests(unittest.TestCase):
    def test_normalize_title_removes_newlines_and_controls(self) -> None:
        self.assertEqual(normalize_title("  A\nTitle\x00  "), "A Title")

    def test_safe_note_filename_is_stable_and_readable(self) -> None:
        filename = safe_note_filename("A/B: Test?", "https://example.com/article")
        self.assertRegex(filename, r"^A-B- Test--[0-9a-f]{8}\.md$")

    def test_source_note_path_reuses_url_suffix_when_title_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            note_dir = Path(temp_dir)
            original = note_dir / safe_note_filename(
                "Original title", "https://example.com/article"
            )
            original.write_text("existing\n")

            resolved = source_note_path(
                note_dir, "Changed title", "https://example.com/article"
            )

            self.assertEqual(resolved, original)

    def test_apply_writes_capture_manifest_and_source_note(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)

            with patch("vault_pr.ingest.fetch_url", return_value=markdown_fetch()):
                result = ingest_url(
                    config=make_config(vault),
                    url="https://example.com/article.md",
                    domain="tech",
                    apply=True,
                )

            self.assertEqual(result["status"], "captured")
            capture_dir = vault / result["capture_dir"]
            note_path = vault / result["source_note"]
            manifest = json.loads((capture_dir / "manifest.json").read_text())
            note = note_path.read_text()

            self.assertEqual(manifest["source_id"], result["source_id"])
            self.assertEqual(manifest["extractor"], "direct-markdown")
            self.assertIn("type: source", note)
            self.assertIn('domain: "tech"', note)
            self.assertIn(result["source_id"], note)

    def test_dry_run_does_not_write_to_vault(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)

            with patch("vault_pr.ingest.fetch_url", return_value=markdown_fetch()):
                result = ingest_url(
                    config=make_config(vault),
                    url="https://example.com/article.md",
                    domain="tech",
                    apply=False,
                )

            self.assertEqual(result["status"], "planned")
            self.assertFalse((vault / ".vault-pr").exists())
            self.assertFalse((vault / "knowledgebase").exists())

    def test_failed_note_write_removes_new_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)

            with (
                patch("vault_pr.ingest.fetch_url", return_value=markdown_fetch()),
                patch("pathlib.Path.replace", side_effect=OSError("disk full")),
            ):
                with self.assertRaisesRegex(IngestError, "failed to write"):
                    ingest_url(
                        config=make_config(vault),
                        url="https://example.com/article.md",
                        domain="tech",
                        apply=True,
                    )

            archive = vault / ".vault-pr" / "sources" / "public"
            self.assertFalse(archive.exists() and any(archive.iterdir()))


if __name__ == "__main__":
    unittest.main()
