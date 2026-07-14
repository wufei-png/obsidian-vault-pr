from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from vault_pr.config import ConfigError
from vault_pr.index import update_qmd_index


def make_config(vault: Path, *, max_retries: int = 3) -> dict[str, object]:
    return {
        "vault": {"path": str(vault)},
        "index": {
            "name": "test-notebook",
            "max_retries": max_retries,
            "retry_backoff_seconds": [0, 0, 0],
        },
    }


class SequenceRunner:
    def __init__(self, return_codes: list[int]) -> None:
        self.return_codes = iter(return_codes)
        self.calls = 0

    def __call__(
        self, *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls += 1
        code = next(self.return_codes)
        return subprocess.CompletedProcess(
            args=args,
            returncode=code,
            stdout="updated" if code == 0 else "",
            stderr="temporary failure" if code else "",
        )


class IndexTests(unittest.TestCase):
    def make_vault(self, root: Path) -> Path:
        vault = root / "vault"
        vault.mkdir()
        subprocess.run(["git", "init", "-q", str(vault)], check=True)
        return vault

    def test_retries_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            runner = SequenceRunner([1, 1, 0])

            status = update_qmd_index(
                make_config(vault),
                runner=runner,
                sleeper=lambda _: None,
                executable="qmd",
            )

            self.assertEqual(status["status"], "indexed")
            self.assertEqual(status["attempts"], 3)
            self.assertEqual(runner.calls, 3)

    def test_stops_after_maximum_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            runner = SequenceRunner([1, 1, 1, 1])

            status = update_qmd_index(
                make_config(vault),
                runner=runner,
                sleeper=lambda _: None,
                executable="qmd",
            )

            self.assertEqual(status["status"], "index_failed")
            self.assertEqual(status["attempts"], 4)
            status_file = vault / ".git" / "vault-pr-index-status.json"
            self.assertEqual(
                json.loads(status_file.read_text())["status"], "index_failed"
            )

    def test_requires_one_delay_per_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = self.make_vault(Path(temp_dir))
            config = make_config(vault, max_retries=4)
            with self.assertRaisesRegex(ConfigError, "one value per retry"):
                update_qmd_index(config, executable="qmd")


if __name__ == "__main__":
    unittest.main()
