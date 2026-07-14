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

## Current capabilities

The implemented workflow provides:

- `vault-pr doctor` for Git safety and declarative qmd drift checks;
- URL ingestion through Defuddle, direct Markdown, PDF, WeChat, or an
  authenticated browser snapshot;
- domain-scoped BM25/vector retrieval for deduplication and semantic planning;
- hash-bound change plans with a 15-note default limit;
- independent reviewer approval for merge, delete, contradiction, and work
  changes;
- deterministic plan apply with rollback-on-error and an audit archive;
- qmd collection reconciliation, update, and incremental embedding with at most
  three retries after the first attempt.

## Local setup

```bash
cp vault-pr.example.toml vault-pr.local.toml
# Edit vault.path in the local file.
uv sync
uv run vault-pr doctor --config vault-pr.local.toml
uv run vault-pr ingest https://example.com/article \
  --domain tech --config vault-pr.local.toml --apply
uv run vault-pr index --config vault-pr.local.toml
uv run vault-pr retrieve src_0123456789abcdef0123 \
  --config vault-pr.local.toml
uv run vault-pr plan init src_0123456789abcdef0123 \
  --summary "Integrate the source" --config vault-pr.local.toml
```

The local configuration is intentionally ignored by Git.

Use `vault-pr index --check` for a read-only collection drift report. A normal
`vault-pr index` repairs only explicitly configured collections, leaves all
unmanaged collections untouched, and then refreshes lexical and vector indexes.
If `doctor` reports an interrupted semantic transaction, run
`vault-pr recover --config vault-pr.local.toml`; it rolls back only after journal,
backup, file-state, and Git-state verification succeeds.
