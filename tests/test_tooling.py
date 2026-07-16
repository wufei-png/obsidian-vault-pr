from __future__ import annotations

import unittest
from collections.abc import Sequence

from vault_pr.process import LimitedProcessResult, LimitedProcessTimeout
from vault_pr.tooling import (
    CapabilityProbe,
    ToolProbeError,
    probe_defuddle_capabilities,
    probe_qmd_capabilities,
    probe_tool_capabilities,
)


class ProbeRunner:
    def __init__(self, version: str = "qmd fixture") -> None:
        self.version = version
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> LimitedProcessResult:
        values = list(command)
        self.calls.append(values)
        if values[-1] == "--version":
            return LimitedProcessResult(0, self.version, "")
        if values == ["qmd", "--help"]:
            return LimitedProcessResult(0, "Usage: qmd --index NAME", "")
        if values[-2:] == ["collection", "--help"]:
            return LimitedProcessResult(0, "Commands: show add remove", "")
        if values[-2:] == ["context", "--help"]:
            return LimitedProcessResult(0, "Commands: list add rm", "")
        if values[-2:] == ["update", "--help"]:
            return LimitedProcessResult(0, "Update the index", "")
        if values[-2:] == ["embed", "--help"]:
            return LimitedProcessResult(0, "Options: --max-docs-per-batch", "")
        raise AssertionError(f"unexpected probe command: {values}")


class ToolingTests(unittest.TestCase):
    def test_defuddle_probe_requires_parse_and_output_flags(self) -> None:
        def runner(command: Sequence[str]) -> LimitedProcessResult:
            values = list(command)
            if values[-1] == "--version":
                return LimitedProcessResult(0, "defuddle fixture", "")
            if values == ["defuddle", "--help"]:
                return LimitedProcessResult(0, "Commands: parse", "")
            if values == ["defuddle", "parse", "--help"]:
                return LimitedProcessResult(0, "Options: --md -o -p", "")
            raise AssertionError(values)

        snapshot = probe_defuddle_capabilities("defuddle", runner=runner)
        self.assertEqual(snapshot["tool"], "defuddle")
        self.assertTrue(all(snapshot["capabilities"].values()))

    def test_qmd_probe_records_version_and_required_capabilities(self) -> None:
        runner = ProbeRunner()

        snapshot = probe_qmd_capabilities(
            "qmd", index_name="fixture", runner=runner
        )

        self.assertEqual(snapshot["tool"], "qmd")
        self.assertEqual(snapshot["version"], "qmd fixture")
        self.assertTrue(all(snapshot["capabilities"].values()))
        self.assertEqual(len(runner.calls), 6)

    def test_capability_fingerprint_is_stable_across_version_changes(self) -> None:
        first = probe_qmd_capabilities(
            "qmd", index_name="fixture", runner=ProbeRunner("qmd one")
        )
        second = probe_qmd_capabilities(
            "qmd", index_name="fixture", runner=ProbeRunner("qmd two")
        )

        self.assertNotEqual(first["version"], second["version"])
        self.assertEqual(first["capability_sha256"], second["capability_sha256"])

    def test_missing_required_marker_rejects_tool(self) -> None:
        def runner(command: Sequence[str]) -> LimitedProcessResult:
            if command[-1] == "--version":
                return LimitedProcessResult(0, "tool fixture", "")
            return LimitedProcessResult(0, "different output", "")

        with self.assertRaisesRegex(ToolProbeError, "missing required capabilities"):
            probe_tool_capabilities(
                tool="fixture",
                executable="fixture",
                probes=(CapabilityProbe("needed", ("--help",), ("required",)),),
                runner=runner,
            )

    def test_known_incompatible_version_is_rejected_without_semver_rules(self) -> None:
        runner = ProbeRunner("qmd bad-build")

        with self.assertRaisesRegex(ToolProbeError, "known to be incompatible"):
            probe_tool_capabilities(
                tool="qmd",
                executable="qmd",
                probes=(CapabilityProbe("help", ("--help",), ("--index",)),),
                runner=runner,
                known_incompatible_versions=frozenset({"qmd bad-build"}),
            )

        self.assertEqual(runner.calls, [["qmd", "--version"]])

    def test_probe_timeout_is_reported_as_capability_failure(self) -> None:
        calls = 0

        def runner(_command: Sequence[str]) -> LimitedProcessResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                return LimitedProcessResult(0, "fixture 1", "")
            raise LimitedProcessTimeout("timeout")

        with self.assertRaisesRegex(ToolProbeError, "capability probe timed out"):
            probe_tool_capabilities(
                tool="fixture",
                executable="fixture",
                probes=(CapabilityProbe("help", ("--help",)),),
                runner=runner,
            )

    def test_probe_count_is_bounded(self) -> None:
        probes = tuple(
            CapabilityProbe(f"probe-{index}", ("--help",)) for index in range(17)
        )

        with self.assertRaisesRegex(ToolProbeError, "between 1 and 16"):
            probe_tool_capabilities(
                tool="fixture",
                executable="fixture",
                probes=probes,
                runner=ProbeRunner(),
            )


if __name__ == "__main__":
    unittest.main()
