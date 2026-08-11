# Release process

The repository CI is intentionally small and dependency-locked. A release
candidate must pass:

```bash
uv sync --locked --no-install-project
PYTHONPATH=src uv run --no-sync python -m unittest discover -s tests
PYTHONPATH=src uv run --no-sync python -m compileall -q src
npx --yes skills-ref@0.1.5 validate skills/vault-pr
uv build --wheel \
  --build-constraint .github/build-constraints.txt --require-hashes
uv export --locked --no-dev --no-emit-project \
  --format requirements.txt --output-file .smoke-requirements.txt
```

CI runs the Python checks on Linux and macOS with Python 3.11, 3.12, and 3.13,
validates the skill schema, builds the wheel with a hash-locked PEP 517 build
environment, installs runtime dependencies exported from `uv.lock` into a fresh
environment, installs the wheel with `--no-deps`, and smoke-tests the `vault-pr`
entry point. Third-party Actions are pinned to full commit hashes.

Before release, also run `vault-pr doctor` with a machine-local configuration
against each supported real qmd/Defuddle setup. CI does not contain a private
Vault and cannot establish live-provider, private-source, full qmd model, or
human semantic-review evidence. Record those results separately and do not turn
a passing fixture suite into a production-readiness claim.

Ruff, static type checking, and a coverage threshold are not release gates yet.
They should be introduced only with repository-owned configuration, a clean
baseline, and locked tool versions rather than as drifting ad hoc CI commands.
