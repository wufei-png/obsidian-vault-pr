from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from vault_pr.cli import doctor
from vault_pr.config import load_config, load_vault_path


class ConfigTests(unittest.TestCase):
    def test_load_vault_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            vault.mkdir()
            config = root / "config.toml"
            config.write_text(f'[vault]\npath = "{vault}"\n', encoding="utf-8")

            self.assertEqual(load_vault_path(load_config(config)), vault.resolve())

    def test_doctor_rejects_dirty_vault(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            (vault / "untracked.md").write_text("dirty\n", encoding="utf-8")
            config = Path(temp_dir) / "config.toml"
            config.write_text(f'[vault]\npath = "{vault}"\n', encoding="utf-8")

            self.assertEqual(doctor(config), 1)


if __name__ == "__main__":
    unittest.main()
