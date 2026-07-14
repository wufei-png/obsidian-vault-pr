from __future__ import annotations

import os
import sys
import unittest

from vault_pr.process import LimitedProcessTimeout, run_limited_process


class LimitedProcessTests(unittest.TestCase):
    def test_wall_timeout_terminates_child(self) -> None:
        with self.assertRaises(LimitedProcessTimeout):
            run_limited_process(
                [sys.executable, "-c", "while True: pass"],
                timeout_seconds=0.1,
                max_memory_bytes=268_435_456,
                max_file_bytes=16_384,
            )

    @unittest.skipUnless(os.name == "posix", "requires POSIX resource limits")
    def test_file_limit_bounds_child_output_before_capture(self) -> None:
        result = run_limited_process(
            [sys.executable, "-c", "print('x' * 1000000)"],
            timeout_seconds=5,
            max_memory_bytes=268_435_456,
            max_file_bytes=4096,
            max_capture_bytes=1024,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertLessEqual(len(result.stdout.encode()), 1036)
