from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

from vault_pr.process import LimitedProcessResult, LimitedProcessTimeout
from vault_pr.tooling import (
    CapabilityProbe,
    ToolProbeError,
    _qmd_config_path,
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
        if values[-2:] == ["search", "--help"]:
            return LimitedProcessResult(0, "Options: --format", "")
        if values[-2:] == ["embed", "--help"]:
            return LimitedProcessResult(
                0,
                "Options: --max-docs-per-batch --chunk-strategy",
                "",
            )
        if values[-2:] == ["query", "--help"]:
            return LimitedProcessResult(
                0,
                "Syntax: lex: vec: --no-rerank --chunk-strategy",
                "",
            )
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
            "qmd",
            index_name="fixture",
            runner=runner,
            embedding_model_reader=lambda _name, _version: "model-fixture",
        )

        self.assertEqual(snapshot["tool"], "qmd")
        self.assertEqual(snapshot["version"], "qmd fixture")
        self.assertTrue(all(snapshot["capabilities"].values()))
        self.assertEqual(snapshot["retrieval_mode"], "hybrid")
        self.assertEqual(snapshot["chunk_strategy"], "regex")
        self.assertEqual(
            snapshot["embedding_model_sha256"],
            hashlib.sha256(b"model-fixture").hexdigest(),
        )
        self.assertEqual(len(runner.calls), 8)

    def test_capability_fingerprint_is_stable_across_version_changes(self) -> None:
        first = probe_qmd_capabilities(
            "qmd",
            index_name="fixture",
            runner=ProbeRunner("qmd one"),
            embedding_model_reader=lambda _name, _version: "model-fixture",
        )
        second = probe_qmd_capabilities(
            "qmd",
            index_name="fixture",
            runner=ProbeRunner("qmd two"),
            embedding_model_reader=lambda _name, _version: "model-fixture",
        )

        self.assertNotEqual(first["version"], second["version"])
        self.assertEqual(first["capability_sha256"], second["capability_sha256"])
        self.assertNotEqual(
            first["runtime_identity_sha256"],
            second["runtime_identity_sha256"],
        )

    def test_lexical_probe_skips_vector_capabilities_and_model_identity(self) -> None:
        runner = ProbeRunner()

        snapshot = probe_qmd_capabilities(
            "qmd",
            index_name="fixture",
            retrieval_mode="lexical",
            runner=runner,
            embedding_model_reader=lambda _name, _version: self.fail(
                "lexical mode must not read the embedding model"
            ),
        )

        self.assertEqual(snapshot["retrieval_mode"], "lexical")
        self.assertIsNone(snapshot["embedding_model_sha256"])
        self.assertIsNone(snapshot["chunk_strategy"])
        self.assertFalse(
            any(call[-2:] in (["embed", "--help"], ["query", "--help"])
                for call in runner.calls)
        )
        self.assertEqual(len(runner.calls), 6)

    def test_hybrid_probe_hashes_unmaterialized_locator_without_path_leak(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            model = "/private/models/custom-embedding.gguf"
            (config_dir / "fixture.yml").write_text(
                f"models:\n  embed: {model}\n",
                encoding="utf-8",
            )
            environment = {
                key: value
                for key, value in os.environ.items()
                if key not in {"XDG_CONFIG_HOME", "QMD_EMBED_MODEL"}
            }
            environment["QMD_CONFIG_DIR"] = str(config_dir)

            with patch.dict(os.environ, environment, clear=True):
                snapshot = probe_qmd_capabilities(
                    "qmd",
                    index_name="fixture",
                    runner=ProbeRunner(),
                )

            self.assertEqual(
                snapshot["embedding_model_sha256"],
                hashlib.sha256(model.encode("utf-8")).hexdigest(),
            )
            self.assertNotIn(model, str(snapshot))

    def test_hybrid_probe_detects_changed_bytes_at_the_same_model_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir()
            model = root / "private-model.gguf"
            model.write_bytes(b"GGUF-first-model-bytes")
            (config_dir / "fixture.yml").write_text(
                f"models:\n  embed: {model}\n",
                encoding="utf-8",
            )
            environment = {
                key: value
                for key, value in os.environ.items()
                if key not in {"XDG_CONFIG_HOME", "QMD_EMBED_MODEL"}
            }
            environment["QMD_CONFIG_DIR"] = str(config_dir)

            with patch.dict(os.environ, environment, clear=True):
                first = probe_qmd_capabilities(
                    "qmd",
                    index_name="fixture",
                    runner=ProbeRunner(),
                )
                model.write_bytes(b"GGUF-second-model-bytes")
                second = probe_qmd_capabilities(
                    "qmd",
                    index_name="fixture",
                    runner=ProbeRunner(),
                )

            self.assertEqual(
                first["embedding_model_sha256"],
                hashlib.sha256(b"GGUF-first-model-bytes").hexdigest(),
            )
            self.assertEqual(
                second["embedding_model_sha256"],
                hashlib.sha256(b"GGUF-second-model-bytes").hexdigest(),
            )
            self.assertNotEqual(
                first["runtime_identity_sha256"],
                second["runtime_identity_sha256"],
            )
            self.assertNotIn(str(model), str(first))
            self.assertNotIn(str(model), str(second))

    def test_hybrid_probe_hashes_materialized_huggingface_cache_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir()
            cache_root = root / "cache"
            model_cache = cache_root / "qmd" / "models"
            model_cache.mkdir(parents=True)
            locator = "hf:example/model/example-Q8.gguf"
            content = b"GGUF-cached-model-bytes"
            (model_cache / "hf_example_model_example-Q8.gguf").write_bytes(content)
            (config_dir / "fixture.yml").write_text(
                f"models:\n  embed: {locator}\n",
                encoding="utf-8",
            )
            environment = {
                key: value
                for key, value in os.environ.items()
                if key not in {"QMD_EMBED_MODEL", "XDG_CONFIG_HOME"}
            }
            environment.update(
                {
                    "QMD_CONFIG_DIR": str(config_dir),
                    "XDG_CACHE_HOME": str(cache_root),
                }
            )

            with patch.dict(os.environ, environment, clear=True):
                snapshot = probe_qmd_capabilities(
                    "qmd",
                    index_name="fixture",
                    runner=ProbeRunner(),
                )

            self.assertEqual(
                snapshot["embedding_model_sha256"],
                hashlib.sha256(content).hexdigest(),
            )
            self.assertNotIn(locator, str(snapshot))

    def test_hybrid_probe_does_not_adopt_another_hf_repository_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir()
            cache_root = root / "cache"
            model_cache = cache_root / "qmd" / "models"
            model_cache.mkdir(parents=True)
            locator = "hf:example/model/shared-Q8.gguf"
            (model_cache / "hf_other_model_shared-Q8.gguf").write_bytes(
                b"GGUF-unrelated-model-bytes"
            )
            (config_dir / "fixture.yml").write_text(
                f"models:\n  embed: {locator}\n",
                encoding="utf-8",
            )
            environment = {
                key: value
                for key, value in os.environ.items()
                if key not in {"QMD_EMBED_MODEL", "XDG_CONFIG_HOME"}
            }
            environment.update(
                {
                    "QMD_CONFIG_DIR": str(config_dir),
                    "XDG_CACHE_HOME": str(cache_root),
                }
            )

            with patch.dict(os.environ, environment, clear=True):
                snapshot = probe_qmd_capabilities(
                    "qmd",
                    index_name="fixture",
                    runner=ProbeRunner(),
                )

            self.assertEqual(
                snapshot["embedding_model_sha256"],
                hashlib.sha256(locator.encode("utf-8")).hexdigest(),
            )

    def test_path_like_index_name_uses_lexical_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            config_dir = root / "config"
            expected_name = str(alias / "index").replace("/", "_").lstrip("_")

            with (
                patch.dict(os.environ, {"QMD_CONFIG_DIR": str(config_dir)}),
                patch("pathlib.Path.cwd", return_value=root),
                patch("os.getcwd", return_value=str(root)),
            ):
                path = _qmd_config_path("alias/index")

            self.assertEqual(path, config_dir / f"{expected_name}.yml")
            self.assertNotIn("real", path.name)

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
