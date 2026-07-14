# Obsidian Vault PR

`obsidian-vault-pr` is a governance and change-management layer for existing
Obsidian vaults. It lets an agent ingest sources, update interlinked notes, run
deterministic checks, request semantic review for risky changes, and leave an
auditable Git transaction.

The repository is private during incubation. The first development target is a
real existing vault configured in the Git-ignored `vault-pr.local.toml` file;
private vault content and machine-specific paths must never be committed here.

## Names

- Repository and Python distribution: `obsidian-vault-pr`
- CLI: `vault-pr`
- Agent skill: `vault-pr`

## Current status

The first vertical slice provides:

- `vault-pr doctor` for read-only environment and Git safety checks;
- `vault-pr ingest <url> --domain <name>` for immutable capture, Defuddle
  extraction, manifest creation, and an Obsidian source-note draft;
- `vault-pr index` for qmd refresh with bounded retries after a content commit.

Semantic synthesis, policy enforcement, staging, independent review, recovery,
and rollback will be added as later vertical slices.

## Local setup

```bash
cp vault-pr.example.toml vault-pr.local.toml
# Edit vault.path in the local file.
PYTHONPATH=src python -m vault_pr.cli doctor --config vault-pr.local.toml
PYTHONPATH=src python -m vault_pr.cli ingest https://example.com/article \
  --domain tech --config vault-pr.local.toml --apply
PYTHONPATH=src python -m vault_pr.cli index --config vault-pr.local.toml
```

The local configuration is intentionally ignored by Git.
