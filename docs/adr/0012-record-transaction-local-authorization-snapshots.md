# Record transaction-local authorization snapshots

Every Semantic Transaction records exactly one immutable Write Authorization Snapshot and may cite multiple items of Transaction Evidence. The snapshot records the user request or current Vault policy asserted by the orchestrator, while evidence supports what should change; the Governance Kernel binds both into validation and review but does not treat the snapshot as an authenticated credential, reusable permission, or independently managed lifecycle object.

This requirement applies to agent-proposed semantic changes, not to every state-writing command. Deterministic capture and archival, migration, evaluation-baseline recording, recovery, and index refresh retain their operation-specific manifests and journals without adopting the authorization schema. A later Semantic Transaction may cite their outputs as evidence.
