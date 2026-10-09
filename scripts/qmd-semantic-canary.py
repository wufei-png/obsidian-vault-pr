#!/usr/bin/env python3
"""Release-only semantic canary for an explicitly supported qmd runtime."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from vault_pr.retrieve import build_qmd_path_map, resolve_qmd_path
from vault_pr.tooling import (
    QMD_ALLOWED_VERSION_OUTPUTS,
    QMD_PATH_PRESERVING_VERSION_OUTPUTS,
)

COLLECTION = "qmd-semantic-canary"
EXPECTED_TOKENS = {
    "QMD_CANARY_ALPHA_DUPLICATE",
    "QMD_CANARY_BETA_DUPLICATE",
    "QMD_CANARY_UNICODE_SPACED_PATH",
}


def _run(command: list[str]) -> str:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"qmd command failed: {detail}")
    return result.stdout


def _search(qmd: str, index: Path, query: str) -> list[dict[str, Any]]:
    output = _run(
        [
            qmd,
            "--index",
            str(index),
            "search",
            query,
            "--collection",
            COLLECTION,
            "--all",
            "--format",
            "json",
        ]
    )
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("qmd search did not return JSON") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise RuntimeError("qmd search returned an unsupported JSON shape")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qmd", default="qmd")
    args = parser.parse_args()
    version = _run([args.qmd, "--version"]).strip()
    if version not in QMD_ALLOWED_VERSION_OUTPUTS:
        raise RuntimeError(f"unsupported qmd runtime: {version}")

    fixture = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "qmd_semantic"
    preserve_paths = version in QMD_PATH_PRESERVING_VERSION_OUTPUTS
    expected_tokens = set(EXPECTED_TOKENS)
    expected_paths = {
        "Alpha/Shared.md",
        "Beta/Shared.md",
        "Unicode Notes/知识 图谱.md" if preserve_paths else "Unicode-Notes/知识-图谱.md",
    }
    with tempfile.TemporaryDirectory(prefix="vault-pr-qmd-canary-") as temporary:
        index = Path(temporary) / "index.sqlite"
        fixture = shutil.copytree(fixture, Path(temporary) / "fixture").resolve()
        if preserve_paths:
            collisions = fixture / "Collision"
            collisions.mkdir()
            for index_number, filename in enumerate(("A B.md", "A-B.md", "A_B.md")):
                token = f"QMD_CANARY_COLLISION_{index_number}"
                (collisions / filename).write_text(f"# {filename}\n\n{token}\n")
                expected_tokens.add(token)
                expected_paths.add(f"Collision/{filename}")
        _run(
            [
                args.qmd,
                "--index",
                str(index),
                "collection",
                "add",
                str(fixture),
                "--name",
                COLLECTION,
                "--mask",
                "**/*.md",
            ]
        )
        _run([args.qmd, "--index", str(index), "update"])
        results = _search(args.qmd, index, "QMD_CANARY")
        hidden = _search(args.qmd, index, "QMD_CANARY_HIDDEN_INTERNAL")
        path_map = build_qmd_path_map(fixture, preserve_paths=preserve_paths)
        for item in results:
            parsed = urlsplit(str(item.get("file", "")))
            if parsed.scheme != "qmd" or parsed.netloc != COLLECTION:
                raise RuntimeError("qmd returned a result outside the fixture collection")
            resolve_qmd_path(fixture, unquote(parsed.path).lstrip("/"), path_map)

    if hidden:
        raise RuntimeError("qmd indexed hidden .vault-pr content")
    if len(results) != len(expected_paths):
        raise RuntimeError(
            f"qmd returned {len(results)} visible canary results, "
            f"expected {len(expected_paths)}"
        )
    for item in results:
        if not all(
            isinstance(item.get(field), expected)
            for field, expected in (
                ("docid", str),
                ("score", (int, float)),
                ("file", str),
                ("line", int),
                ("title", str),
                ("snippet", str),
            )
        ):
            raise RuntimeError("qmd result lacks the required structured fields")
    snippets = "\n".join(str(item["snippet"]) for item in results)
    if {token for token in expected_tokens if token in snippets} != expected_tokens:
        raise RuntimeError("qmd results lost a duplicate-name or Unicode/spaced-path fixture")
    files = {unquote(urlsplit(str(item["file"])).path).lstrip("/") for item in results}
    if files != expected_paths:
        raise RuntimeError("qmd virtual paths do not preserve the required identities")

    print(
        json.dumps(
            {
                "status": "pass",
                "qmd_version": version,
                "visible_documents": len(results),
                "hidden_internal_documents": len(hidden),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
