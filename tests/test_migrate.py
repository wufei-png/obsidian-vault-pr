from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vault_pr.capture import load_validated_capture
from vault_pr.git import GitSafetyError
from vault_pr.migrate import MigrationError, migrate_captures_v3
from vault_pr.recovery import commit_applied_transaction


def _git(vault: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _v2_capture(
    vault: Path,
    *,
    adapter: str = "http",
    old_source_id: str = "src_11111111111111111111",
    note_name: str = "legacy.md",
) -> str:
    raw = b"<main>legacy</main>\n"
    extracted = b"# Legacy\n\nKnowledge.\n"
    note_relative = f"knowledgebase/sources/{note_name}"
    capture = vault / ".vault-pr" / "sources" / "public" / old_source_id
    capture.mkdir(parents=True)
    (capture / "source.html").write_bytes(raw)
    (capture / "extracted.md").write_bytes(extracted)
    note = vault / note_relative
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "---\n"
        "title: Legacy\n"
        "type: source\n"
        "domain: tech\n"
        f'source_id: "{old_source_id}"\n'
        "---\n\n"
        f"# Legacy\n\nProvenance: {old_source_id}\n",
        encoding="utf-8",
    )
    related = vault / "knowledgebase" / "related.md"
    related.write_text(f"source: {old_source_id}\n", encoding="utf-8")
    manifest = {
        "schema_version": 2,
        "source_id": old_source_id,
        "requested_url": "https://example.com/legacy",
        "final_url": "https://example.com/legacy",
        "title": "Legacy",
        "captured_at": "2026-07-15T00:00:00+00:00",
        "http_status": 200,
        "response_headers": {"content-type": "text/html"},
        "domain": "tech",
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "extracted_sha256": hashlib.sha256(extracted).hexdigest(),
        "artifacts": {
            "raw": "source.html",
            "extracted": "extracted.md",
            "source_note": note_relative,
        },
        "extractor": "defuddle",
        "extractor_version": "1",
        "asset_sha256": {},
        "adapter": adapter,
    }
    (capture / "manifest.json").write_text(
        json.dumps(manifest) + "\n", encoding="utf-8"
    )
    return old_source_id


