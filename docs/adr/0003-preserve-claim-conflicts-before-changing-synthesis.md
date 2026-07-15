# Preserve Claim Conflicts before changing current synthesis

Vault PR records conflicting provenance-bound claims without automatically
selecting a winner. Detecting a Claim Conflict is read-only, while a Semantic
Transaction that changes the current synthesis remains a contradiction-risk
operation requiring independent review. This preserves historical evidence and
prevents recency or source-ranking heuristics from silently rewriting knowledge.
