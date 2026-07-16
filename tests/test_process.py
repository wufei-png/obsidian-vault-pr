from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from vault_pr.process import LimitedProcessTimeout, run_limited_process


class LimitedProcessTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_successful_parent_cannot_leave_background_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "background-survived"
            script = (
                "import os, pathlib, sys, time\n"
                "pid = os.fork()\n"
                "if pid == 0:\n"
                "    time.sleep(0.4)\n"
                "    pathlib.Path(sys.argv[1]).write_text('survived')\n"
                "    os._exit(0)\n"
                "raise SystemExit(0)\n"
            )

            result = run_limited_process(
                [sys.executable, "-c", script, str(marker)],
                timeout_seconds=2,
                max_memory_bytes=268_435_456,
                max_file_bytes=16_384,
            )
            time.sleep(0.6)

            self.assertEqual(result.returncode, 0)
            self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_wall_timeout_terminates_descendant_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "descendant-survived"
            script = (
                "import os, pathlib, sys, time\n"
                "pid = os.fork()\n"
                "if pid == 0:\n"
                "    time.sleep(0.4)\n"
                "    pathlib.Path(sys.argv[1]).write_text('survived')\n"
                "    os._exit(0)\n"
                "while True:\n"
                "    time.sleep(1)\n"
            )

            with self.assertRaises(LimitedProcessTimeout):
                run_limited_process(
                    [sys.executable, "-c", script, str(marker)],
                    timeout_seconds=0.1,
                    max_memory_bytes=268_435_456,
                    max_file_bytes=16_384,
                )
            time.sleep(0.6)

            self.assertFalse(marker.exists())

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
