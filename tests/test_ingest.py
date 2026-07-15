from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vault_pr.capture import load_validated_capture
from vault_pr.config import ConfigError, vault_relative_path
from vault_pr.ingest import (
    ExtractedSource,
    FetchedSource,
    IngestError,
    canonicalize_url,
    extract_source,
    fetch_url,
    ingest_url,
    normalize_title,
    require_public_url,
    safe_note_filename,
    source_note_path,
    snapshot_source,
)
from vault_pr.recovery import commit_applied_transaction
from vault_pr.process import LimitedProcessTimeout


def make_config(vault: Path) -> dict[str, object]:
    return {
        "vault": {"path": str(vault)},
        "sources": {
            "public_archive_dir": ".vault-pr/sources/public",
            "visible_notes_dir": "knowledgebase/sources",
            "timeout_seconds": 5,
            "max_download_bytes": 1_000_000,
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
        sensitive_urls = [
            "https://example.com/article?token=secret",
            "https://example.com/article?X-Amz-Credential=value",
            "https://example.com/article?X-Amz-Signature=value",
            "https://example.com/article?X-Goog-Credential=value",
            "https://example.com/article?jwt=value",
        ]
        for url in sensitive_urls:
            with self.subTest(url=url):
                with self.assertRaisesRegex(IngestError, "sensitive parameters"):
                    canonicalize_url(url)

    def test_fetch_pins_the_single_validated_dns_answer(self) -> None:
        public_result = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))
        ]
        opened_targets = []

        class Response:
            status = 200
            headers = {"Content-Type": "text/plain"}

            def getheader(self, name: str) -> str | None:
                return self.headers.get(name)

            def read(self, _limit: int) -> bytes:
                return b"pinned body"

        class Connection:
            def request(
                self, _method: str, _path: str, *, headers: dict[str, str]
            ) -> None:
                self.headers = headers

            def getresponse(self) -> Response:
                return Response()

            def close(self) -> None:
                pass

        def open_connection(target: object, _timeout: float) -> Connection:
            opened_targets.append(target)
            return Connection()

        with (
            patch(
                "vault_pr.ingest.socket.getaddrinfo", return_value=public_result
            ) as resolver,
            patch(
                "vault_pr.ingest._open_pinned_connection",
                side_effect=open_connection,
            ),
        ):
            fetched = fetch_url("http://example.test/article", 5, 1000)

        self.assertEqual(fetched.body, b"pinned body")
        self.assertEqual(resolver.call_count, 1)
        self.assertEqual(opened_targets[0].address, public_result[0])

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
        fetched = FetchedSource(
            requested_url="https://example.com/report.pdf",
            final_url="https://example.com/report.pdf",
            status=200,
            headers={"content-type": "application/pdf"},
            body=b"%PDF synthetic fixture",
            captured_at="2026-07-15T00:00:00+00:00",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)

            def run_worker(
                command: list[str], **_: object
            ) -> subprocess.CompletedProcess[str]:
                (output_dir / "extracted.md").write_text(
                    "# PDF Title\n\n## Page 1\n\n" + "PDF text " * 30
                )
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({"title": "PDF Title", "extractor_version": "test"}),
                    "",
                )

            with patch("vault_pr.ingest.subprocess.run", side_effect=run_worker):
                extracted = extract_source(fetched, Path(temp_dir))

            self.assertEqual(extracted.extractor, "pypdf")
            self.assertEqual(extracted.raw_filename, "source.pdf")
            self.assertIn("## Page 1", (Path(temp_dir) / "extracted.md").read_text())

    def test_defuddle_timeout_is_bounded(self) -> None:
        fetched = FetchedSource(
            requested_url="https://example.com/article",
            final_url="https://example.com/article",
            status=200,
            headers={"content-type": "text/html"},
            body=b"<main>" + b"content " * 30 + b"</main>",
            captured_at="2026-07-15T00:00:00+00:00",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("vault_pr.ingest.shutil.which", return_value="defuddle"),
                patch(
                    "vault_pr.ingest.run_limited_process",
                    side_effect=LimitedProcessTimeout("timeout"),
                ),
            ):
                with self.assertRaisesRegex(IngestError, "exceeded 1 seconds"):
                    extract_source(
                        fetched,
                        Path(temp_dir),
                        extraction_timeout_seconds=1,
                    )

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

    def test_source_note_path_versions_changed_content_for_same_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            note_dir = Path(temp_dir)
            url = "https://example.com/article"
            first_id = "src_11111111111111111111"
            second_id = "src_22222222222222222222"
            original = note_dir / safe_note_filename("Original", url)
            original.write_text(f'---\nsource_id: "{first_id}"\n---\n')

            first = source_note_path(note_dir, "Changed", url, source_id=first_id)
            second = source_note_path(note_dir, "Changed", url, source_id=second_id)
            second.write_text(f'---\nsource_id: "{second_id}"\n---\n')

            self.assertEqual(first, original)
            self.assertNotEqual(second, original)
            self.assertEqual(
                source_note_path(note_dir, "Again", url, source_id=second_id), second
            )

    def test_raw_snapshot_changes_produce_new_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            first_fetch = markdown_fetch()
            second_fetch = FetchedSource(
                requested_url=first_fetch.requested_url,
                final_url=first_fetch.final_url,
                status=first_fetch.status,
                headers=first_fetch.headers,
                body=b"different raw response",
                captured_at=first_fetch.captured_at,
            )

            def constant_extraction(
                fetched: FetchedSource, temp: Path, **_: object
            ) -> ExtractedSource:
                (temp / "raw.bin").write_bytes(fetched.body)
                (temp / "extracted.md").write_text("# Same extracted content\n")
                return ExtractedSource(
                    title="Same title",
                    raw_filename="raw.bin",
                    extracted_filename="extracted.md",
                    extractor="fixture",
                    extractor_version="1",
                )

            with (
                patch(
                    "vault_pr.ingest.fetch_url",
                    side_effect=[first_fetch, second_fetch],
                ),
                patch(
                    "vault_pr.ingest.extract_source", side_effect=constant_extraction
                ),
            ):
                first = ingest_url(
                    config=make_config(vault),
                    url=first_fetch.requested_url,
                    apply=False,
                )
                second = ingest_url(
                    config=make_config(vault),
                    url=first_fetch.requested_url,
                    apply=False,
                )

            self.assertNotEqual(first["source_id"], second["source_id"])

    def test_same_url_changed_content_creates_versioned_source_note(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(vault), "config", "user.name", "Test"],
                check=True,
            )
            first_fetch = markdown_fetch()
            second_fetch = FetchedSource(
                requested_url=first_fetch.requested_url,
                final_url=first_fetch.final_url,
                status=first_fetch.status,
                headers=first_fetch.headers,
                body=b"# Example Source\n\nA changed source body.\n",
                captured_at="2026-07-15T00:01:00+00:00",
            )
            with patch(
                "vault_pr.ingest.fetch_url",
                side_effect=[first_fetch, second_fetch],
            ):
                first = ingest_url(
                    config=make_config(vault),
                    url=first_fetch.requested_url,
                    apply=True,
                )
                commit_applied_transaction(
                    vault,
                    vault / ".git",
                    first["transaction_id"],
                    "Capture first source version",
                )
                second = ingest_url(
                    config=make_config(vault),
                    url=second_fetch.requested_url,
                    apply=True,
                )

            self.assertNotEqual(first["source_id"], second["source_id"])
            self.assertNotEqual(first["source_note"], second["source_note"])
            self.assertTrue((vault / first["source_note"]).is_file())
            self.assertTrue((vault / second["source_note"]).is_file())

    def test_apply_writes_capture_manifest_and_source_note(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)

            with patch("vault_pr.ingest.fetch_url", return_value=markdown_fetch()):
                result = ingest_url(
                    config=make_config(vault),
                    url="https://example.com/article.md",
                    apply=True,
                )

            self.assertEqual(result["status"], "captured")
            self.assertTrue(result["requires_commit"])
            capture_dir = vault / result["capture_dir"]
            note_path = vault / result["source_note"]
            manifest = json.loads((capture_dir / "manifest.json").read_text())
            note = note_path.read_text()

            self.assertEqual(manifest["source_id"], result["source_id"])
            self.assertEqual(manifest["schema_version"], 2)
            self.assertNotIn("domain", manifest)
            self.assertEqual(manifest["extractor"], "direct-markdown")
            self.assertIn("type: source", note)
            self.assertNotIn("domain:", note)
            self.assertIn(result["source_id"], note)
            journal = (
                vault
                / ".git"
                / "vault-pr-active"
                / result["transaction_id"]
                / "journal.json"
            )
            self.assertEqual(json.loads(journal.read_text())["phase"], "applied")

    def test_reingest_rejects_tampered_committed_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(vault), "config", "user.name", "Test"],
                check=True,
            )
            with patch("vault_pr.ingest.fetch_url", return_value=markdown_fetch()):
                first = ingest_url(
                    config=make_config(vault),
                    url="https://example.com/article.md",
                    apply=True,
                )
            commit_applied_transaction(
                vault,
                vault / ".git",
                first["transaction_id"],
                "Capture fixture source",
            )
            capture = vault / first["capture_dir"]
            manifest = json.loads((capture / "manifest.json").read_text())
            raw = capture / manifest["artifacts"]["raw"]
            raw.write_bytes(b"tampered raw capture")
            subprocess.run(["git", "-C", str(vault), "add", str(raw)], check=True)
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "tamper fixture"],
                check=True,
            )

            with patch("vault_pr.ingest.fetch_url", return_value=markdown_fetch()):
                with self.assertRaisesRegex(IngestError, "integrity failed"):
                    ingest_url(
                        config=make_config(vault),
                        url="https://example.com/article.md",
                        apply=True,
                    )

    def test_rejects_sensitive_url_returned_by_fetch_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            fetched = markdown_fetch()
            fetched = FetchedSource(
                requested_url=fetched.requested_url,
                final_url="https://example.com/article?X-Amz-Signature=secret",
                status=fetched.status,
                headers=fetched.headers,
                body=fetched.body,
                captured_at=fetched.captured_at,
            )

            with patch("vault_pr.ingest.fetch_url", return_value=fetched):
                with self.assertRaisesRegex(IngestError, "sensitive parameters"):
                    ingest_url(
                        config=make_config(vault),
                        url="https://example.com/article.md",
                        apply=False,
                    )

            self.assertFalse((vault / ".vault-pr").exists())

    def test_dry_run_does_not_write_to_vault(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)

            with patch("vault_pr.ingest.fetch_url", return_value=markdown_fetch()):
                result = ingest_url(
                    config=make_config(vault),
                    url="https://example.com/article.md",
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
                    apply=False,
                    adapter="authenticated",
                )

    def test_http_adapter_rejects_local_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            snapshot = root / "article.html"
            snapshot.write_text("<main>local snapshot</main>")

            with self.assertRaisesRegex(IngestError, "does not accept --snapshot"):
                ingest_url(
                    config=make_config(vault),
                    url="https://example.com/article",
                    apply=False,
                    snapshot_path=snapshot,
                    adapter="http",
                )

    def test_wechat_adapter_preserves_converted_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            article = root / "article"
            images = article / "images"
            images.mkdir(parents=True)
            (images / "cover.png").write_bytes(b"png fixture")
            snapshot = article / "article.md"
            snapshot.write_text(
                "# WeChat article\n\n![cover](images/cover.png)\n\n" + "content " * 30
            )

            result = ingest_url(
                config=make_config(vault),
                url="https://mp.weixin.qq.com/s/example",
                apply=True,
                snapshot_path=snapshot,
                adapter="wechat",
            )

            capture = vault / result["capture_dir"]
            manifest = json.loads((capture / "manifest.json").read_text())
            self.assertEqual(manifest["adapter"], "wechat")
            self.assertEqual(manifest["extractor"], "wechat-markdown-snapshot")
            self.assertIn("images/cover.png", manifest["asset_sha256"])
            self.assertTrue((capture / "images" / "cover.png").is_file())

    def test_wechat_adapter_does_not_copy_empty_image_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            article = root / "article"
            (article / "images" / "empty").mkdir(parents=True)
            snapshot = article / "article.md"
            snapshot.write_text("# WeChat article\n\n" + "content " * 30)

            result = ingest_url(
                config=make_config(vault),
                url="https://mp.weixin.qq.com/s/example",
                apply=True,
                snapshot_path=snapshot,
                adapter="wechat",
            )

            capture = vault / result["capture_dir"]
            self.assertFalse((capture / "images").exists())
            load_validated_capture(
                capture,
                source_id=result["source_id"],
                vault=vault,
            )

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
                apply=False,
                snapshot_path=snapshot,
                adapter="wechat",
            )
            cover.write_bytes(b"version two")
            second = ingest_url(
                config=make_config(vault),
                url="https://mp.weixin.qq.com/s/example",
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
                    apply=False,
                    adapter="wechat",
                )

    def test_wechat_url_requires_local_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)

            with self.assertRaisesRegex(IngestError, "local HTML, Markdown, or PDF"):
                ingest_url(
                    config=make_config(vault),
                    url="https://mp.weixin.qq.com/s/example",
                    apply=False,
                )

    def test_wechat_url_rejects_explicit_adapter_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            snapshot = root / "article.html"
            snapshot.write_text("<main>captured article</main>")

            for adapter, snapshot_path in (
                ("http", None),
                ("http", snapshot),
                ("authenticated", snapshot),
            ):
                with self.subTest(adapter=adapter, snapshot=bool(snapshot_path)):
                    with self.assertRaisesRegex(
                        IngestError, "must use the offline WeChat snapshot adapter"
                    ):
                        ingest_url(
                            config=make_config(vault),
                            url="https://mp.weixin.qq.com/s/example",
                            apply=False,
                            snapshot_path=snapshot_path,
                            adapter=adapter,
                        )

    def test_wechat_adapter_accepts_local_html_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = Path(temp_dir) / "article.html"
            snapshot.write_text("<main>captured article</main>")

            fetched = snapshot_source(
                url="https://mp.weixin.qq.com/s/example",
                snapshot_path=snapshot,
                adapter="wechat",
                max_bytes=1000,
            )

            self.assertEqual(fetched.adapter, "wechat")
            self.assertEqual(fetched.headers["content-type"], "text/html")

    def test_failed_note_write_removes_new_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)

            original_replace = os.replace

            def fail_note_replace(source: Path, destination: Path) -> None:
                if Path(destination).suffix == ".md":
                    raise OSError("disk full")
                original_replace(source, destination)

            with (
                patch("vault_pr.ingest.fetch_url", return_value=markdown_fetch()),
                patch("vault_pr.ingest.os.replace", side_effect=fail_note_replace),
            ):
                with self.assertRaisesRegex(IngestError, "failed to write"):
                    ingest_url(
                        config=make_config(vault),
                        url="https://example.com/article.md",
                        apply=True,
                    )

            archive = vault / ".vault-pr" / "sources" / "public"
            self.assertFalse(archive.exists() and any(archive.iterdir()))
            self.assertFalse(any(vault.rglob("*.vault-pr-tmp")))
            status = subprocess.run(
                ["git", "-C", str(vault), "status", "--porcelain=v1"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout, "")

    def test_failed_capture_stage_leaves_vault_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)

            with (
                patch("vault_pr.ingest.fetch_url", return_value=markdown_fetch()),
                patch(
                    "vault_pr.ingest.shutil.copytree",
                    side_effect=OSError("injected copy failure"),
                ),
            ):
                with self.assertRaisesRegex(IngestError, "failed to write"):
                    ingest_url(
                        config=make_config(vault),
                        url="https://example.com/article.md",
                        apply=True,
                    )

            status = subprocess.run(
                ["git", "-C", str(vault), "status", "--porcelain=v1"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout, "")
            active_root = vault / ".git" / "vault-pr-active"
            self.assertFalse(
                active_root.is_dir()
                and any(path.is_dir() for path in active_root.iterdir())
            )


if __name__ == "__main__":
    unittest.main()
