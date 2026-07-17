# Semantic change plan v3

Keep the Plan and proposed files in the private staging directory returned by
`vault-pr plan init`. Runtime creates schema v3 Plans and continues to accept
schema v2 Plans that do not use Asset Promotion.

```json
{
  "schema_version": 3,
  "transaction_id": "txn_20260715T000000Z_example",
  "summary": "Integrate the source into the existing RAG concept page",
  "authorization": {
    "kind": "maintenance_request",
    "summary": "Integrate the captured source"
  },
  "evidence": [
    {
      "kind": "source_capture",
      "source_id": "src_0123456789abcdef0123"
    }
  ],
  "intent": "integrate",
  "retrieval": {
    "required": true
  },
  "contradiction": false,
  "operations": [
    {
      "action": "update",
      "path": "knowledgebase/sources/Source article--01234567.md",
      "expected_sha256": "sha256-of-current-source-note",
      "content_file": "files/source-note.md"
    },
    {
      "action": "update",
      "path": "knowledgebase/ai/agent/rag.md",
      "expected_sha256": "sha256-of-current-note",
      "content_file": "files/rag.md"
    },
    {
      "action": "promote_asset",
      "source_id": "src_0123456789abcdef0123",
      "source_path": "images/retrieval-flow.png",
      "path": "attachments/retrieval-flow--89abcdef0123.png"
    }
  ]
}
```

Every Plan contains exactly one Write Authorization Snapshot and zero or more
Evidence items. Authorization records why a write may be proposed; Evidence
supports what should change. A Maintenance Request may have no Evidence.
Integration requires Source Capture Evidence. Autonomous repair requires
eligible Quality Record Evidence and the Kernel-generated current policy
snapshot. Retrieval results are bounded Review Packet inputs, not persistent
Transaction Evidence. The current deterministic capabilities may update only
selected finding-owner notes and may create at most one note per selected
finding when the resulting owner note actually references that new note. No
current capability authorizes rename; the global action vocabulary reserves it
for a future capability with an explicit path scope.

Source Capture manifests do not own a Source Note path. The Kernel resolves the
unique current Source Note by exact `source_id`; every integrated Capture's
Source Note must be updated and preserve that ID. For each integrated Capture,
at least one concept or index note modified by the same transaction must contain
a resolved wikilink to that Source Note. Updating only Source Notes is not a
complete integration.

Retrieval-dependent Plans declare `{"required": true}` and require an index
fresh for the exact Vault HEAD, relevant index configuration, and qmd
capabilities. Only a Maintenance Request may use
`{"required": false, "reason": "..."}`.

Normal Markdown operations are `create`, `update`, `delete`, and `rename`.
Each action has a closed field set; unknown metadata is rejected rather than
silently ignored.
`promote_asset` is a schema-v3 operation containing exactly `source_id`,
`source_path`, and `path`. The source must be a manifest-declared `asset` from
Source Capture Evidence in the same Plan; `supplement` entries are not
promotable. The v1 promotion MIME set is PNG, JPEG, GIF, WebP, and PDF. The
target is a flat file under `[assets].directory` (default `attachments/`) with a
MIME-compatible extension and a readable name ending in
`--<first-12-sha256>`. The operation must be referenced by visible Markdown
modified in the same transaction. Equal content reuses an existing tracked
Asset Promotion target with that hash marker; different content is never
allowed to overwrite it. Ordinary pre-existing attachments remain outside this
managed naming and deduplication contract. The transaction-wide promotion count
and declared bytes use the configured `sources.max_asset_files` and
`sources.max_asset_bytes` bounds.
`purge_capture` is a separate high-risk operation containing only `source_id`.
It requires a Maintenance Request and independent review, expands to deletion
of the verified Capture and unique Source Note, and is blocked by remaining
source-ID, wikilink, or `supersedes` references. Capture binaries are not copied
into the Transaction Archive.

Merge, split, delete, contradiction, purge, and autonomous-repair transactions
always require independent review even when the Vault uses risk-based review
for ordinary maintenance.

The Kernel validates frontmatter, newly introduced wikilink and anchor defects,
tracked attachments, referenced Bases, provenance boundaries, exact hashes,
note limits, and autonomous-repair postconditions. Asset Promotion may only
create or exactly reuse a Vault Asset; updates, renames, and deletes remain
outside this contract. Anchor existence is checked only when the target note is
modified by the transaction; links to unmodified notes still require an
unambiguous existing file. Visible notes must not directly embed hidden
`.vault-pr` Capture assets; the agent rewrites such references to an explicit
promotion target before validation.

`vault-pr plan check` deterministically regenerates the Review Packet, including
the full proposed Markdown up to the packet limit. A reviewer returns a closed
structured assessment. Record it with:

```text
vault-pr review <plan> --assessment <assessment.json> --config <config>
```

Any Plan, proposed content, policy, Evidence, or relevant validation change
invalidates the assessment. `plan apply` leaves a journaled transaction that
must be finalized with `vault-pr commit`. An exact retry returns
`already_active` with the next `commit` or `recover` action, and a retry after
finalization returns `already_committed`; changing any request-bound Plan,
proposed content, review record, or relevant policy under the same transaction
ID is an integrity error.
