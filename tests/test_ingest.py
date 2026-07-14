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
    snapshot_source,
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
            "tech": {
                "root_dir": "knowledgebase",
                "source_notes_dir": "knowledgebase/sources",
                "qmd_collection": "notebook-tech",
            },
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

    def test_pdf_extracts_page_text(self) -> None:
        class Page:
            def extract_text(self) -> str:
                return "PDF text " * 30

        class Metadata:
            title = "PDF Title"

        class Reader:
            metadata = Metadata()
            pages = [Page()]

        fetched = FetchedSource(
            requested_url="https://example.com/report.pdf",
            final_url="https://example.com/report.pdf",
            status=200,
            headers={"content-type": "application/pdf"},
            body=b"%PDF synthetic fixture",
            captured_at="2026-07-15T00:00:00+00:00",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("pypdf.PdfReader", return_value=Reader()):
                extracted = extract_source(fetched, Path(temp_dir))

            self.assertEqual(extracted.extractor, "pypdf")
            self.assertEqual(extracted.raw_filename, "source.pdf")
            self.assertIn("## Page 1", (Path(temp_dir) / "extracted.md").read_text())

    def test_authenticated_snapshot_never_fetches_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = Path(temp_dir) / "captured.md"
            snapshot.write_text("# Private article\n\n" + "body " * 30)

            fetched = snapshot_source(
                url="https://internal.example/article",
                snapshot_path=snapshot,
                adapter="authenticated",
                max_bytes=10000,
            )

            self.assertEqual(fetched.status, 0)
            self.assertEqual(fetched.adapter, "authenticated")
            self.assertNotIn("cookie", fetched.headers)

    def test_snapshot_rejects_symbolic_link(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target.md"
            target.write_text("# Target\n")
            snapshot = root / "snapshot.md"
            snapshot.symlink_to(target)

            with self.assertRaisesRegex(IngestError, "symbolic link"):
                snapshot_source(
                    url="https://example.com/article",
                    snapshot_path=snapshot,
                    adapter="authenticated",
                    max_bytes=10000,
                )


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

    def test_snapshot_ingest_records_authenticated_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            snapshot = root / "private.md"
            snapshot.write_text("# Private source\n\n" + "trusted body " * 20)

            result = ingest_url(
                config=make_config(vault),
                url="https://internal.example/private",
                domain="tech",
                apply=True,
                snapshot_path=snapshot,
                adapter="authenticated",
            )

            manifest = json.loads(
                (vault / result["capture_dir"] / "manifest.json").read_text()
            )
            self.assertEqual(manifest["adapter"], "authenticated")
            self.assertEqual(manifest["http_status"], 0)
            self.assertEqual(manifest["extractor"], "authenticated-snapshot")

    def test_authenticated_adapter_requires_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)

            with self.assertRaisesRegex(IngestError, "requires --snapshot"):
                ingest_url(
                    config=make_config(vault),
                    url="https://internal.example/private",
                    domain="tech",
                    apply=False,
                    adapter="authenticated",
                )

    def test_wechat_adapter_preserves_converted_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)

            def converted_snapshot(
                _url: str, output_dir: Path, timeout_seconds: float
            ) -> Path:
                self.assertGreater(timeout_seconds, 0)
                article = output_dir / "wechat-adapter" / "output" / "article"
                images = article / "images"
                images.mkdir(parents=True)
                (images / "cover.png").write_bytes(b"png fixture")
                markdown = article / "article.md"
                markdown.write_text(
                    "# WeChat article\n\n![cover](images/cover.png)\n\n"
                    + "content " * 30
                )
                return markdown

            with (
                patch(
                    "vault_pr.ingest._wechat_snapshot", side_effect=converted_snapshot
                ),
                patch(
                    "vault_pr.ingest.require_public_url",
                    side_effect=lambda url: url,
                ),
            ):
                result = ingest_url(
                    config=make_config(vault),
                    url="https://mp.weixin.qq.com/s/example",
                    domain="tech",
                    apply=True,
                )

            capture = vault / result["capture_dir"]
            manifest = json.loads((capture / "manifest.json").read_text())
            self.assertEqual(manifest["adapter"], "wechat")
            self.assertIn("images/cover.png", manifest["asset_sha256"])
            self.assertTrue((capture / "images" / "cover.png").is_file())

    def test_wechat_asset_changes_produce_new_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            article = root / "article"
            images = article / "images"
            images.mkdir(parents=True)
            snapshot = article / "article.md"
            snapshot.write_text("# Article\n\n![cover](images/cover.png)\n")
            cover = images / "cover.png"
            cover.write_bytes(b"version one")

            first = ingest_url(
                config=make_config(vault),
                url="https://mp.weixin.qq.com/s/example",
                domain="tech",
                apply=False,
                snapshot_path=snapshot,
                adapter="wechat",
            )
            cover.write_bytes(b"version two")
            second = ingest_url(
                config=make_config(vault),
                url="https://mp.weixin.qq.com/s/example",
                domain="tech",
                apply=False,
                snapshot_path=snapshot,
                adapter="wechat",
            )

            self.assertNotEqual(first["source_id"], second["source_id"])

    def test_wechat_assets_obey_file_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            article = root / "article"
            images = article / "images"
            images.mkdir(parents=True)
            (images / "one.png").write_bytes(b"one")
            (images / "two.png").write_bytes(b"two")
            snapshot = article / "article.md"
            snapshot.write_text("# Article\n")
            config = make_config(vault)
            config["sources"]["max_asset_files"] = 1

            with self.assertRaisesRegex(IngestError, "1-file limit"):
                ingest_url(
                    config=config,
                    url="https://mp.weixin.qq.com/s/example",
                    domain="tech",
                    apply=False,
                    snapshot_path=snapshot,
                    adapter="wechat",
                )

    def test_wechat_network_adapter_rejects_non_wechat_host(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)

            with self.assertRaisesRegex(IngestError, "only accepts mp.weixin.qq.com"):
                ingest_url(
                    config=make_config(vault),
                    url="http://127.0.0.1/private",
                    domain="tech",
                    apply=False,
                    adapter="wechat",
                )

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
