# Use one authorization and many evidence items

Semantic Plan v2 records exactly one transaction-local Write Authorization
Snapshot and zero or more Evidence items.

Authorization answers why an agent may propose a write: either an explicit
Maintenance Request or the current Autonomous Repair Policy. Evidence supports
what should change and may include Source Captures or Quality Records. Bounded
retrieval results remain transaction-local Review Packet evidence rather than
persistent Transaction Evidence. Evidence never grants authority. Quality
Findings remain transient unless an eligible deterministic finding will be
used for autonomous repair.

This replaces the overloaded Plan Origin concept, which mixed permission and
provenance. Authorization is an audit assertion supplied by the orchestrator,
not a credential or separate lifecycle object. Independent review remains
required by risk and policy, and autonomous repair remains restricted to the
Kernel capability whitelist.
