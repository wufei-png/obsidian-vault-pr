from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from vault_pr.rag_eval import (
    RagEvaluationError,
    accept_rag_baseline,
    evaluate_rag,
)
from vault_pr.recovery import commit_applied_transaction


def _git(vault: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(vault), *args],
        check=True,
        capture_output=True,
        text=True,
    )


class RagEvaluationTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, dict[str, object], Path]:
        vault = root / "vault"
        vault.mkdir()
        _git(vault, "init", "-q")
        _git(vault, "config", "user.name", "Fixture")
        _git(vault, "config", "user.email", "fixture@example.com")
        note = vault / "knowledgebase" / "rag.md"
        note.parent.mkdir()
        note.write_text("# RAG\n\nGrounded retrieval.\n", encoding="utf-8")
        cases = vault / ".vault-pr" / "quality" / "rag" / "cases.jsonl"
        cases.parent.mkdir(parents=True)
        cases.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "case_id": "rag_grounded_retrieval",
                    "accepted": True,
                    "query": "How is retrieval grounded?",
                    "expected_paths": ["knowledgebase/rag.md"],
                    "expected_claims": ["Retrieval is grounded."],
                    "expected_provenance": ["knowledgebase/rag.md"],
                    "thresholds": {
                        "min_recall_at_k": 1.0,
                        "min_reciprocal_rank": 1.0,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        _git(vault, "add", "--all")
        _git(vault, "commit", "-qm", "Seed RAG evaluation fixture")
        config: dict[str, object] = {
            "vault": {"path": str(vault)},
            "index": {"name": "fixture", "collection": "fixture"},
        }
        answers = root / "answers.jsonl"
        answers.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "case_id": "rag_grounded_retrieval",
                    "answer": "Retrieval is grounded.",
                    "provenance": ["knowledgebase/rag.md"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return vault, config, answers

    @staticmethod
    def _index_evidence(eligible: bool = True) -> dict[str, object]:
        return {
            "eligible": eligible,
            "reasons": [] if eligible else ["commit_matches"],
            "git_commit": "fixture",
            "config_sha256": "a" * 64,
            "qmd_version": "qmd fixture",
            "status_sha256": "b" * 64,
            "collection_status": "aligned",
        }

    def test_scores_retrieval_and_builds_assessor_packet(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _vault, config, answers = self._fixture(Path(temp_dir))

            def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps(
                        [
                            {
                                "file": "qmd://fixture/knowledgebase/rag.md",
                                "score": 0.9,
                            }
                        ]
                    ),
                    stderr="",
                )

            result = evaluate_rag(
                config,
                answers_path=answers,
                runner=runner,
                executable="qmd",
                limit=5,
                index_validator=lambda _config, _qmd: self._index_evidence(),
            )

            self.assertTrue(result["assessment_candidate_eligible"])
            self.assertFalse(result["baseline_recorded"])
            self.assertEqual(result["metrics"]["retrieval_passed"], 1)
            self.assertEqual(result["metrics"]["mean_recall_at_k"], 1.0)
            packet = result["quality_assessor_packet"]
            self.assertEqual(packet["payload"]["mode"], "rag_blind")
            self.assertRegex(packet["packet_id"], r"^pkt_[0-9a-f]{20}$")
            self.assertEqual(
                len(packet["payload"]["note_evidence"][0]["sha256"]),
                64,
            )
            self.assertEqual(
                packet["payload"]["cases"][0]["evidence_note_refs"],
                ["knowledgebase/rag.md"],
            )

    def test_rejects_unaccepted_generated_case(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, config, _answers = self._fixture(Path(temp_dir))
            cases = vault / ".vault-pr" / "quality" / "rag" / "cases.jsonl"
            content = json.loads(cases.read_text(encoding="utf-8"))
            content["accepted"] = False
            cases.write_text(json.dumps(content) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(RagEvaluationError, "human-accepted"):
                evaluate_rag(config, runner=lambda *_args, **_kwargs: None, executable="qmd")

    def test_wraps_invalid_qmd_output_as_evaluation_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _vault, config, _answers = self._fixture(Path(temp_dir))

            def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess([], 0, stdout="not json", stderr="")

            with self.assertRaisesRegex(RagEvaluationError, "JSON results"):
                evaluate_rag(config, runner=runner, executable="qmd")

    def test_untracked_case_file_cannot_become_a_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, config, answers = self._fixture(Path(temp_dir))
            cases_relative = ".vault-pr/quality/rag/cases.jsonl"
            _git(vault, "rm", "--cached", "--", cases_relative)
            (vault / ".gitignore").write_text(cases_relative + "\n", encoding="utf-8")
            _git(vault, "add", ".gitignore")
            _git(vault, "commit", "-qm", "Ignore local proposed evaluation cases")

            def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps(
                        [
                            {
                                "file": "qmd://fixture/knowledgebase/rag.md",
                                "score": 0.9,
                            }
                        ]
                    ),
                    stderr="",
                )

            result = evaluate_rag(
                config,
                answers_path=answers,
                runner=runner,
                executable="qmd",
                index_validator=lambda _config, _qmd: self._index_evidence(),
            )

            self.assertTrue(result["git_clean"])
            self.assertFalse(result["assessment_candidate_eligible"])

    def test_deduplicates_note_evidence_across_cases(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, config, answers = self._fixture(root)
            cases = vault / ".vault-pr/quality/rag/cases.jsonl"
            second_case = json.loads(cases.read_text(encoding="utf-8"))
            second_case["case_id"] = "rag_grounded_retrieval_second"
            cases.write_text(
                cases.read_text(encoding="utf-8") + json.dumps(second_case) + "\n",
                encoding="utf-8",
            )
            second_answer = json.loads(answers.read_text(encoding="utf-8"))
            second_answer["case_id"] = "rag_grounded_retrieval_second"
            answers.write_text(
                answers.read_text(encoding="utf-8") + json.dumps(second_answer) + "\n",
                encoding="utf-8",
            )
            _git(vault, "add", "--", str(cases.relative_to(vault)))
            _git(vault, "commit", "-qm", "Add second RAG case")

            def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps(
                        [{"file": "qmd://fixture/knowledgebase/rag.md", "score": 0.9}]
                    ),
                    stderr="",
                )

            result = evaluate_rag(
                config,
                answers_path=answers,
                runner=runner,
                executable="qmd",
                index_validator=lambda _config, _qmd: self._index_evidence(),
            )

            payload = result["quality_assessor_packet"]["payload"]
            self.assertEqual(len(payload["cases"]), 2)
            self.assertEqual(len(payload["note_evidence"]), 1)

    def test_stale_index_cannot_become_a_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config, answers = self._fixture(root)

            def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps(
                        [{"file": "qmd://fixture/knowledgebase/rag.md", "score": 0.9}]
                    ),
                    stderr="",
                )

            stale_evidence = self._index_evidence(False)
            result = evaluate_rag(
                config,
                answers_path=answers,
                runner=runner,
                executable="qmd",
                index_validator=lambda _config, _qmd: stale_evidence,
            )

            self.assertFalse(result["assessment_candidate_eligible"])
            packet = result["quality_assessor_packet"]
            assessment = {
                "schema_version": 1,
                "packet_type": "quality_assessment",
                "packet_id": packet["packet_id"],
                "input_sha256": packet["input_sha256"],
                "instance_id": "assessor-fixture",
                "status": "pass",
                "findings": [],
                "claim_conflicts": [],
                "unassessed": [],
            }
            evaluation_path = root / "evaluation.json"
            assessment_path = root / "assessment.json"
            evaluation_path.write_text(json.dumps(result), encoding="utf-8")
            assessment_path.write_text(json.dumps(assessment), encoding="utf-8")

            with self.assertRaisesRegex(RagEvaluationError, "qmd index evidence"):
                accept_rag_baseline(
                    config,
                    evaluation_path=evaluation_path,
                    assessment_path=assessment_path,
                    executable="qmd",
                    index_validator=lambda _config, _qmd: stale_evidence,
                )

    def test_untracked_expected_note_cannot_become_a_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault, config, answers = self._fixture(Path(temp_dir))
            note_relative = "knowledgebase/rag.md"
            _git(vault, "rm", "--cached", "--", note_relative)
            (vault / ".gitignore").write_text(note_relative + "\n", encoding="utf-8")
            _git(vault, "add", ".gitignore")
            _git(vault, "commit", "-qm", "Ignore local expected note")

            def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps(
                        [{"file": "qmd://fixture/knowledgebase/rag.md", "score": 0.9}]
                    ),
                    stderr="",
                )

            result = evaluate_rag(
                config,
                answers_path=answers,
                runner=runner,
                executable="qmd",
                index_validator=lambda _config, _qmd: self._index_evidence(),
            )

            self.assertTrue(result["git_clean"])
            self.assertFalse(result["expected_paths_tracked"])
            self.assertFalse(result["assessment_candidate_eligible"])

    def test_untracked_provenance_note_cannot_become_a_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, config, answers = self._fixture(root)
            provenance = vault / "knowledgebase" / "provenance.md"
            provenance.write_text("# Provenance\n", encoding="utf-8")
            cases = vault / ".vault-pr/quality/rag/cases.jsonl"
            case = json.loads(cases.read_text(encoding="utf-8"))
            case["expected_provenance"] = ["knowledgebase/provenance.md"]
            cases.write_text(json.dumps(case) + "\n", encoding="utf-8")
            answer = json.loads(answers.read_text(encoding="utf-8"))
            answer["provenance"] = ["knowledgebase/provenance.md"]
            answers.write_text(json.dumps(answer) + "\n", encoding="utf-8")
            _git(vault, "add", "--", ".vault-pr/quality/rag/cases.jsonl")
            _git(vault, "commit", "-qm", "Reference untracked provenance")

            def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps(
                        [{"file": "qmd://fixture/knowledgebase/rag.md", "score": 0.9}]
                    ),
                    stderr="",
                )

            result = evaluate_rag(
                config,
                answers_path=answers,
                runner=runner,
                executable="qmd",
                index_validator=lambda _config, _qmd: self._index_evidence(),
            )

            self.assertFalse(result["provenance_paths_tracked"])
            self.assertFalse(result["assessment_candidate_eligible"])

    def test_records_passed_assessment_as_recoverable_git_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, config, answers = self._fixture(root)

            def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps(
                        [{"file": "qmd://fixture/knowledgebase/rag.md", "score": 0.9}]
                    ),
                    stderr="",
                )

            evidence = self._index_evidence()
            evaluation = evaluate_rag(
                config,
                answers_path=answers,
                runner=runner,
                executable="qmd",
                index_validator=lambda _config, _qmd: evidence,
            )
            packet = evaluation["quality_assessor_packet"]
            assessment = {
                "schema_version": 1,
                "packet_type": "quality_assessment",
                "packet_id": packet["packet_id"],
                "input_sha256": packet["input_sha256"],
                "instance_id": "assessor-fixture",
                "status": "pass",
                "findings": [],
                "claim_conflicts": [],
                "unassessed": [],
            }
            evaluation_path = root / "evaluation.json"
            assessment_path = root / "assessment.json"
            evaluation_path.write_text(json.dumps(evaluation), encoding="utf-8")
            assessment_path.write_text(json.dumps(assessment), encoding="utf-8")

            accepted = accept_rag_baseline(
                config,
                evaluation_path=evaluation_path,
                assessment_path=assessment_path,
                apply=True,
                executable="qmd",
                index_validator=lambda _config, _qmd: evidence,
            )

            self.assertEqual(accepted["status"], "baseline_applied")
            record = vault / accepted["path"]
            self.assertTrue(record.is_file())
            git_dir = Path(
                subprocess.run(
                    ["git", "-C", str(vault), "rev-parse", "--absolute-git-dir"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            )
            committed = commit_applied_transaction(
                vault, git_dir, accepted["transaction_id"], "Record RAG baseline"
            )
            self.assertEqual(committed["status"], "committed_and_finalized")
            self.assertFalse(
                subprocess.run(
                    ["git", "-C", str(vault), "status", "--porcelain=v1"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )

    def test_rejects_tampered_assessor_packet_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config, answers = self._fixture(root)

            def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps(
                        [{"file": "qmd://fixture/knowledgebase/rag.md", "score": 0.9}]
                    ),
                    stderr="",
                )

            evaluation = evaluate_rag(
                config,
                answers_path=answers,
                runner=runner,
                executable="qmd",
                index_validator=lambda _config, _qmd: self._index_evidence(),
            )
            packet = evaluation["quality_assessor_packet"]
            assessment = {
                "schema_version": 1,
                "packet_type": "quality_assessment",
                "packet_id": packet["packet_id"],
                "input_sha256": packet["input_sha256"],
                "instance_id": "assessor-fixture",
                "status": "pass",
                "findings": [],
                "claim_conflicts": [],
                "unassessed": [],
            }
            packet["payload"]["limit"] = 49
            evaluation_path = root / "evaluation.json"
            assessment_path = root / "assessment.json"
            evaluation_path.write_text(json.dumps(evaluation), encoding="utf-8")
            assessment_path.write_text(json.dumps(assessment), encoding="utf-8")

            with self.assertRaisesRegex(RagEvaluationError, "hash validation"):
                accept_rag_baseline(
                    config,
                    evaluation_path=evaluation_path,
                    assessment_path=assessment_path,
                )

    def test_accepts_tracked_cases_override_from_packet_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, config, answers = self._fixture(root)
            default_cases = vault / ".vault-pr/quality/rag/cases.jsonl"
            override = vault / ".vault-pr/quality/rag/alternate.jsonl"
            override.write_bytes(default_cases.read_bytes())
            _git(vault, "add", "--", str(override.relative_to(vault)))
            _git(vault, "commit", "-qm", "Add alternate RAG cases")

            def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps(
                        [{"file": "qmd://fixture/knowledgebase/rag.md", "score": 0.9}]
                    ),
                    stderr="",
                )

            evidence = self._index_evidence()
            evaluation = evaluate_rag(
                config,
                cases_path=override.relative_to(vault),
                answers_path=answers,
                runner=runner,
                executable="qmd",
                index_validator=lambda _config, _qmd: evidence,
            )
            packet = evaluation["quality_assessor_packet"]
            assessment = {
                "schema_version": 1,
                "packet_type": "quality_assessment",
                "packet_id": packet["packet_id"],
                "input_sha256": packet["input_sha256"],
                "instance_id": "assessor-fixture",
                "status": "pass",
                "findings": [],
                "claim_conflicts": [],
                "unassessed": [],
            }
            evaluation_path = root / "evaluation.json"
            assessment_path = root / "assessment.json"
            evaluation_path.write_text(json.dumps(evaluation), encoding="utf-8")
            assessment_path.write_text(json.dumps(assessment), encoding="utf-8")

            accepted = accept_rag_baseline(
                config,
                evaluation_path=evaluation_path,
                assessment_path=assessment_path,
                executable="qmd",
                index_validator=lambda _config, _qmd: evidence,
            )

            self.assertEqual(accepted["status"], "baseline_ready")

    def test_rejects_unsafe_note_evidence_path_before_reading(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config, answers = self._fixture(root)

            def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps(
                        [{"file": "qmd://fixture/knowledgebase/rag.md", "score": 0.9}]
                    ),
                    stderr="",
                )

            evidence = self._index_evidence()
            evaluation = evaluate_rag(
                config,
                answers_path=answers,
                runner=runner,
                executable="qmd",
                index_validator=lambda _config, _qmd: evidence,
            )
            packet = evaluation["quality_assessor_packet"]
            packet["payload"]["note_evidence"][0]["path"] = "/dev/zero"
            digest = hashlib.sha256(
                json.dumps(
                    packet["payload"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            packet["packet_id"] = f"pkt_{digest[:20]}"
            packet["input_sha256"] = digest
            assessment = {
                "schema_version": 1,
                "packet_type": "quality_assessment",
                "packet_id": packet["packet_id"],
                "input_sha256": digest,
                "instance_id": "assessor-fixture",
                "status": "pass",
                "findings": [],
                "claim_conflicts": [],
                "unassessed": [],
            }
            evaluation_path = root / "evaluation.json"
            assessment_path = root / "assessment.json"
            evaluation_path.write_text(json.dumps(evaluation), encoding="utf-8")
            assessment_path.write_text(json.dumps(assessment), encoding="utf-8")

            with self.assertRaisesRegex(RagEvaluationError, "note evidence"):
                accept_rag_baseline(
                    config,
                    evaluation_path=evaluation_path,
                    assessment_path=assessment_path,
                    executable="qmd",
                    index_validator=lambda _config, _qmd: evidence,
                )

    def test_failed_retrieval_threshold_cannot_be_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config, answers = self._fixture(root)

            def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess([], 0, stdout="[]", stderr="")

            evidence = self._index_evidence()
            evaluation = evaluate_rag(
                config,
                answers_path=answers,
                runner=runner,
                executable="qmd",
                index_validator=lambda _config, _qmd: evidence,
            )
            self.assertFalse(evaluation["assessment_candidate_eligible"])
            packet = evaluation["quality_assessor_packet"]
            assessment = {
                "schema_version": 1,
                "packet_type": "quality_assessment",
                "packet_id": packet["packet_id"],
                "input_sha256": packet["input_sha256"],
                "instance_id": "assessor-fixture",
                "status": "pass",
                "findings": [],
                "claim_conflicts": [],
                "unassessed": [],
            }
            evaluation_path = root / "evaluation.json"
            assessment_path = root / "assessment.json"
            evaluation_path.write_text(json.dumps(evaluation), encoding="utf-8")
            assessment_path.write_text(json.dumps(assessment), encoding="utf-8")

            with self.assertRaisesRegex(RagEvaluationError, "thresholds"):
                accept_rag_baseline(
                    config,
                    evaluation_path=evaluation_path,
                    assessment_path=assessment_path,
                    executable="qmd",
                    index_validator=lambda _config, _qmd: evidence,
                )


if __name__ == "__main__":
    unittest.main()
