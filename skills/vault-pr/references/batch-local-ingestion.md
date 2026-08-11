# Batch local Markdown ingestion

Use this contract when one request selects more than one user-provided local
Markdown file.

## Freeze the input set

1. Expand the requested filename pattern once and list the regular files in a
   deterministic order.
2. Record each logical filename, byte size, content hash, first headings, and
   whether it contains embedded `data:` payloads. Do not execute source text.
3. Compare exact content with existing Vault notes and Captures before writing.
   A matching filename alone is not a duplicate. When body-equivalent content
   already exists, preserve the user's chosen policy: govern and link the
   existing note, create a separate concept, or skip that source. Never label a
   skipped file `already_captured` unless the Kernel returned that status.
4. Run `doctor` with the explicitly selected CLI-checkout configuration. Stop
   on unrelated dirty state or an active transaction.

## Capture serially

For each retained file:

1. Run `vault-pr ingest <absolute-file> --adapter local_file --logical-name
   <filename> --config <config> --apply`.
2. Record the returned `source_id` and `transaction_id`.
3. Immediately run `vault-pr commit <transaction-id> ...` before starting the
   next file. Never leave several ingest journals active.

After every Capture is committed, run `vault-pr index` once for the batch. A
collection can be aligned while containing zero documents, so inspect `qmd
status --index <index-name>` and require a nonzero document count before
retrieval.

## Retrieve and plan

1. Run `vault-pr retrieve` for every retained `source_id`.
2. Treat candidates as bounded evidence, not automatic Durable Knowledge
   Links. Large leading blobs and inline `data:` payloads can make vector
   results noisy. Use titles and headings for a bounded supplemental BM25 check
   when needed, but do not skip the governed retrieval command.
3. Initialize one multi-source Plan with repeated `--source-id` flags when the
   sources form a coherent transaction. Count every updated Source Note and
   concept/index note toward the 15-note limit; split the batch by topic when
   the bound would be exceeded.
4. Update every integrated Source Note, and ensure at least one concept or
   index note modified in the same transaction links to it.
5. Keep embedded `data:` payloads only in the immutable Capture. If the image
   itself is required evidence, stop for a supported acquisition route that can
   declare it as an Asset; do not relabel the local source. State the omission
   in the Source Note when it affects interpretation.
6. Run check, required independent review, apply, and commit exactly as in the
   main workflow.

## Verify after commit

1. Run `vault-pr index` only after journal finalization.
2. Re-run `vault-pr retrieve` for every integrated source.
3. Verify the expected concept is findable when semantically appropriate. If a
   concept remains absent, report the exact noisy candidates or query-shaping
   limitation instead of treating successful command execution as semantic
   retrieval success.
4. During first-use vector-model warmup, monitor the existing process. Do not
   launch duplicate retrievals merely because the first command is quiet.
