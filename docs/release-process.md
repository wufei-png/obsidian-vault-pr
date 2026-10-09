# Release process

The repository CI is intentionally small and dependency-locked. A release
candidate must pass:

```bash
export UV_DEFAULT_INDEX=https://pypi.org/simple
uv sync --locked --no-install-project
uv lock --check
uv run --no-sync ruff check .
uv run --no-sync pyright
PYTHONPATH=src uv run --no-sync python -m unittest discover -s tests
PYTHONPATH=src uv run --no-sync coverage run --branch -m unittest discover -s tests
uv run --no-sync coverage report
PYTHONPATH=src uv run --no-sync python -m compileall -q src
npx --yes skills-ref@0.1.5 validate skills/vault-pr
uv build --wheel \
  --build-constraint .github/build-constraints.txt --require-hashes
uv export --locked --no-dev --no-emit-project \
  --format requirements.txt --output-file .smoke-requirements.txt
```

CI runs the Python tests on Linux and macOS with Python 3.11, 3.12, and 3.13. A
separate Python 3.11 quality job runs Ruff, Pyright over `src/`, the full suite
under branch coverage, prints the coverage report without a percentage gate,
and verifies that all checks leave the checkout clean. CI also validates the
skill schema, builds the wheel with a hash-locked PEP 517 environment, installs
runtime dependencies exported from `uv.lock` into a fresh environment, installs
the wheel with `--no-deps`, and smoke-tests the `vault-pr` entry point.
Third-party Actions are pinned to full commit hashes.

Before release, run `uv run --frozen python scripts/qmd-semantic-canary.py` and
`vault-pr doctor` with a machine-local configuration against each supported real
qmd/Defuddle setup. The qmd canary must pass before its exact version enters the
runtime allowlist; it covers hidden `.vault-pr` exclusion, JSON results,
Unicode/spaced paths, duplicate filenames, and (for 2.8.3) names differing only
by spaces, hyphens, or underscores. It also resolves the returned paths through
the application resolver. Supported builds are `qmd 2.5.3 (c92adb3)` and the
stamped release `qmd 2.8.3 (facd35e)`. CI does not contain qmd or a private
Vault and cannot establish live-provider, private-source, full qmd model, or
human semantic-review evidence. Record those results separately and do not turn
a passing fixture suite into a production-readiness claim.
