# Use one executable Plan schema before 1.0

Plan schema v4 is the only schema Vault PR creates, checks, reviews, or applies.
Upgrading to the v4 runtime requires that the target Vault have no active
transaction; unfinished v2 or v3 Plans must be completed or recovered with the
older runtime first. The v4 implementation removes the v2/v3 execution branches
instead of carrying forward the retired autonomous-repair protocol.

Committed Transaction Receipts and Transaction Archives remain readable as
historical evidence and are not rewritten. Plans are private, short-lived staging
artifacts rather than durable Vault knowledge, so this pre-1.0 compatibility break
is preferable to maintaining several meanings and validation paths indefinitely.
