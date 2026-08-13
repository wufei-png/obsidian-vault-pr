# Treat fifteen notes as a review threshold

Semantic Transactions touch at most 15 notes by default, but 15 is not a
Governance Kernel hard limit. A Vault owner may configure a higher positive
`semantic.max_touched_notes` value in local policy; any transaction touching
more than 15 notes requires independent semantic review regardless of the
Vault's ordinary review policy.

This preserves a conservative default accident radius while allowing high-fanout
renames and global terminology migrations to remain atomic. Vault PR will not add
a separate bulk-maintenance mode: existing Plan size limits, exact-path checks,
Review Packet binding, and journaled commit semantics continue to govern the
larger transaction.
