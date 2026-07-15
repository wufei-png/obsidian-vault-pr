# Agent packet contract

Role packets are bounded JSON objects serialized with sorted keys when hashed.
Input packets use `quality_assessment_input` or `semantic_review_input` and bind
only the task evidence. Role output packets use `maintainer_result`,
`quality_assessment`, or `semantic_review` and additionally identify the fresh
instance and its closed-vocabulary decision.

Every input packet contains:

```json
{
  "schema_version": 1,
  "packet_type": "quality_assessment_input | semantic_review_input",
  "packet_id": "pkt_<sha256-prefix>",
  "created_at": "RFC3339 timestamp",
  "input_sha256": "64 lowercase hexadecimal characters",
  "payload": {
    "evidence": [],
    "unassessed": []
  }
}
```

`input_sha256` is the SHA-256 of canonical JSON for `payload` alone. The packet
ID is derived from the same digest, so a receiver can recompute both using only
the packet it received.

Every output packet repeats `packet_id` and `input_sha256`, sets its role-specific
output `packet_type`, and adds `instance_id` plus `status`.

`packet_id` is the first 20 hexadecimal characters of the SHA-256 digest of the
canonical packet input, prefixed with `pkt_`. Evidence entries use Vault-relative
paths, case IDs, source IDs, content hashes, or metric IDs; they never contain
credentials, full private source text, absolute paths, or hidden conversation.

The orchestrator rejects unknown schema versions, packet types, status values,
missing hashes, duplicate evidence identities, absolute or escaping paths, and a
review packet whose content hashes differ from the plan being decided. Packet
validation proves evidence binding, not that an agent instance was truly
independent; the orchestrator must create fresh Quality Assessor and Semantic
Reviewer contexts without inherited conversation.