class CaptureMigrationTests(unittest.TestCase):
    def _vault(self, root: Path) -> tuple[Path, dict[str, object]]:
        vault = root / "vault"
        vault.mkdir()
        _git(vault, "init", "-q")
        _git(vault, "config", "user.email", "fixture@example.com")
        _git(vault, "config", "user.name", "Fixture")
        return vault, {
            "vault": {"path": str(vault)},
            "sources": {
                "capture_archive_dir": ".vault-pr/sources/public",
                "visible_notes_dir": "knowledgebase/sources",
            },
        }

    def test_plans_without_writing_dirty_vault(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, config = self._vault(Path(temp_dir))
            old_source_id = _v2_capture(vault)
            _git(vault, "add", "--all")
            _git(vault, "commit", "-qm", "Add legacy fixture")
            (vault / "unrelated.md").write_text("dirty\n", encoding="utf-8")

            result = migrate_captures_v3(config)

            self.assertEqual(result["status"], "planned")
            self.assertEqual(result["migrated_captures"], 1)
            self.assertTrue(
                (vault / ".vault-pr" / "sources" / "public" / old_source_id).is_dir()
            )

    def test_rejects_schema_v1_instead_of_reporting_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, config = self._vault(Path(temp_dir))
            source_id = _v2_capture(vault)
            manifest_path = (
                vault
                / ".vault-pr"
                / "sources"
                / "public"
                / source_id
                / "manifest.json"
            )
            manifest = json.loads(manifest_path.read_text())
            manifest["schema_version"] = 1
            manifest_path.write_text(json.dumps(manifest) + "\n")

            with self.assertRaisesRegex(
                MigrationError, "unsupported capture schema version"
            ):
                migrate_captures_v3(config)

    def test_apply_rejects_dirty_vault_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, config = self._vault(Path(temp_dir))
            old_source_id = _v2_capture(vault)
            _git(vault, "add", "--all")
            _git(vault, "commit", "-qm", "Add legacy fixture")
            unrelated = vault / "unrelated.md"
            unrelated.write_text("dirty\n", encoding="utf-8")

            with self.assertRaisesRegex(GitSafetyError, "not clean"):
                migrate_captures_v3(config, apply=True)

            self.assertTrue(
                (vault / ".vault-pr" / "sources" / "public" / old_source_id).is_dir()
            )
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "dirty\n")

    def test_dedupe_conflict_lists_every_colliding_v2_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, config = self._vault(Path(temp_dir))
            first = _v2_capture(vault)
            second = _v2_capture(
                vault,
                old_source_id="src_22222222222222222222",
                note_name="legacy-copy.md",
            )

            result = migrate_captures_v3(config)

            self.assertEqual(result["status"], "dedupe_conflict")
            conflict = next(iter(result["conflicts"].values()))
            self.assertEqual(conflict, [first, second])

    def test_validation_failure_rolls_back_every_migrated_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, config = self._vault(Path(temp_dir))
            old_source_id = _v2_capture(vault)
            _git(vault, "add", "--all")
            _git(vault, "commit", "-qm", "Add legacy fixture")
            source_note = vault / "knowledgebase" / "sources" / "legacy.md"
            original_note = source_note.read_bytes()

            with mock.patch(
                "vault_pr.migrate.load_validated_capture",
                side_effect=RuntimeError("validation fault"),
            ):
                with self.assertRaisesRegex(MigrationError, "validation fault"):
                    migrate_captures_v3(config, apply=True)

            self.assertTrue(
                (vault / ".vault-pr" / "sources" / "public" / old_source_id).is_dir()
            )
            self.assertEqual(source_note.read_bytes(), original_note)
            self.assertFalse(_git(vault, "status", "--porcelain").stdout)
            git_dir = Path(
                _git(vault, "rev-parse", "--absolute-git-dir").stdout.strip()
            )
            active_root = git_dir / "vault-pr-active"
            self.assertFalse(active_root.exists() and any(active_root.iterdir()))

    def test_applies_and_commits_recoverable_v3_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, config = self._vault(Path(temp_dir))
            old_source_id = _v2_capture(vault)
            _git(vault, "add", "--all")
            _git(vault, "commit", "-qm", "Add legacy fixture")

            result = migrate_captures_v3(config, apply=True)

            self.assertEqual(result["status"], "migrated")
            new_source_id = result["source_ids"][old_source_id]
            self.assertFalse(
                (vault / ".vault-pr" / "sources" / "public" / old_source_id).exists()
            )
            capture = vault / ".vault-pr" / "sources" / "public" / new_source_id
            manifest = load_validated_capture(
                capture, source_id=new_source_id, vault=vault
            )
            self.assertEqual(manifest["schema_version"], 3)
            self.assertEqual(manifest["acquisition"]["adapter"], "http")
            self.assertNotIn("domain", manifest)
            source_note = vault / "knowledgebase" / "sources" / "legacy.md"
            note_content = source_note.read_text(encoding="utf-8")
            self.assertIn(new_source_id, note_content)
            self.assertNotIn(old_source_id, note_content)
            self.assertNotIn("domain:", note_content)
            self.assertIn(
                new_source_id,
                (vault / "knowledgebase" / "related.md").read_text(encoding="utf-8"),
            )

            committed = commit_applied_transaction(
                vault,
                Path(_git(vault, "rev-parse", "--absolute-git-dir").stdout.strip()),
                result["transaction_id"],
                "Upgrade captures to schema v3",
            )
            self.assertEqual(committed["status"], "committed_and_finalized")
            self.assertFalse(_git(vault, "status", "--porcelain").stdout)

    def test_apply_retry_returns_active_then_committed_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, config = self._vault(Path(temp_dir))
            _v2_capture(vault)
            _git(vault, "add", "--all")
            _git(vault, "commit", "-qm", "Add legacy fixture")

            first = migrate_captures_v3(config, apply=True)
            active_retry = migrate_captures_v3(config, apply=True)

            self.assertEqual(active_retry["status"], "already_active")
            self.assertEqual(active_retry["transaction_id"], first["transaction_id"])
            self.assertEqual(active_retry["next_action"], "commit")
            self.assertTrue(active_retry["requires_commit"])

            git_dir = Path(
                _git(vault, "rev-parse", "--absolute-git-dir").stdout.strip()
            )
            commit_applied_transaction(
                vault,
                git_dir,
                first["transaction_id"],
                "Upgrade captures to schema v3",
            )
            committed_retry = migrate_captures_v3(config, apply=True)
            self.assertEqual(committed_retry["status"], "already_committed")
            self.assertEqual(
                committed_retry["transaction_id"], first["transaction_id"]
            )
            self.assertFalse(committed_retry["requires_commit"])


if __name__ == "__main__":
    unittest.main()
