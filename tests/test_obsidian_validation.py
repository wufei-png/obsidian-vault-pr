from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from vault_pr.obsidian_validation import (
    ObsidianValidationError,
    validate_proposed_obsidian_state,
)


def _git(vault: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(vault), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _note(title: str, body: str) -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "type: concept\n"
        "aliases: []\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{body}\n"
    )


class ObsidianValidationTests(unittest.TestCase):
    def _vault(self, root: Path) -> tuple[Path, dict[str, object]]:
        vault = root / "vault"
        (vault / "knowledgebase").mkdir(parents=True)
        _git(vault, "init", "-q")
        _git(vault, "config", "user.email", "fixture@example.com")
        _git(vault, "config", "user.name", "Fixture")
        return vault, {
            "notes": {
                "include": ["**/*.md"],
                "exclude": [".git/**", ".obsidian/**", ".vault-pr/**"],
            }
        }

    def _commit(self, vault: Path) -> None:
        _git(vault, "add", ".")
        _git(vault, "commit", "-qm", "fixture")

    def _update(
        self, root: Path, vault: Path, relative: str, content: str
    ) -> list[dict[str, object]]:
        proposed = root / "proposed.md"
        proposed.write_text(content, encoding="utf-8")
        return [
            {
                "action": "update",
                "path": vault / relative,
                "content_file": proposed,
            }
        ]

    def test_accepts_valid_same_note_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, config = self._vault(root)
            path = vault / "knowledgebase" / "note.md"
            path.write_text(_note("Note", "## Details\n\nBody"), encoding="utf-8")
            self._commit(vault)

            result = validate_proposed_obsidian_state(
                config,
                vault,
                self._update(
                    root,
                    vault,
                    "knowledgebase/note.md",
                    _note("Note", "## Details\n\nSee [[#Details]]."),
                ),
            )

            self.assertEqual(result["status"], "valid")

    def test_rejects_new_broken_anchor_but_preserves_existing_debt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, config = self._vault(root)
            path = vault / "knowledgebase" / "note.md"
            path.write_text(
                _note("Note", "Existing [[#Missing]]."),
                encoding="utf-8",
            )
            self._commit(vault)

            preserved = validate_proposed_obsidian_state(
                config,
                vault,
                self._update(
                    root,
                    vault,
                    "knowledgebase/note.md",
                    _note("Note", "Existing [[#Missing]].\n\nUnrelated edit."),
                ),
            )
            self.assertEqual(preserved["status"], "valid")

            with self.assertRaisesRegex(
                ObsidianValidationError, "broken_anchor"
            ):
                validate_proposed_obsidian_state(
                    config,
                    vault,
                    self._update(
                        root,
                        vault,
                        "knowledgebase/note.md",
                        _note(
                            "Note",
                            "Existing [[#Missing]].\n\nNew [[#Also Missing]].",
                        ),
                    ),
                )

    def test_does_not_validate_anchor_on_unmodified_external_note(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, config = self._vault(root)
            source = vault / "knowledgebase" / "source.md"
            target = vault / "knowledgebase" / "target.md"
            source.write_text(_note("Source", "Body"), encoding="utf-8")
            target.write_text(_note("Target", "No matching heading."), encoding="utf-8")
            self._commit(vault)

            result = validate_proposed_obsidian_state(
                config,
                vault,
                self._update(
                    root,
                    vault,
                    "knowledgebase/source.md",
                    _note("Source", "See [[Target#Missing Heading]]."),
                ),
            )

            self.assertEqual(result["status"], "valid")

    def test_accepts_angle_bracket_attachment_path_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, config = self._vault(root)
            note = vault / "knowledgebase" / "note.md"
            note.write_text(_note("Note", "Body"), encoding="utf-8")
            attachment = vault / "attachments" / "diagram one.png"
            attachment.parent.mkdir()
            attachment.write_bytes(b"tracked fixture")
            self._commit(vault)

            result = validate_proposed_obsidian_state(
                config,
                vault,
                self._update(
                    root,
                    vault,
                    "knowledgebase/note.md",
                    _note("Note", "![Diagram](<../attachments/diagram one.png>)"),
                ),
            )

            self.assertEqual(result["status"], "valid")

    def test_rejects_direct_embed_of_internal_capture_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, config = self._vault(root)
            note = vault / "knowledgebase" / "note.md"
            note.write_text(_note("Note", "Body"), encoding="utf-8")
            capture_asset = (
                vault
                / ".vault-pr"
                / "sources"
                / "src_11111111111111111111"
                / "cover.png"
            )
            capture_asset.parent.mkdir(parents=True)
            capture_asset.write_bytes(b"tracked capture asset")
            self._commit(vault)

            with self.assertRaisesRegex(
                ObsidianValidationError, "asset_promotion_required"
            ):
                validate_proposed_obsidian_state(
                    config,
                    vault,
                    self._update(
                        root,
                        vault,
                        "knowledgebase/note.md",
                        _note(
                            "Note",
                            "![[.vault-pr/sources/"
                            "src_11111111111111111111/cover.png]]",
                        ),
                    ),
                )


if __name__ == "__main__":
    unittest.main()
