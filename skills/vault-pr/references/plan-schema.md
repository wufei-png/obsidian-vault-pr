# Semantic change plan

Keep plans and proposed files in the staging directory returned by
`vault-pr plan init`. The directory is under the vault Git directory, not the
worktree.

```json
{
  "schema_version": 1,
  "transaction_id": "txn_20260715T000000Z_example",
  "source_id": "src_0123456789abcdef0123",
  "summary": "Integrate the source into the existing RAG concept page",
  "intent": "integrate",
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
      "action": "create",
      "path": "knowledgebase/ai/agent/New concept.md",
      "content_file": "files/new-concept.md"
    }
  ]
}
```

Every plan must update the exact visible source note named by the capture
manifest's `artifacts.source_note`. Replace its captured placeholders with the
final summary, key claims, provenance, and links to the integrated concept notes.

Use `create`, `update`, `delete`, or `rename`. Add `from` and the source hash for
rename. Add the current file hash for update, delete, and rename. Proposed create
and update files must contain valid Obsidian frontmatter.

Set `intent` to `merge`, `split`, or `rename` when applicable. Set
`contradiction` to true when changing the current synthesis because sources
conflict. Merge, delete, contradiction, and work-domain plans require an
independent hash-bound approval. Any plan edit invalidates an earlier approval.
Approval covers both `plan.json` and every referenced proposed content file;
editing either after review invalidates the approval.
