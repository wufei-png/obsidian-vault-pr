# Assess graph quality by Note Role

Vault PR applies graph and provenance invariants by Note Role rather than
requiring every Markdown file to have both incoming and outgoing links. Concept
and topic notes are assessed for durable knowledge relationships, source notes
for provenance-to-synthesis connections, and index notes for coverage; assets,
archives, and operational files are excluded from knowledge-graph metrics.

Transaction-local integrity failures such as broken provenance, invalid durable
links, or missing required source-note connections block that transaction.
Vault-wide measures such as orphan rate, coverage, and component shape produce
read-only Quality Findings instead of blocking unrelated ingestion; the Vault
Maintainer may turn those findings into a later bounded Semantic Transaction.
