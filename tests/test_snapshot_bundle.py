from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from vault_pr.snapshot_bundle import (
    SnapshotBundleError,
    inspect_single_snapshot,
    validate_snapshot_bundle,
)


def _record(path: str, content: bytes, mime: str, role: str | None = None) -> dict:
    record = {
        "path": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "mime": mime,
    }
    if role is not None:
        record["role"] = role
    return record


def _write_manifest(
    root: Path,
    *,
    primary: dict,
    files: list[dict],
    provider: dict | None = None,
    extra: dict | None = None,
) -> None:
    manifest = {
        "schema_version": 1,
        "primary": primary,
        "files": files,
        "provider": provider
        or {
            "name": "fixture-provider",
            "version": "1.0",
            "acquisition": "authenticated-browser",
        },
    }
    if extra:
        manifest.update(extra)
    (root / "snapshot-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class SnapshotBundleTests(unittest.TestCase):
    def _bundle(self, root: Path) -> tuple[bytes, bytes, bytes]:
        primary = b"# Article\n\nGrounded body.\n"
        image = b"\x89PNG\r\n\x1a\nfixture image bytes"
        supplement = b"Provider context\n"
        (root / "article.md").write_bytes(primary)
        (root / "images").mkdir()
        (root / "images" / "cover.png").write_bytes(image)
        (root / "context.txt").write_bytes(supplement)
        _write_manifest(
            root,
            primary=_record("article.md", primary, "text/markdown"),
            files=[
                _record("images/cover.png", image, "image/png", "asset"),
                _record("context.txt", supplement, "text/plain", "supplement"),
            ],
        )
        return primary, image, supplement

    def test_inspects_and_copies_one_file_while_computing_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "article.md"
            content = b"# Article\n\n" + b"body " * 30_000
            source.write_bytes(content)
            copied = root / "copied" / "article.md"

            result = inspect_single_snapshot(
                source,
                max_bytes=len(content),
                destination=copied,
            )

            self.assertEqual(result["path"], "article.md")
            self.assertEqual(result["sha256"], hashlib.sha256(content).hexdigest())
            self.assertEqual(result["size"], len(content))
            self.assertEqual(result["mime"], "text/markdown")
            self.assertEqual(copied.read_bytes(), content)

    def test_single_file_rejects_signature_mismatch_and_removes_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "document.pdf"
            source.write_bytes(b"not a PDF")
            copied = root / "copied.pdf"

            with self.assertRaisesRegex(
                SnapshotBundleError, "unsupported or mismatched"
            ):
                inspect_single_snapshot(
                    source,
                    max_bytes=100,
                    destination=copied,
                )

            self.assertFalse(copied.exists())

    def test_single_file_enforces_streamed_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "article.md"
            source.write_bytes(b"x" * 100)
            copied = root / "copied.md"

            with self.assertRaisesRegex(SnapshotBundleError, "exceeds 99 bytes"):
                inspect_single_snapshot(
                    source,
                    max_bytes=99,
                    destination=copied,
                )

            self.assertFalse(copied.exists())

    def test_validates_and_materializes_exact_declared_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bundle"
            root.mkdir()
            primary, image, supplement = self._bundle(root)
            destination = Path(temp_dir) / "materialized"

            result = validate_snapshot_bundle(
                root,
                max_primary_bytes=1_000,
                max_files=2,
                max_total_bytes=10_000,
                destination=destination,
            )

            self.assertEqual(result["primary"]["path"], "article.md")
            self.assertEqual(
                [item["path"] for item in result["files"]],
                ["context.txt", "images/cover.png"],
            )
            self.assertEqual((destination / "article.md").read_bytes(), primary)
            self.assertEqual(
                (destination / "images" / "cover.png").read_bytes(), image
            )
            self.assertEqual(
                (destination / "context.txt").read_bytes(), supplement
            )
            self.assertTrue((destination / "snapshot-manifest.json").is_file())

    def test_rejects_unknown_manifest_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary = b"# Article\n"
            (root / "article.md").write_bytes(primary)
            _write_manifest(
                root,
                primary=_record("article.md", primary, "text/markdown"),
                files=[],
                extra={"unexpected": True},
            )

            with self.assertRaisesRegex(
                SnapshotBundleError, "unknown fields"
            ):
                validate_snapshot_bundle(
                    root,
                    max_primary_bytes=1_000,
                    max_files=0,
                    max_total_bytes=1_000,
                )

    def test_rejects_unknown_provider_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary = b"# Article\n"
            (root / "article.md").write_bytes(primary)
            _write_manifest(
                root,
                primary=_record("article.md", primary, "text/markdown"),
                files=[],
                provider={
                    "name": "fixture",
                    "version": "1",
                    "acquisition": "local",
                    "token": "must not be accepted",
                },
            )

            with self.assertRaisesRegex(
                SnapshotBundleError, "provider has unknown"
            ):
                validate_snapshot_bundle(
                    root,
                    max_primary_bytes=1_000,
                    max_files=0,
                    max_total_bytes=1_000,
                )

    def test_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root.parent / "outside.md"
            outside.write_bytes(b"# Outside\n")
            _write_manifest(
                root,
                primary=_record(
                    "../outside.md", outside.read_bytes(), "text/markdown"
                ),
                files=[],
            )

            with self.assertRaisesRegex(SnapshotBundleError, "path is invalid"):
                validate_snapshot_bundle(
                    root,
                    max_primary_bytes=1_000,
                    max_files=0,
                    max_total_bytes=1_000,
                )

    def test_rejects_missing_declared_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            content = b"# Missing\n"
            _write_manifest(
                root,
                primary=_record("missing.md", content, "text/markdown"),
                files=[],
            )

            with self.assertRaisesRegex(
                SnapshotBundleError, "missing declared files"
            ):
                validate_snapshot_bundle(
                    root,
                    max_primary_bytes=1_000,
                    max_files=0,
                    max_total_bytes=1_000,
                )

    def test_enforces_declared_file_and_total_size_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary = b"# Article\n"
            supplement = b"context\n"
            (root / "article.md").write_bytes(primary)
            (root / "context.txt").write_bytes(supplement)
            _write_manifest(
                root,
                primary=_record("article.md", primary, "text/markdown"),
                files=[
                    _record(
                        "context.txt", supplement, "text/plain", "supplement"
                    )
                ],
            )

            with self.assertRaisesRegex(SnapshotBundleError, "0-file limit"):
                validate_snapshot_bundle(
                    root,
                    max_primary_bytes=1_000,
                    max_files=0,
                    max_total_bytes=1_000,
                )
            with self.assertRaisesRegex(SnapshotBundleError, "exceeds 10 bytes"):
                validate_snapshot_bundle(
                    root,
                    max_primary_bytes=1_000,
                    max_files=1,
                    max_total_bytes=10,
                )

    def test_rejects_undeclared_files_and_does_not_discover_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary = b"# Article\n"
            (root / "article.md").write_bytes(primary)
            (root / "images").mkdir()
            (root / "images" / "cover.png").write_bytes(
                b"\x89PNG\r\n\x1a\nundeclared"
            )
            _write_manifest(
                root,
                primary=_record("article.md", primary, "text/markdown"),
                files=[],
            )

            with self.assertRaisesRegex(
                SnapshotBundleError, "undeclared content"
            ):
                validate_snapshot_bundle(
                    root,
                    max_primary_bytes=1_000,
                    max_files=0,
                    max_total_bytes=1_000,
                )

    def test_rejects_symlink_anywhere_in_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary = b"# Article\n"
            (root / "article.md").write_bytes(primary)
            (root / "linked.md").symlink_to(root / "article.md")
            _write_manifest(
                root,
                primary=_record("article.md", primary, "text/markdown"),
                files=[
                    _record(
                        "linked.md",
                        primary,
                        "text/markdown",
                        "supplement",
                    )
                ],
            )

            with self.assertRaisesRegex(SnapshotBundleError, "symbolic link"):
                validate_snapshot_bundle(
                    root,
                    max_primary_bytes=1_000,
                    max_files=1,
                    max_total_bytes=2_000,
                )

    def test_rejects_symlinked_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            real_manifest = root / "real.json"
            real_manifest.write_text("{}\n", encoding="utf-8")
            (root / "snapshot-manifest.json").symlink_to(real_manifest)

            with self.assertRaisesRegex(SnapshotBundleError, "symbolic link"):
                validate_snapshot_bundle(
                    root,
                    max_primary_bytes=1_000,
                    max_files=0,
                    max_total_bytes=1_000,
                )

    def test_rejects_hardlink_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary = b"# Article\n"
            (root / "article.md").write_bytes(primary)
            os.link(root / "article.md", root / "copy.md")
            _write_manifest(
                root,
                primary=_record("article.md", primary, "text/markdown"),
                files=[
                    _record(
                        "copy.md",
                        primary,
                        "text/markdown",
                        "supplement",
                    )
                ],
            )

            with self.assertRaisesRegex(SnapshotBundleError, "physically alias"):
                validate_snapshot_bundle(
                    root,
                    max_primary_bytes=1_000,
                    max_files=1,
                    max_total_bytes=2_000,
                )

    def test_accepts_duplicate_content_in_distinct_declared_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary = b"# Article\n"
            duplicate = b"same supplement\n"
            (root / "article.md").write_bytes(primary)
            (root / "one.txt").write_bytes(duplicate)
            (root / "two.txt").write_bytes(duplicate)
            _write_manifest(
                root,
                primary=_record("article.md", primary, "text/markdown"),
                files=[
                    _record("one.txt", duplicate, "text/plain", "supplement"),
                    _record("two.txt", duplicate, "text/plain", "supplement"),
                ],
            )

            result = validate_snapshot_bundle(
                root,
                max_primary_bytes=1_000,
                max_files=2,
                max_total_bytes=2_000,
            )

            self.assertEqual(len(result["files"]), 2)

    def test_revalidates_hash_size_and_mime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary = b"# Article\n"
            image = b"\x89PNG\r\n\x1a\nimage"
            (root / "article.md").write_bytes(primary)
            (root / "cover.png").write_bytes(image)
            bad_image_record = _record("cover.png", image, "image/jpeg", "asset")
            _write_manifest(
                root,
                primary=_record("article.md", primary, "text/markdown"),
                files=[bad_image_record],
            )

            with self.assertRaisesRegex(
                SnapshotBundleError, "does not match its manifest"
            ):
                validate_snapshot_bundle(
                    root,
                    max_primary_bytes=1_000,
                    max_files=1,
                    max_total_bytes=2_000,
                )

            bad_image_record["mime"] = "image/png"
            bad_image_record["size"] += 1
            _write_manifest(
                root,
                primary=_record("article.md", primary, "text/markdown"),
                files=[bad_image_record],
            )
            with self.assertRaisesRegex(
                SnapshotBundleError, "does not match its manifest"
            ):
                validate_snapshot_bundle(
                    root,
                    max_primary_bytes=1_000,
                    max_files=1,
                    max_total_bytes=2_000,
                )

            bad_image_record["size"] -= 1
            bad_image_record["sha256"] = "0" * 64
            _write_manifest(
                root,
                primary=_record("article.md", primary, "text/markdown"),
                files=[bad_image_record],
            )
            with self.assertRaisesRegex(
                SnapshotBundleError, "does not match its manifest"
            ):
                validate_snapshot_bundle(
                    root,
                    max_primary_bytes=1_000,
                    max_files=1,
                    max_total_bytes=2_000,
                )

    def test_rejects_asset_role_for_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary = b"# Article\n"
            supplement = b"# More\n"
            (root / "article.md").write_bytes(primary)
            (root / "more.md").write_bytes(supplement)
            _write_manifest(
                root,
                primary=_record("article.md", primary, "text/markdown"),
                files=[
                    _record("more.md", supplement, "text/markdown", "asset")
                ],
            )

            with self.assertRaisesRegex(SnapshotBundleError, "not allowed"):
                validate_snapshot_bundle(
                    root,
                    max_primary_bytes=1_000,
                    max_files=1,
                    max_total_bytes=2_000,
                )

    def test_failure_cleans_partial_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bundle"
            root.mkdir()
            self._bundle(root)
            (root / "unexpected.txt").write_text("not declared", encoding="utf-8")
            destination = Path(temp_dir) / "materialized"

            with self.assertRaisesRegex(SnapshotBundleError, "undeclared content"):
                validate_snapshot_bundle(
                    root,
                    max_primary_bytes=1_000,
                    max_files=2,
                    max_total_bytes=10_000,
                    destination=destination,
                )

            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
