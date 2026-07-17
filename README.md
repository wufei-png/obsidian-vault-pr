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
- Capture schema v3 ingestion through pinned HTTP, a Kernel-verified local
  file, or a manifest-driven Snapshot Bundle for WeChat and authenticated
  pages; local absolute paths and provider metadata do not define identity;
- vault-wide BM25/vector retrieval for deduplication and semantic planning;
- Plan schema v3 with one Write Authorization Snapshot, multiple Evidence
  items, explicit retrieval requirements, governed Source Capture Asset
  promotion, and a 15-note default limit; existing v2 Plans remain readable;
- deterministic Review Packets and structured assessments for merge, delete,
  contradiction, `purge_capture`, autonomous repair, or review-all policy;
  the skill orchestration assigns these plans to a separate reviewer agent;
- deterministic plan apply with rollback-on-error, an audit archive, and a
  durable `apply -> commit -> finalize` recovery protocol;
- qmd collection reconciliation, capability-gated update, exact-HEAD freshness,
  and incremental embedding with at most three retries after the first attempt;
- disposable live capture-route canaries for HTML, PDF, provider-assisted
  WeChat, and environment-gated authenticated snapshots;
- read-only Note Role graph audits and vault-owned RAG evaluation cases with
  deterministic retrieval metrics and hash-bound blind-RAG assessor packets;
- formal Vault Maintainer, Quality Assessor, and Semantic Reviewer contracts
  behind one user-facing skill.

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
uv run vault-pr ingest ./reference.pdf --adapter local_file \
  --logical-name "Reference PDF" --config vault-pr.local.toml --apply
# Commit only the returned transaction's paths and close its journal under lock.
uv run vault-pr commit txn_ingest_... --message "Ingest source article" \
  --config vault-pr.local.toml
uv run vault-pr migrate captures-v3 \
  --config vault-pr.local.toml
uv run vault-pr index --config vault-pr.local.toml
uv run vault-pr retrieve src_0123456789abcdef0123 \
  --config vault-pr.local.toml
uv run vault-pr plan init --source-id src_0123456789abcdef0123 \
  --summary "Integrate the source" \
  --authorization-summary "Integrate the captured source" \
  --config vault-pr.local.toml
uv run vault-pr canary https://www.rfc-editor.org/rfc/rfc9110.html \
  --adapter http
uv run vault-pr quality graph --config vault-pr.local.toml
uv run vault-pr quality rag --answers /tmp/vault-pr-rag-answers.jsonl \
  --config vault-pr.local.toml > /tmp/vault-pr-rag-evaluation.json
uv run vault-pr quality rag-accept \
  --evaluation /tmp/vault-pr-rag-evaluation.json \
  --assessment /tmp/vault-pr-rag-assessment.json --apply \
  --config vault-pr.local.toml
```

The local configuration is intentionally ignored by Git.

Use `vault-pr migrate captures-v3` to inspect schema-v2 Capture upgrades without
writing. A new `--apply` starts only on a clean Vault; an exact retry may instead
return the existing transaction and its required `commit` or `recover` action.
Runtime consumers reject Capture v1/v2; the explicit migration validates the
old tree, derives the new content-bound identity, rewrites exact source-ID
tokens, and leaves a recoverable transaction finalized with `vault-pr commit`.

`vault-pr canary` uses a disposable Git and qmd fixture and never writes the
configured user vault. It validates capture, exact-path commit, indexing, and
retrieval, then removes the fixture collection; semantic integration remains a
separate agent judgment. User-supplied retention URLs always go through the real
configured vault workflow instead of this disposable command.

`vault-pr quality graph` reports durable-link findings by Note Role without
writing. `vault-pr quality rag` reads human-accepted cases from
`.vault-pr/quality/rag/cases.jsonl`, scores expected retrieval, and binds a
temporary JSONL answer file plus bounded note excerpts into the packet a fresh
Quality Assessor uses to judge claims and provenance. Omitting `--answers`
creates discovery-only diagnostic evidence. A passed assessor output must be
recorded with `quality rag-accept` and finalized with `vault-pr commit`; only
that Git-tracked Quality Record is a baseline. Runs on a dirty worktree remain
diagnostic and return `assessment_candidate_eligible: false`.

Use `vault-pr index --check` for a read-only collection drift report. A normal
`vault-pr index` repairs only explicitly configured collections, leaves all
unmanaged collections untouched, and then refreshes lexical and vector indexes.
Every journaled mutation uses the same executor and writes a Git-tracked
transaction receipt. A new apply returns its domain status with
`requires_commit: true`. Retrying the exact request returns `already_active`
with `next_action: commit` or `recover`; after finalization it returns
`already_committed`. Reusing a transaction ID with a different request digest
is an integrity error. Run `vault-pr commit <transaction-id> --message <message>
--config vault-pr.local.toml`; it holds the vault lock, stages only journal paths,
verifies staged hashes, commits, and finalizes. Finalization verifies the clean
worktree, new commit, complete commit-path range, every committed file hash, and
Git mode, plus the transaction receipt before removing the journal. Recovery
journals bind to a one-way `vault_id`; they do not persist or print the absolute
Vault path. Retrieval and Plan
validation re-verify Capture identity, exact declared files, hashes, MIME
signatures, and the independently resolved unique Source Note before consuming
it. Retrieval also requires freshness for the exact Vault HEAD, relevant index
configuration, and qmd capability fingerprint. Indexing refuses to run before
commit finalization. `vault-pr finalize` remains available when a commit was
completed before a process interruption.
If a process stops before finalization, run
`vault-pr recover --config vault-pr.local.toml`. Recovery auto-finalizes an
already committed exact result, or rolls back an uncommitted result only after
journal, backup, file-state, and Git-state verification succeeds.

Reviewer independence is an orchestration guarantee, not a local identity or
cryptographic boundary. The CLI regenerates a deterministic Review Packet,
requires a closed structured assessment, and binds it to the exact Plan and
proposed files; the orchestrator must ensure the planner does not review its own
risky transaction.
