# Generate review excerpts from source focus ranges

A Semantic Plan may attach `focus_ranges` to a Source Capture Evidence item. Each
range uses one-based inclusive `start_line` and `end_line` coordinates in the
Capture's hash-bound extracted Markdown. When present, `focus_ranges` must be a
non-empty array and every range must exist within that artifact. The Vault
Maintainer selects ranges containing evidence important to the current
transaction.

The field remains optional in the schema. When it is absent, the Governance
Kernel includes the complete extracted Markdown if it fits the per-source full
text budget and rejects the Plan otherwise. Thus short sources need no range
annotation, while long sources cannot silently fall back to a truncated prefix.

The Governance Kernel deterministically extracts each focused excerpt with
bounded surrounding context and includes it in the digest-bound Review Packet.
The Semantic Reviewer may inspect outside the selected ranges, including the full
Capture and original primary artifact, to find missing qualifications or
conflicting evidence. Existing Source Note references remain the durable
provenance visible in Vault notes; no block identity, Knowledge Basis, Vault byte
locator, Receipt mapping, or proposed-content dependency graph is introduced.

The Kernel guarantees write safety, valid references, and reproducible evidence.
The Vault Maintainer and Semantic Reviewer remain responsible for semantic
correctness; the Kernel does not construct a semantic proof system.
