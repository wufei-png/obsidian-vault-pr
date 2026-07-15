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
- URL ingestion through pinned HTTP plus local Defuddle extraction, direct
  Markdown, PDF, or an agent-selected Capture Provider for WeChat and
  authenticated pages, with bounded snapshot validation, extraction time, PDF
  pages, extracted text, and PDF-worker memory;
- vault-wide BM25/vector retrieval for deduplication and semantic planning;
- hash-bound change plans with a 15-note default limit;
- hash-bound reviewer records for merge, delete, contradiction, or a vault-wide
  review-all policy;
  the skill orchestration assigns these plans to a separate reviewer agent;
- deterministic plan apply with rollback-on-error, an audit archive, and a
  durable `apply -> commit -> finalize` recovery protocol;
- qmd collection reconciliation, update, and incremental embedding with at most
  three retries after the first attempt.

## Capability boundary

The CLI is a deterministic Governance Kernel, not a registry of agent skills.
It directly handles ordinary public HTTP sources and accepts untrusted local
Snapshot Bundles for specialized sources. The agent skill may compose optional
Capture Providers such as `wechat-article-to-markdown`, but the Python package
does not depend on those skills or their packages. See
[the Capture Provider decision](docs/adr/0001-compose-specialized-capture-providers.md)
and [the provider contract](skills/vault-pr/references/capture-providers.md).

`defuddle` and `qmd` are external executables checked by `vault-pr doctor`;
`pypdf` and PyYAML are Python package dependencies. The `defuddle` agent skill is
not a CLI dependency.

## Local setup

```bash
cp vault-pr.example.toml vault-pr.local.toml
# Edit vault.path in the local file.
uv sync
uv run vault-pr doctor --config vault-pr.local.toml
uv run vault-pr ingest https://example.com/article \
  --config vault-pr.local.toml --apply
# Commit only the returned transaction's paths and close its journal under lock.
uv run vault-pr commit txn_ingest_... --message "Ingest source article" \
  --config vault-pr.local.toml
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
An applied ingest or semantic plan returns `requires_commit: true` and keeps its
recovery journal. Run `vault-pr commit <transaction-id> --message <message>
--config vault-pr.local.toml`; it holds the vault lock, stages only journal paths,
verifies staged hashes, commits, and finalizes. Finalization verifies the clean
worktree, new commit, complete commit-path range, every committed file hash, and
Git mode, plus the audit record before removing the journal. Retrieval and plan
validation re-verify the capture identity, complete artifact set, hashes, and
source-note linkage before consuming it. Indexing refuses to run before this
step. `vault-pr finalize` remains available when a commit was completed before a
process interruption.
If a process stops before finalization, run
`vault-pr recover --config vault-pr.local.toml`. Recovery auto-finalizes an
already committed exact result, or rolls back an uncommitted result only after
journal, backup, file-state, and Git-state verification succeeds.

Reviewer independence is an orchestration guarantee, not a local identity or
cryptographic boundary. The CLI prevents stale approval by binding the record to
the exact plan and proposed files; the orchestrator must ensure the planner does
not review its own risky transaction.
