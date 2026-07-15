# Keep three agent roles behind one Vault PR skill

Vault PR exposes one user-facing skill while keeping three internal role
contracts: the main agent acts as Vault Maintainer, fresh read-only Quality
Assessor instances produce findings and evaluation judgments, and a fresh
Semantic Reviewer approves or rejects risk-bearing transactions from an
immutable Review Packet. Role prompts, rubrics, and schemas live with the skill;
the orchestrator starts separate instances, while Capture Providers and the
Governance Kernel remain capabilities rather than agent roles.

## Consequences

The same Semantic Reviewer role may use different risk rubrics, and the same
Quality Assessor role may run discovery or blind evaluation, but an instance may
not approve work it planned or repair a finding it produced. Independence
requires a fresh context and role boundary, not a different model provider.
Rejected plans may be revised by the Vault Maintainer and sent to a fresh
reviewer only a configured, bounded number of times; exhaustion leaves the
finding and unapplied transaction available for later inspection rather than
falling back to user maintenance or self-approval.
