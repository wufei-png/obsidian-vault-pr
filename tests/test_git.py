from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vault_pr.git import vault_write_lock


class GitLockTests(unittest.TestCase):
    def test_kernel_releases_lock_after_writer_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=True)
            script = (
                "import os, sys\n"
                "from pathlib import Path\n"
                "from vault_pr.git import vault_write_lock\n"
                "lock = vault_write_lock(Path(sys.argv[1]).resolve())\n"
                "lock.__enter__()\n"
                "os._exit(0)\n"
            )

            crashed = subprocess.run(
                [sys.executable, "-c", script, str(vault)], check=False
            )
            self.assertEqual(crashed.returncode, 0)

            with vault_write_lock(vault.resolve()):
                pass
