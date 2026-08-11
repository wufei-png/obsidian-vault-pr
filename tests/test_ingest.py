from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from vault_pr.capture import (
    CaptureError,
    load_validated_capture,
    source_identity_digest,
)
from vault_pr.config import ConfigError, vault_relative_path
from vault_pr.acquisition import (
    ExtractedSource,
    FetchedSource,
    IngestError,
    canonicalize_url,
    extract_source,
    fetch_url,
    require_public_url,
    snapshot_source,
)
from vault_pr.ingest import ingest_source
from vault_pr.mutation import execute_journaled_mutation as execute_mutation
from vault_pr.source_files import copy_snapshot_assets as _copy_snapshot_assets
from vault_pr.source_note import (
    build_source_note,
    normalize_title,
    safe_note_filename,
    source_note_has_id,
    source_note_path,
)
from vault_pr.recovery import commit_applied_transaction
from vault_pr.process import LimitedProcessResult, LimitedProcessTimeout


def ingest_url(*, url: str, **kwargs: object) -> dict[str, object]:
    return ingest_source(source_reference=url, **kwargs)


def make_config(vault: Path) -> dict[str, object]:
    return {
        "vault": {"path": str(vault)},
        "sources": {
            "capture_archive_dir": ".vault-pr/sources/public",
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
        with patch(
            "vault_pr.acquisition.socket.getaddrinfo",
            return_value=private_result,
        ):
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
                "vault_pr.acquisition.socket.getaddrinfo",
                return_value=public_result,
            ) as resolver,
            patch(
                "vault_pr.acquisition._open_pinned_connection",
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

    def test_explicit_html_mime_overrides_markdown_url_suffix(self) -> None:
        fetched = FetchedSource(
            requested_url="https://example.com/article.md",
            final_url="https://example.com/article.md",
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=(
                b"<html><head><title>HTML Article</title></head><body><main>"
                + b"useful content " * 30
                + b"</main></body></html>"
            ),
            captured_at="2026-07-15T00:00:00+00:00",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)

            def run_defuddle(
                command: list[str], **_: object
            ) -> LimitedProcessResult:
                if "--version" in command:
                    return LimitedProcessResult(0, "defuddle 1.0", "")
                if "-p" in command:
                    return LimitedProcessResult(0, "HTML Article", "")
                (output_dir / "extracted.md").write_text(
                    "# HTML Article\n\n" + "useful content " * 30
                )
                return LimitedProcessResult(0, "", "")

            with (
                patch("vault_pr.acquisition.shutil.which", return_value="defuddle"),
                patch(
                    "vault_pr.acquisition.run_limited_process",
                    side_effect=run_defuddle,
                ),
            ):
                extracted = extract_source(fetched, output_dir)

            self.assertEqual(extracted.raw_filename, "source.html")
            self.assertEqual(extracted.extractor, "defuddle")

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
            ) -> LimitedProcessResult:
                (output_dir / "extracted.md").write_text(
                    "# PDF Title\n\n## Page 1\n\n" + "PDF text " * 30
                )
                return LimitedProcessResult(
                    0,
                    json.dumps({"title": "PDF Title", "extractor_version": "test"}),
                    "",
                )

            with patch("vault_pr.acquisition.run_limited_process", side_effect=run_worker):
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
                patch("vault_pr.acquisition.shutil.which", return_value="defuddle"),
                patch(
                    "vault_pr.acquisition.run_limited_process",
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

    def test_snapshot_rejects_large_file_without_unbounded_read_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = Path(temp_dir) / "large.md"
            with snapshot.open("wb") as file:
                file.truncate(1_000_000)

            with patch.object(
                Path, "read_bytes", side_effect=AssertionError("unbounded read")
            ):
                with self.assertRaisesRegex(IngestError, "exceeds 100 bytes"):
                    snapshot_source(
                        url="https://example.com/article",
                        snapshot_path=snapshot,
                        adapter="authenticated",
                        max_bytes=100,
                    )

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires nofollow file opens")
    def test_snapshot_asset_replacement_with_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            snapshot = root / "article.md"
            snapshot.write_text("# Article\n")
            images = root / "images"
            images.mkdir()
            asset = images / "asset.bin"
            asset.write_bytes(b"original")
            outside = root / "outside.bin"
            outside.write_bytes(b"outside secret")
            output = root / "output"
            output.mkdir()
            original_open = os.open
            replaced = False

            def raced_open(
                path: object,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal replaced
                if path == "asset.bin" and dir_fd is not None and not replaced:
                    replaced = True
                    asset.unlink()
                    asset.symlink_to(outside)
                return original_open(path, flags, mode, dir_fd=dir_fd)

            with patch("vault_pr.source_files.os.open", side_effect=raced_open):
                with self.assertRaisesRegex(IngestError, "changed during capture"):
                    _copy_snapshot_assets(
                        snapshot,
                        output,
                        max_files=10,
                        max_bytes=1000,
                    )

            self.assertEqual(outside.read_bytes(), b"outside secret")
            self.assertFalse((output / "images").exists())

    def test_snapshot_asset_replacement_with_regular_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            snapshot = root / "article.md"
            snapshot.write_text("# Article\n")
            images = root / "images"
            images.mkdir()
            asset = images / "asset.bin"
            asset.write_bytes(b"original")
            outside = root / "outside.bin"
            outside.write_bytes(b"outside secret")
            output = root / "output"
            output.mkdir()
            original_open = os.open
            replaced = False

            def raced_open(
                path: object,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal replaced
                if path == "asset.bin" and dir_fd is not None and not replaced:
                    replaced = True
                    asset.unlink()
                    os.link(outside, asset)
                return original_open(path, flags, mode, dir_fd=dir_fd)

            with patch("vault_pr.source_files.os.open", side_effect=raced_open):
                with self.assertRaisesRegex(IngestError, "changed during capture"):
                    _copy_snapshot_assets(
                        snapshot,
                        output,
                        max_files=10,
                        max_bytes=1000,
                    )

            self.assertEqual(outside.read_bytes(), b"outside secret")
            self.assertFalse((output / "images").exists())


class IngestTests(unittest.TestCase):
    def test_normalize_title_removes_newlines_and_controls(self) -> None:
        self.assertEqual(normalize_title("  A\nTitle\x00  "), "A Title")

    def test_safe_note_filename_is_stable_and_readable(self) -> None:
        filename = safe_note_filename("A/B: Test?", "https://example.com/article")
        self.assertRegex(filename, r"^A-B- Test--[0-9a-f]{8}\.md$")

    def test_source_note_body_cannot_activate_untrusted_obsidian_syntax(self) -> None:
        title = "![[Private Note]] [[Other]] #secret ![x](https://bad.example)"
        fetched = FetchedSource(
            requested_url="https://example.com/a[[Private]]",
            final_url="https://example.com/a[[Private]]",
            status=200,
            headers={"content-type": "text/markdown"},
            body=b"# body\n",
            captured_at="2026-07-15T00:00:00+00:00",
        )
        extracted = ExtractedSource(
            title=title,
            raw_filename="source` ![[Private]].md",
            extracted_filename="extracted` #secret.md",
            extractor="fixture",
            extractor_version="1",
        )

        note = build_source_note(
            title=title,
            source_id="src_11111111111111111111",
            source={
                "kind": "url",
                "access_kind": "public",
                "requested_url": fetched.requested_url,
                "final_url": fetched.final_url,
            },
            captured_at=fetched.captured_at,
            acquisition={"adapter": "http"},
            primary={
                "path": extracted.raw_filename,
                "sha256": "a" * 64,
                "size": 1,
                "mime": "text/markdown",
            },
            extracted={
                "path": extracted.extracted_filename,
                "sha256": "b" * 64,
                "size": 1,
                "mime": "text/markdown",
            },
            note_path=Path("/vault/knowledgebase/sources/source.md"),
            capture_dir=Path(
                "/vault/.vault-pr/sources/public/src_11111111111111111111"
            ),
        )
        body = note.split("---", 2)[2]

        self.assertNotIn("![[", body)
        self.assertNotIn("[[Private", body)
        self.assertNotIn("#secret", body)
        self.assertNotIn("![x](", body)
        self.assertNotIn("![[Private]]", body)
        self.assertNotIn("source`", body)
        self.assertNotIn("extracted`", body)
        self.assertIn("[打开原始链接]", body)
        self.assertIn("%5B%5BPrivate%5D%5D", body)

    def test_ingest_rejects_extractor_with_duplicate_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)

            def duplicate_extraction(
                _fetched: FetchedSource, output: Path, **_: object
            ) -> ExtractedSource:
                (output / "same.md").write_text("# Same\n")
                return ExtractedSource(
                    title="Same",
                    raw_filename="same.md",
                    extracted_filename="same.md",
                    extractor="fixture",
                    extractor_version="1",
                )

            with (
                patch("vault_pr.ingest.fetch_url", return_value=markdown_fetch()),
                patch(
                    "vault_pr.ingest.extract_source",
                    side_effect=duplicate_extraction,
                ),
            ):
                with self.assertRaisesRegex(IngestError, "artifacts alias"):
                    ingest_url(
                        config=make_config(vault),
                        url="https://example.com/article.md",
                        apply=False,
                    )

    def test_ingest_rejects_physically_duplicate_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)

            def hardlinked_extraction(
                _fetched: FetchedSource, output: Path, **_: object
            ) -> ExtractedSource:
                raw = output / "raw.md"
                raw.write_text("# Same\n")
                os.link(raw, output / "extracted.md")
                return ExtractedSource(
                    title="Same",
                    raw_filename="raw.md",
                    extracted_filename="extracted.md",
                    extractor="fixture",
                    extractor_version="1",
                )

            with (
                patch("vault_pr.ingest.fetch_url", return_value=markdown_fetch()),
                patch(
                    "vault_pr.ingest.extract_source",
                    side_effect=hardlinked_extraction,
                ),
            ):
                with self.assertRaisesRegex(IngestError, "artifacts alias"):
                    ingest_url(
                        config=make_config(vault),
                        url="https://example.com/article.md",
                        apply=False,
                    )

    def test_ingest_rejects_undeclared_extractor_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            outside = root / "outside.bin"
            outside.write_bytes(b"SECRET")
            outside.chmod(0o600)

            def malicious_extraction(
                _fetched: FetchedSource, output: Path, **_: object
            ) -> ExtractedSource:
                (output / "raw.md").write_text("# Raw\n")
                (output / "extracted.md").write_text("# Extracted\n")
                (output / "leak.bin").symlink_to(outside)
                return ExtractedSource(
                    title="Malicious",
                    raw_filename="raw.md",
                    extracted_filename="extracted.md",
                    extractor="fixture",
                    extractor_version="1",
                )

            with (
                patch("vault_pr.ingest.fetch_url", return_value=markdown_fetch()),
                patch(
                    "vault_pr.ingest.extract_source",
                    side_effect=malicious_extraction,
                ),
            ):
                with self.assertRaisesRegex(IngestError, "undeclared content"):
                    ingest_url(
                        config=make_config(vault),
                        url="https://example.com/article.md",
                        apply=False,
                    )

            self.assertEqual(outside.read_bytes(), b"SECRET")
            self.assertEqual(outside.stat().st_mode & 0o777, 0o600)

    def test_ingest_rejects_extractor_artifact_through_symlinked_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            outside = root / "outside"
            outside.mkdir()
            (outside / "raw.md").write_text("# Outside\n")

            def symlinked_extraction(
                _fetched: FetchedSource, output: Path, **_: object
            ) -> ExtractedSource:
                (output / "linked").symlink_to(outside, target_is_directory=True)
                (output / "extracted.md").write_text("# Extracted\n")
                return ExtractedSource(
                    title="Linked",
                    raw_filename="linked/raw.md",
                    extracted_filename="extracted.md",
                    extractor="fixture",
                    extractor_version="1",
                )

            with (
                patch("vault_pr.ingest.fetch_url", return_value=markdown_fetch()),
                patch(
                    "vault_pr.ingest.extract_source",
                    side_effect=symlinked_extraction,
                ),
            ):
                with self.assertRaisesRegex(IngestError, "symbolic link"):
                    ingest_url(
                        config=make_config(vault),
                        url="https://example.com/article.md",
                        apply=False,
                    )

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

    def test_source_id_must_be_in_yaml_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_id = "src_11111111111111111111"
            body_only = root / "body-only.md"
            body_only.write_text(
                "---\ntitle: Body only\n---\n\n"
                f"source_id: {source_id}\n"
            )
            wrong_frontmatter = root / "wrong.md"
            wrong_frontmatter.write_text(
                "---\nsource_id: src_22222222222222222222\n---\n\n"
                f"source_id: {source_id}\n"
            )
            correct = root / "correct.md"
            correct.write_text(f"---\nsource_id: {source_id}\n---\n")

            self.assertFalse(source_note_has_id(body_only, source_id))
            self.assertFalse(source_note_has_id(wrong_frontmatter, source_id))
            self.assertTrue(source_note_has_id(correct, source_id))

    def test_capture_rejects_raw_and_extracted_same_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            capture = vault / ".vault-pr" / "sources" / "public" / "pending"
            capture.mkdir(parents=True)
            note = vault / "knowledgebase" / "sources" / "source.md"
            note.parent.mkdir(parents=True)
            digest = "a" * 64
            identity = {
                "schema_version": 2,
                "requested_url": "https://example.com/article",
                "final_url": "https://example.com/article",
                "adapter": "http",
                "raw_sha256": digest,
                "extracted_sha256": digest,
                "asset_sha256": {},
                "artifacts": {
                    "raw": "same.md",
                    "extracted": "same.md",
                    "source_note": str(note.relative_to(vault)),
                },
            }
            source_id = f"src_{source_identity_digest(identity)[:20]}"
            final_capture = capture.parent / source_id
            capture.rename(final_capture)
            note.write_text(f"---\nsource_id: {source_id}\n---\n")
            (final_capture / "same.md").write_bytes(b"same")
            manifest = {
                **identity,
                "source_id": source_id,
                "title": "Same",
            }
            (final_capture / "manifest.json").write_text(
                json.dumps(manifest) + "\n"
            )

            with self.assertRaisesRegex(CaptureError, "manifest schema is invalid"):
                load_validated_capture(
                    final_capture,
                    source_id=source_id,
                    vault=vault,
                )

    def test_capture_rejects_physically_duplicate_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            capture = vault / ".vault-pr" / "sources" / "public" / "pending"
            capture.mkdir(parents=True)
            note = vault / "knowledgebase" / "sources" / "source.md"
            note.parent.mkdir(parents=True)
            digest_value = hashlib.sha256(b"same").hexdigest()
            identity = {
                "schema_version": 2,
                "requested_url": "https://example.com/article",
                "final_url": "https://example.com/article",
                "adapter": "http",
                "raw_sha256": digest_value,
                "extracted_sha256": digest_value,
                "asset_sha256": {},
                "artifacts": {
                    "raw": "raw.md",
                    "extracted": "extracted.md",
                    "source_note": str(note.relative_to(vault)),
                },
            }
            source_id = f"src_{source_identity_digest(identity)[:20]}"
            final_capture = capture.parent / source_id
            capture.rename(final_capture)
            note.write_text(f"---\nsource_id: {source_id}\n---\n")
            raw = final_capture / "raw.md"
            raw.write_bytes(b"same")
            os.link(raw, final_capture / "extracted.md")
            (final_capture / "manifest.json").write_text(
                json.dumps({**identity, "source_id": source_id, "title": "Same"})
                + "\n"
            )

            with self.assertRaisesRegex(CaptureError, "manifest schema is invalid"):
                load_validated_capture(
                    final_capture,
                    source_id=source_id,
                    vault=vault,
                )

    def test_capture_rejects_parent_directory_replaced_by_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            pending = vault / ".vault-pr" / "sources" / "public" / "pending"
            images = pending / "images"
            images.mkdir(parents=True)
            note = vault / "knowledgebase" / "sources" / "source.md"
            note.parent.mkdir(parents=True)
            raw = b"raw"
            extracted = b"# Extracted\n"
            asset = b"asset"
            identity = {
                "schema_version": 2,
                "requested_url": "https://example.com/article",
                "final_url": "https://example.com/article",
                "adapter": "http",
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "extracted_sha256": hashlib.sha256(extracted).hexdigest(),
                "asset_sha256": {
                    "images/cover.bin": hashlib.sha256(asset).hexdigest()
                },
                "artifacts": {
                    "raw": "raw.bin",
                    "extracted": "extracted.md",
                    "source_note": str(note.relative_to(vault)),
                },
            }
            source_id = f"src_{source_identity_digest(identity)[:20]}"
            capture = pending.parent / source_id
            pending.rename(capture)
            images = capture / "images"
            (capture / "raw.bin").write_bytes(raw)
            (capture / "extracted.md").write_bytes(extracted)
            (images / "cover.bin").write_bytes(asset)
            note.write_text(f"---\nsource_id: {source_id}\n---\n")
            (capture / "manifest.json").write_text(
                json.dumps({**identity, "source_id": source_id}) + "\n"
            )
            outside = root / "outside"
            outside.mkdir()
            (outside / "cover.bin").write_bytes(asset)
            parked = capture / "images-original"
            original_open = os.open
            replaced = False

            def raced_open(
                path: object,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal replaced
                if path == "images" and dir_fd is not None and not replaced:
                    replaced = True
                    images.rename(parked)
                    images.symlink_to(outside, target_is_directory=True)
                return original_open(path, flags, mode, dir_fd=dir_fd)

            with patch("vault_pr.capture.os.open", side_effect=raced_open):
                with self.assertRaisesRegex(
                    CaptureError, "manifest schema is invalid"
                ):
                    load_validated_capture(
                        capture,
                        source_id=source_id,
                        vault=vault,
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
            self.assertEqual(manifest["schema_version"], 3)
            self.assertNotIn("domain", manifest)
            self.assertEqual(manifest["acquisition"]["adapter"], "http")
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

    def test_http_markdown_suffix_normalizes_text_plain_primary_mime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            fetched = replace(
                markdown_fetch(),
                headers={"content-type": "text/plain; charset=utf-8"},
            )

            with patch("vault_pr.ingest.fetch_url", return_value=fetched):
                result = ingest_url(
                    config=make_config(vault),
                    url="https://example.com/article.md",
                    apply=True,
                )

            manifest = json.loads(
                (vault / result["capture_dir"] / "manifest.json").read_text()
            )
            self.assertEqual(manifest["primary"]["path"], "source.md")
            self.assertEqual(manifest["primary"]["mime"], "text/markdown")

    def test_apply_retry_returns_active_then_committed_ingest(self) -> None:
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
                active_retry = ingest_url(
                    config=make_config(vault),
                    url="https://example.com/article.md",
                    apply=True,
                )

            self.assertEqual(active_retry["status"], "already_active")
            self.assertEqual(active_retry["transaction_id"], first["transaction_id"])
            self.assertEqual(active_retry["next_action"], "commit")
            self.assertTrue(active_retry["requires_commit"])

            commit_applied_transaction(
                vault,
                vault / ".git",
                first["transaction_id"],
                "Capture fixture source",
            )
            with patch("vault_pr.ingest.fetch_url", return_value=markdown_fetch()):
                committed_retry = ingest_url(
                    config=make_config(vault),
                    url="https://example.com/article.md",
                    apply=True,
                )
            self.assertEqual(committed_retry["status"], "already_committed")
            self.assertEqual(
                committed_retry["transaction_id"], first["transaction_id"]
            )
            self.assertFalse(committed_retry["requires_commit"])

    def test_apply_adopts_valid_committed_capture_without_a_receipt(self) -> None:
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
            config = make_config(vault)
            with patch("vault_pr.ingest.fetch_url", return_value=markdown_fetch()):
                first = ingest_url(
                    config=config,
                    url="https://example.com/article.md",
                    apply=True,
                )
            commit_applied_transaction(
                vault,
                vault / ".git",
                first["transaction_id"],
                "Capture fixture source",
            )
            receipt = (
                vault
                / ".vault-pr"
                / "transaction-receipts"
                / f"{first['transaction_id']}.json"
            )
            receipt.unlink()
            subprocess.run(
                ["git", "-C", str(vault), "add", "--update", "--", str(receipt)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "Remove legacy receipt"],
                check=True,
            )
            adoption_base = subprocess.run(
                ["git", "-C", str(vault), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            refetched = replace(
                markdown_fetch(),
                captured_at="2026-07-15T01:00:00+00:00",
            )
            with patch("vault_pr.ingest.fetch_url", return_value=refetched):
                adopted = ingest_url(
                    config=config,
                    url="https://example.com/article.md",
                    apply=True,
                )

            self.assertEqual(adopted["status"], "adopted")
            self.assertTrue(adopted["adopted_existing"])
            self.assertEqual(adopted["adopted_from_commit"], adoption_base)
            self.assertTrue(adopted["requires_commit"])
            dirty = subprocess.run(
                [
                    "git",
                    "-C",
                    str(vault),
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(len(dirty), 1)
            self.assertIn(first["transaction_id"], dirty[0])

            commit_applied_transaction(
                vault,
                vault / ".git",
                adopted["transaction_id"],
                "Adopt existing capture audit record",
            )
            with patch("vault_pr.ingest.fetch_url", return_value=refetched):
                retried = ingest_url(
                    config=config,
                    url="https://example.com/article.md",
                    apply=True,
                )
            self.assertEqual(retried["status"], "already_committed")
            self.assertTrue(retried["adopted_existing"])

    def test_adoption_rejects_capture_artifacts_not_tracked_in_head(self) -> None:
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
            config = make_config(vault)
            source = markdown_fetch()
            with patch("vault_pr.ingest.fetch_url", return_value=source):
                first = ingest_url(
                    config=config,
                    url="https://example.com/article.md",
                    apply=True,
                )
            commit_applied_transaction(
                vault,
                vault / ".git",
                first["transaction_id"],
                "Capture fixture source",
            )
            receipt = (
                vault
                / ".vault-pr"
                / "transaction-receipts"
                / f"{first['transaction_id']}.json"
            )
            receipt.unlink()
            capture_dir = vault / first["capture_dir"]
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(vault),
                    "rm",
                    "-qr",
                    "--cached",
                    "--",
                    str(capture_dir),
                ],
                check=True,
            )
            (vault / ".gitignore").write_text(
                f"/{first['capture_dir']}/\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", str(vault), "add", "--all"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "Legacy untracked capture"],
                check=True,
            )

            with patch("vault_pr.ingest.fetch_url", return_value=source):
                with self.assertRaisesRegex(IngestError, "match HEAD"):
                    ingest_url(
                        config=config,
                        url="https://example.com/article.md",
                        apply=True,
                    )

            self.assertFalse(receipt.exists())

    def test_adoption_rejects_head_change_before_journal_prepare(self) -> None:
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
            config = make_config(vault)
            source = markdown_fetch()
            with patch("vault_pr.ingest.fetch_url", return_value=source):
                first = ingest_url(
                    config=config,
                    url="https://example.com/article.md",
                    apply=True,
                )
            commit_applied_transaction(
                vault,
                vault / ".git",
                first["transaction_id"],
                "Capture fixture source",
            )
            receipt = (
                vault
                / ".vault-pr"
                / "transaction-receipts"
                / f"{first['transaction_id']}.json"
            )
            receipt.unlink()
            subprocess.run(
                ["git", "-C", str(vault), "add", "--update", "--", str(receipt)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(vault), "commit", "-qm", "Remove legacy receipt"],
                check=True,
            )

            def advance_head(**kwargs: object) -> dict[str, object]:
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(vault),
                        "commit",
                        "--allow-empty",
                        "-qm",
                        "Concurrent head advance",
                    ],
                    check=True,
                )
                return execute_mutation(**kwargs)

            with patch(
                "vault_pr.ingest.fetch_url", return_value=source
            ), patch(
                "vault_pr.ingest.execute_journaled_mutation",
                side_effect=advance_head,
            ):
                with self.assertRaisesRegex(IngestError, "HEAD changed"):
                    ingest_url(
                        config=config,
                        url="https://example.com/article.md",
                        apply=True,
                    )

            self.assertFalse(receipt.exists())
            active_root = vault / ".git" / "vault-pr-active"
            self.assertFalse(
                active_root.exists() and any(active_root.iterdir())
            )

    def test_same_capture_with_different_title_is_a_request_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            with patch("vault_pr.ingest.fetch_url", return_value=markdown_fetch()):
                ingest_url(
                    config=make_config(vault),
                    url="https://example.com/article.md",
                    title_override="First title",
                    apply=True,
                )
                with self.assertRaisesRegex(IngestError, "digest conflict"):
                    ingest_url(
                        config=make_config(vault),
                        url="https://example.com/article.md",
                        title_override="Second title",
                        apply=True,
                    )

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
            raw = capture / manifest["primary"]["path"]
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
            self.assertEqual(
                manifest["acquisition"]["adapter"], "authenticated"
            )
            self.assertEqual(
                manifest["source"]["access_kind"], "authenticated"
            )

    def test_local_file_is_hashed_by_kernel_without_persisting_absolute_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            source = root / "private-name.md"
            source.write_text("# Local reference\n\n" + "content " * 30)

            result = ingest_source(
                config=make_config(vault),
                source_reference=str(source),
                adapter="local_file",
                logical_name="Architecture reference",
                apply=True,
            )

            capture = vault / str(result["capture_dir"])
            manifest_text = (capture / "manifest.json").read_text()
            manifest = json.loads(manifest_text)
            note_text = (vault / str(result["source_note"])).read_text()
            self.assertEqual(manifest["source"]["kind"], "local_file")
            self.assertEqual(manifest["source"]["access_kind"], "local")
            self.assertEqual(
                manifest["source"]["logical_name"], "Architecture reference"
            )
            self.assertNotIn(str(source), manifest_text)
            self.assertNotIn(str(source), note_text)
            self.assertEqual(
                manifest["primary"]["sha256"],
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )

    def test_local_file_identity_ignores_filename_and_logical_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            first = root / "first.md"
            second = root / "second.md"
            first.write_text("# Same\n\n" + "body " * 30)
            second.write_bytes(first.read_bytes())

            first_result = ingest_source(
                config=make_config(vault),
                source_reference=str(first),
                adapter="local_file",
                logical_name="First name",
                apply=False,
            )
            second_result = ingest_source(
                config=make_config(vault),
                source_reference=str(second),
                adapter="local_file",
                logical_name="Second name",
                apply=False,
            )
            self.assertEqual(first_result["source_id"], second_result["source_id"])

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

    def test_wechat_single_file_does_not_implicitly_copy_sibling_images(self) -> None:
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
            self.assertEqual(manifest["acquisition"]["adapter"], "wechat")
            self.assertEqual(manifest["files"], [])
            self.assertFalse((capture / "images" / "cover.png").exists())

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

            self.assertEqual(first["source_id"], second["source_id"])

    def test_wechat_single_file_ignores_undeclared_sibling_file_count(self) -> None:
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

            result = ingest_url(
                config=config,
                url="https://mp.weixin.qq.com/s/example",
                apply=False,
                snapshot_path=snapshot,
                adapter="wechat",
            )
            self.assertEqual(result["status"], "planned")

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

            with self.assertRaisesRegex(IngestError, "offline WeChat snapshot"):
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
            from vault_pr.source_files import materialize_regular_tree

            def fail_capture_copy(
                root: Path,
                expected_hashes: dict[Path, str],
                *,
                destination: Path | None = None,
            ) -> None:
                if destination is not None:
                    raise OSError("injected copy failure")
                materialize_regular_tree(root, expected_hashes)

            with (
                patch("vault_pr.ingest.fetch_url", return_value=markdown_fetch()),
                patch(
                    "vault_pr.ingest.materialize_regular_tree",
                    side_effect=fail_capture_copy,
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
