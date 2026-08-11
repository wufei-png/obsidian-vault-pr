from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from vault_pr.cli import build_parser, doctor, main
from vault_pr.config import (
    ConfigError,
    index_spec,
    load_config,
    load_vault_path,
    note_path_allowed,
    retrieval_mode,
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
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(
                ["ingest", "https://example.com/article", "--domain", "tech"]
            )

    def test_plan_init_no_longer_accepts_a_chain_id(self) -> None:
        parser = build_parser()
        base_args = [
            "plan",
            "init",
            "--summary",
            "Integrate source",
            "--authorization-summary",
            "Integrate the captured source",
        ]
        parsed = parser.parse_args(base_args)
        self.assertFalse(hasattr(parsed, "chain_id"))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args([*base_args, "--chain-id", "chain_example"])

    def test_parser_errors_use_one_json_envelope(self) -> None:
        output = io.StringIO()

        with redirect_stderr(output), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["ingest"])

        self.assertEqual(raised.exception.code, 2)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["command"], "cli")
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error"]["code"], "invalid_arguments")
        self.assertIn("usage", payload["error"]["details"])

    def test_unexpected_runtime_error_is_redacted_json_without_debug(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            config = Path(temp_dir) / "config.toml"
            config.write_text(f'[vault]\npath = "{vault.resolve()}"\n')
            output = io.StringIO()

            with patch(
                "vault_pr.cli.ingest_source",
                side_effect=RuntimeError(f"secret failure at {vault}"),
            ), redirect_stderr(output):
                status = main(
                    [
                        "ingest",
                        "https://example.com/article",
                        "--config",
                        str(config),
                    ]
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(status, 2)
            self.assertEqual(payload["command"], "ingest")
            self.assertEqual(payload["error"]["code"], "execution_error")
            self.assertEqual(
                payload["error"]["message"], "unexpected execution failure"
            )
            self.assertNotIn(str(vault), output.getvalue())

    def test_debug_reraises_unexpected_runtime_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            config = Path(temp_dir) / "config.toml"
            config.write_text(f'[vault]\npath = "{vault.resolve()}"\n')

            with patch(
                "vault_pr.cli.ingest_source",
                side_effect=RuntimeError("debug fixture"),
            ), self.assertRaisesRegex(RuntimeError, "debug fixture"):
                main(
                    [
                        "--debug",
                        "ingest",
                        "https://example.com/article",
                        "--config",
                        str(config),
                    ]
                )

    def test_transaction_subcommands_parse_stable_arguments(self) -> None:
        parser = build_parser()

        listed = parser.parse_args(["transaction", "list"])
        shown = parser.parse_args(["transaction", "show", "txn_fixture"])
        diffed = parser.parse_args(["transaction", "diff", "txn_fixture"])

        self.assertEqual(listed.transaction_command, "list")
        self.assertEqual(shown.transaction_id, "txn_fixture")
        self.assertEqual(diffed.transaction_command, "diff")

    def test_transaction_list_emits_success_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            config = Path(temp_dir) / "config.toml"
            config.write_text(f'[vault]\npath = "{vault.resolve()}"\n')
            output = io.StringIO()

            with redirect_stdout(output):
                status = main(
                    ["transaction", "list", "--config", str(config)]
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["command"], "transaction.list")
            self.assertEqual(payload["status"], "listed")
            self.assertEqual(payload["data"]["transactions"], [])

    def test_plan_init_rejects_half_of_authorization_request_pair_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            config = Path(temp_dir) / "config.toml"
            config.write_text(f'[vault]\npath = "{vault.resolve()}"\n')
            output = io.StringIO()

            with redirect_stderr(output), self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "plan",
                        "init",
                        "--summary",
                        "Fixture plan",
                        "--authorization-summary",
                        "Fixture authority",
                        "--request-ref",
                        "request-1",
                        "--config",
                        str(config),
                    ]
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(payload["error"]["code"], "invalid_arguments")
            self.assertIn("provided together", payload["error"]["message"])

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

    def test_retrieval_mode_defaults_to_hybrid_and_accepts_lexical(self) -> None:
        self.assertEqual(retrieval_mode({"index": {}}), "hybrid")
        self.assertEqual(
            retrieval_mode({"index": {"retrieval_mode": "lexical"}}),
            "lexical",
        )

    def test_rejects_removed_embed_setting_with_migration_guidance(self) -> None:
        with self.assertRaisesRegex(
            ConfigError,
            r"index\.embed was replaced.*'hybrid'.*'lexical'",
        ):
            retrieval_mode({"index": {"embed": False}})

    def test_rejects_unknown_retrieval_mode(self) -> None:
        with self.assertRaisesRegex(ConfigError, "lexical, hybrid"):
            retrieval_mode({"index": {"retrieval_mode": "full"}})

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
