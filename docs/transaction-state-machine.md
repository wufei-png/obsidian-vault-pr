# Transaction state machine

`vault-pr` exposes a small, read-only lifecycle projection over active recovery
journals and committed transaction receipts. It does not infer state from error
text and it does not expose proposed or committed note content.

```text
APPLIED -------------> COMMITTED -------------> FINALIZED
   |                       |
   +----> RECOVERY_REQUIRED <----+
                    |
                    +---- recover ----> exact rollback or FINALIZED
```

The projection intentionally starts at `APPLIED`. Draft and validated Plans are
private staging artifacts, not durable mutation transactions.

| State | Evidence | Allowed actions | Meaning |
| --- | --- | --- | --- |
| `APPLIED` | Active journal, expected bytes in the worktree, base HEAD unchanged | `commit`, `recover` | Mutation reached the worktree but no exact commit is observed. |
| `COMMITTED` | Active journal and exact governed commit observed | `finalize`, `recover` | Commit exists; final receipt and cleanup still need verification. |
| `RECOVERY_REQUIRED` | Prepared journal, or active state that cannot safely continue normally | `recover` | Recovery must verify the journal and either roll back or finalize. |
| `FINALIZED` | Valid transaction receipt committed at HEAD | none | The transaction is closed. |

Inspect state with:

```bash
vault-pr transaction list --config vault-pr.local.toml
vault-pr transaction show <transaction-id> --config vault-pr.local.toml
vault-pr transaction diff <transaction-id> --config vault-pr.local.toml
```

`list` is compact. `show` includes lifecycle evidence and file transitions.
`diff` returns only paths, SHA-256 values, and Git modes. A finalized receipt
cannot reconstruct a pre-change byte hash for every data path, so that absence
is explicit instead of guessed.

These commands are observational. State changes still use `vault-pr commit`,
`vault-pr finalize`, or `vault-pr recover`, all serialized by the Vault lock.
There is deliberately no generic `resume` or `abort`: those names hide whether
the safe operation is an exact commit, a verified rollback, or finalization.
