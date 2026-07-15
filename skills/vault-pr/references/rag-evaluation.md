# RAG evaluation cases

Vault-specific cases live at `.vault-pr/quality/rag/cases.jsonl` by default and
are excluded from qmd retrieval. Generic schemas and synthetic fixtures remain in
the skill repository. Start with 10–20 human-accepted real questions; a model may
propose candidates but cannot set `accepted: true` on its own.

Each JSONL record has this shape:

```json
{
  "schema_version": 1,
  "case_id": "rag_unique_case_name",
  "accepted": true,
  "query": "A real question asked of this vault",
  "expected_paths": ["knowledgebase/example.md"],
  "expected_claims": ["The answer must preserve this semantic claim."],
  "expected_provenance": ["knowledgebase/example.md"],
  "thresholds": {
    "min_recall_at_k": 1.0,
    "min_reciprocal_rank": 0.1
  }
}
```

The Vault Maintainer generates answers into a temporary JSONL file before blind
assessment:

```json
{"schema_version":1,"case_id":"rag_unique_case_name","answer":"Bounded answer text","provenance":["knowledgebase/example.md"]}
```

Both expected and generated provenance entries are existing Vault-relative
Markdown paths. They are included in the bounded note evidence and must be Git
tracked.

Run `vault-pr quality rag --answers <temporary-jsonl> --config <config>` and
save its JSON output outside the Vault. The
Governance Kernel scores retrieval, verifies the current qmd index commit,
configuration, and version, binds bounded note excerpts and generated answers,
and creates a hash-bound `quality_assessment_input` packet. Without `--answers`,
the command is retrieval discovery only. A successful run is only an assessment
candidate, never a recorded baseline.

Note evidence is deduplicated into a packet-level catalog. Cases reference
Vault-relative catalog paths instead of repeating excerpts. Generation stops at
500 distinct evidence notes or 2 MB of UTF-8 excerpts, keeping the complete
evaluation below the stricter 8 MB acceptance limit.

A fresh Quality Assessor judges answer claims and provenance without requiring
exact wording. Save its `quality_assessment` output, then run
`vault-pr quality rag-accept --evaluation <run-json> --assessment
<assessment-json> --apply --config <config>`. Only a passed, fully assessed,
hash-matched packet whose commit, qmd index, cases, and note evidence are still
current creates a recoverable Quality Record transaction under
`.vault-pr/quality/rag/baselines/`. Finalize it with `vault-pr commit`; only that
Git-tracked record is an Evaluation Baseline. Dirty-vault, stale-index,
untracked-case, or untracked-evidence runs remain diagnostic and return
`assessment_candidate_eligible: false`.
