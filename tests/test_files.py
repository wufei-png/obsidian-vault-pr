from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vault_pr.files import atomic_replace_bytes


class AtomicFileTests(unittest.TestCase):
    def test_replaces_symlink_without_overwriting_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside.txt"
            outside.write_bytes(b"preserve\n")
            target = root / "target.json"
            target.symlink_to(outside)

            atomic_replace_bytes(target, b'{"safe": true}\n')

            self.assertEqual(outside.read_bytes(), b"preserve\n")
            self.assertFalse(target.is_symlink())
            self.assertEqual(target.read_bytes(), b'{"safe": true}\n')


if __name__ == "__main__":
    unittest.main()
