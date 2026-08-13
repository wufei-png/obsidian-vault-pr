#!/usr/bin/env python3
"""Release-only semantic canary for an explicitly supported qmd runtime."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any


EXPECTED_VERSION = "qmd 2.5.3 (c92adb3)"
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
    if version != EXPECTED_VERSION:
        raise RuntimeError(f"unsupported qmd runtime: {version}")

    fixture = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "qmd_semantic"
    with tempfile.TemporaryDirectory(prefix="vault-pr-qmd-canary-") as temporary:
        index = Path(temporary) / "index.sqlite"
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

    if hidden:
        raise RuntimeError("qmd indexed hidden .vault-pr content")
    if len(results) != 3:
        raise RuntimeError(f"qmd returned {len(results)} visible canary results, expected 3")
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
    if {token for token in EXPECTED_TOKENS if token in snippets} != EXPECTED_TOKENS:
        raise RuntimeError("qmd results lost a duplicate-name or Unicode/spaced-path fixture")
    files = {str(item["file"]).split("?", 1)[0] for item in results}
    if not {
        f"qmd://{COLLECTION}/Alpha/Shared.md",
        f"qmd://{COLLECTION}/Beta/Shared.md",
        f"qmd://{COLLECTION}/Unicode-Notes/知识-图谱.md",
    }.issubset(files):
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
