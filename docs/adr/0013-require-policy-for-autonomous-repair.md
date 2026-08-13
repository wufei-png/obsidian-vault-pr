---
status: superseded by ADR-0017
---

# Require policy authorization for autonomous repair

A Quality Record never authorizes its own repair. Autonomous repair requires a current Vault-level Autonomous Repair Policy recorded in the transaction's Write Authorization Snapshot and cites the eligible Quality Record as Transaction Evidence; without that policy, the agent may report the finding but must wait for a Maintenance Request before proposing writes.
