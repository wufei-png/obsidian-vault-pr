# Record findings only for autonomous repair

Quality Findings may remain transient diagnostics and do not need to be committed merely because they were observed. A finding must first be preserved in a Git-tracked Quality Record when an agent will use it as Transaction Evidence for autonomous repair; the transaction separately records the current Vault-level Autonomous Repair Policy in its Write Authorization Snapshot. This keeps routine assessment lightweight while making self-initiated writes auditable and preventing evidence from silently granting authority.
