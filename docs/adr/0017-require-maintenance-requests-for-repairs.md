# Require maintenance requests for repairs

Vault PR does not perform autonomous repair without a current user request.
Graph Quality Findings remain read-only diagnostics, and every proposed repair
uses a Maintenance Request as its Write Authorization Snapshot. Accepted RAG
evaluation outcomes may still be retained as Quality Records for regression
baselines, but neither findings nor records authorize writes.

The former Autonomous Repair Policy, capability whitelist, and graph-finding
Quality Record path added substantial Plan, policy, review, and persistence
machinery for only two repair rules. If unattended write automation becomes a
demonstrated product requirement, it will need a new explicit design rather than
reusing diagnostic evidence as implicit authority.

The v4 runtime rejects the retired `[autonomous_repair]` configuration table with
an explicit migration error, including from `doctor`; users remove that local
table before upgrading. It is not accepted as a no-op and Vault PR does not add a
one-key configuration migration command. Existing accepted RAG Evaluation
Baselines remain valid, while unfinished older transactions must be completed or
recovered with the older runtime as required by ADR-0020.
