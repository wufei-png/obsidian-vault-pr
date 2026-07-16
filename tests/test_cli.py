from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from vault_pr.cli import build_parser, doctor
from vault_pr.config import (
    ConfigError,
    index_spec,
    load_config,
    load_vault_path,
    note_path_allowed,
    source_notes_dir,
)


class ConfigTests(unittest.TestCase):
    def test_load_vault_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            vault.mkdir()
            config = root / "config.toml"
            config.write_text(f'[vault]\npath = "{vault}"\n', encoding="utf-8")

            self.assertEqual(load_vault_path(load_config(config)), vault.resolve())

    def test_ingest_no_longer_accepts_a_domain_argument(self) -> None:
        parser = build_parser()
        parsed = parser.parse_args(["ingest", "https://example.com/article"])
        self.assertFalse(hasattr(parsed, "domain"))
        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["ingest", "https://example.com/article", "--domain", "tech"]
            )

    def test_loads_single_vault_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = (Path(temp_dir) / "vault").resolve()
            config = {
                "vault": {"path": str(vault)},
                "index": {
                    "collection": "fixture-vault",
                },
            }

            spec = index_spec(config, vault)
            self.assertEqual(spec.root_dir, vault)
            self.assertEqual(spec.collection, "fixture-vault")

    def test_rejects_source_notes_outside_vault(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = (Path(temp_dir) / "vault").resolve()
            config = {
                "vault": {"path": str(vault)},
                "sources": {"visible_notes_dir": "../outside"},
            }

            with self.assertRaisesRegex(ConfigError, "escapes the vault"):
                source_notes_dir(config, vault)

    def test_rejects_configurable_index_subroots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir).resolve()
            config = {"index": {"collection": "fixture", "root_dir": "notes"}}

            with self.assertRaisesRegex(ConfigError, "always indexes the vault root"):
                index_spec(config, vault)

    def test_note_write_allowlist_is_vault_wide_and_excludes_internal_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir).resolve()
            config = {"notes": {"include": ["**/*.md"]}}

            self.assertTrue(note_path_allowed(config, vault, vault / "商汤" / "a.md"))
            self.assertTrue(note_path_allowed(config, vault, vault / "top.md"))
            self.assertFalse(
                note_path_allowed(config, vault, vault / ".obsidian" / "readme.md")
            )
            relaxed = {"notes": {"include": ["**/*.md"], "exclude": ["tmp/**"]}}
            self.assertFalse(
                note_path_allowed(relaxed, vault, vault / ".vault-pr" / "audit.md")
            )

            single_level = {"notes": {"include": ["notes/*.md"]}}
            self.assertTrue(
                note_path_allowed(single_level, vault, vault / "notes" / "one.md")
            )
            self.assertFalse(
                note_path_allowed(
                    single_level, vault, vault / "notes" / "private" / "deep.md"
                )
            )
            recursive = {
                "notes": {
                    "include": ["notes/**/*.md"],
                    "exclude": ["notes/private/**"],
                }
            }
            self.assertTrue(
                note_path_allowed(recursive, vault, vault / "notes" / "one.md")
            )
            self.assertFalse(
                note_path_allowed(
                    recursive, vault, vault / "notes" / "private" / "deep.md"
                )
            )

    def test_doctor_rejects_dirty_vault(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            (vault / "untracked.md").write_text("dirty\n", encoding="utf-8")
            config = Path(temp_dir) / "config.toml"
            config.write_text(f'[vault]\npath = "{vault}"\n', encoding="utf-8")

            self.assertEqual(doctor(config), 1)

    def test_doctor_keeps_json_protocol_for_invalid_active_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            active_root = vault / ".git" / "vault-pr-active"
            active_root.write_text("not a directory\n", encoding="utf-8")
            config = Path(temp_dir) / "config.toml"
            config.write_text(f'[vault]\npath = "{vault}"\n', encoding="utf-8")
            output = io.StringIO()

            with redirect_stdout(output):
                status = doctor(config)

            payload = json.loads(output.getvalue())
            self.assertEqual(status, 1)
            self.assertEqual(payload["status"], "unhealthy")
            active_check = next(
                item
                for item in payload["data"]["checks"]
                if item["check"] == "active_transaction"
            )
            self.assertEqual(active_check["status"], "fail")


if __name__ == "__main__":
    unittest.main()
