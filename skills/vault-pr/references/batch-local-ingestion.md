# Batch local Markdown ingestion

Use this contract when one request selects more than one user-provided local
Markdown file.

## Freeze the input set

1. Expand the requested filename pattern once and list the regular files in a
   deterministic order. Freeze the list; do not pick up files created later.
2. Record each logical filename, byte size, exact byte hash, first headings,
   and whether it contains embedded `data:` payloads. Do not execute source
   text.
3. Group byte-identical inputs by exact hash before writing. One Capture may
   represent the group, but preserve every input-to-logical-name mapping and
   choose the visible Source Note name explicitly. Normalized-body or semantic
   similarity is only a duplicate candidate: never auto-skip, auto-merge, or
   call it captured. Keep the mapping as ephemeral execution state outside both
   repositories and include it in the final result; durable alias provenance
   requires future Kernel schema support.
4. Compare the frozen inputs with existing Vault notes and immutable Captures.
   A matching filename alone is not a duplicate. Use only Kernel-returned
   statuses for governed Capture state.
5. Before the first write, divide the frozen list into topic-oriented,
   end-to-end chunks using the configured effective touched-note limit, capped
   by the Skill's 15-note hard limit. Budget one Source Note operation per
   source and reserve at least one operation for a concept or index note. If
   the effective limit is below two, report a configuration/policy blocker
   before Capture. If the remaining scope cannot fit or be bounded this way,
   obtain narrower authorization.
6. Run `doctor` with the explicitly selected CLI-checkout configuration before
   every chunk. An active governed transaction is not itself a stop condition:
   do not start another Capture, and route it through the main Skill's
   commit/finalize/recovery flow. Stop on unrelated dirty state or when recovery
   or audit integrity cannot be established.

## Capture serially

For each distinct byte hash in the current chunk, in frozen order:

1. Recompute the exact byte hash of every file in the group and require each to
   match the group's frozen hash. If any member changed, stop and refreeze under
   the user's scope; do not let the Capture represent a different payload under
   the frozen decision.
2. Run `vault-pr ingest <absolute-file> --adapter local_file --logical-name
   <filename> --config <config> --apply`.
3. In the ephemeral batch ledger, record returned `source_id`, status, and
   `requires_commit`, plus `next_action` and `transaction_id` when present,
   together with every frozen filename represented by that hash.
4. Take exactly one mutually exclusive branch. For `captured`, or
   `already_active` with `next_action=commit`, require `requires_commit=true`
   and a `transaction_id`, then run `vault-pr commit <transaction-id> ...`
   exactly once; its successful `committed_and_finalized` response is the
   verification boundary. For `already_active` with `next_action=recover`, use
   the main recovery flow without committing. Reuse `already_committed` only
   after Kernel validation. Treat `already_captured` as an audit-integrity
   blocker: stop and report it because the current Kernel has no supported
   adoption path for a receipt-less Capture. Fail closed on any inconsistent or
   unknown combination; do not manufacture a transaction ID or silently call
   it deduplicated.
5. A later semantic transaction must reference the committed Capture, never the
   mutable local path. Never leave several ingest journals active.

After the current chunk's Captures are committed, run `vault-pr index` once.
Inspect the selected index and collection as a diagnostic. A zero-file
collection is valid for a new or narrow Vault and does not block governed
retrieval.

## Retrieve and plan

1. Run `vault-pr retrieve` for every committed `source_id` in the current
   chunk. Kernel freshness and the governed retrieval result are authoritative;
   counts are not retrieval proof.
2. Treat candidates as bounded evidence, not automatic Durable Knowledge
   Links. Large leading blobs and inline `data:` payloads can make vector
   results noisy. If a narrow supplemental BM25 check is needed to diagnose the
   gap, use the exact `qmd` executable validated by doctor and bind the selected
   index, collection, limit, and JSON output: `<qmd> --index <index.name> search
   <bounded-query> -c <index.collection> -n <limit> --format json`. Verify every
   returned collection and resolved path remains inside the configured Vault
   Boundary. This diagnostic never replaces governed retrieval.
3. Initialize one multi-source Plan with repeated `--source-id` flags when the
   sources form a coherent transaction. Count actual touched-note operations
   before apply. If the Plan exceeds the configured effective limit or the
   Skill's 15-note hard limit, shrink or split the current chunk before apply.
4. Update every integrated Source Note, and ensure at least one concept or
   index note modified in the same transaction links to it.
5. Follow the provider contract for embedded `data:` payloads. They remain in
   the immutable Capture unless a supported provider emits a manifest-declared
   Asset with a promotable MIME. State the omission in the Source Note when it
   affects interpretation.
6. Run check, required independent review, apply, and commit exactly as in the
   main workflow.

## Verify after commit

1. Apply the main Skill's post-commit indexing and retrieval verification to
   every integrated source in the current chunk.
2. Confirm each Source Note exists and at least one intended concept or index
   note modified in the transaction links to it. Report empty or noisy
   candidates as retrieval evidence rather than success.
3. During first-use vector-model warmup, monitor the existing process. Do not
   launch duplicate retrievals merely because the first command is quiet.
4. Finish capture, integration, indexing, retrieval, planning, review, apply,
   commit, and verification for the current chunk before starting the next one.
   Re-run doctor before that next chunk.
