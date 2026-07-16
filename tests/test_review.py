from __future__ import annotations

import unittest

from vault_pr.review import (
    REQUIRED_JUDGMENTS,
    ReviewError,
    build_review_packet,
    validate_assessment,
)


class ReviewSchemaTests(unittest.TestCase):
    def test_approval_judgments_must_cite_packet_evidence(self) -> None:
        packet = build_review_packet({"evidence_ids": ["plan"]})
        assessment = {
            "schema_version": 2,
            "packet_type": "semantic_review",
            "packet_id": packet["packet_id"],
            "input_sha256": packet["input_sha256"],
            "instance_id": "reviewer-fixture",
            "status": "approve",
            "judgments": {
                name: {
                    "status": "pass",
                    "reason": "Checked",
                    "evidence_refs": [],
                }
                for name in REQUIRED_JUDGMENTS
            },
            "reasons": [],
            "unassessed": [],
        }

        with self.assertRaisesRegex(ReviewError, "must cite evidence"):
            validate_assessment(
                assessment,
                packet,
                prior_instance_ids=set(),
            )


if __name__ == "__main__":
    unittest.main()
