from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from vault_pr.assessor_schema import AssessorSchemaError, validate_rag_assessment
from vault_pr.cli import main
from vault_pr.rag_eval import (
    RagEvaluationError,
    _unwrap_quality_rag_cli_evaluation,
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

    @staticmethod
    def _passed_assessment(packet: dict[str, object]) -> dict[str, object]:
        payload = packet["payload"]
        return {
            "schema_version": 1,
            "packet_type": "quality_assessment",
            "packet_id": packet["packet_id"],
            "input_sha256": packet["input_sha256"],
            "instance_id": "assessor-fixture",
            "mode": "rag_blind",
            "status": "pass",
            "case_judgments": [
                {
                    "case_id": case["case_id"],
                    "answer_claims": {
                        "decision": "pass",
                        "reason": "Claims are supported",
                        "evidence_refs": [
                            f"case:{case['case_id']}",
                            *[
                                f"note:{path}"
                                for path in case["evidence_note_refs"]
                            ],
                        ],
                    },
                    "provenance": {
                        "decision": "pass",
                        "reason": "Provenance is sufficient",
                        "evidence_refs": [
                            f"case:{case['case_id']}",
                            *[
                                f"note:{path}"
                                for path in case["evidence_note_refs"]
                            ],
                        ],
                    },
                }
                for case in payload["cases"]
            ],
            "findings": [],
            "claim_conflicts": [],
            "unassessed": [],
        }

    @staticmethod
    def _rehash_packet(packet: dict[str, object]) -> None:
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

    def test_passed_judgment_requires_reason_and_evidence(self) -> None:
        packet = {
            "payload": {
                "mode": "rag_blind",
                "cases": [
                    {
                        "case_id": "case_one",
                        "evidence_note_refs": ["knowledgebase/one.md"],
                    }
                ],
                "note_evidence": [{"path": "knowledgebase/one.md"}],
            }
        }
        assessment = {
            "schema_version": 1,
            "packet_type": "quality_assessment",
            "packet_id": "packet_fixture",
            "input_sha256": "a" * 64,
            "instance_id": "assessor_fixture",
            "mode": "rag_blind",
            "status": "pass",
            "case_judgments": [
                {
                    "case_id": "case_one",
                    "answer_claims": {
                        "decision": "pass",
                        "reason": None,
                        "evidence_refs": [],
                    },
                    "provenance": {
                        "decision": "pass",
                        "reason": "Checked",
                        "evidence_refs": [
                            "case:case_one",
                            "note:knowledgebase/one.md",
                        ],
                    },
                }
            ],
            "findings": [],
            "claim_conflicts": [],
            "unassessed": [],
        }

        with self.assertRaisesRegex(
            AssessorSchemaError, "requires a reason and evidence"
        ):
            validate_rag_assessment(packet, assessment)

    def test_case_judgment_cannot_cite_another_cases_note(self) -> None:
        packet = {
            "payload": {
                "mode": "rag_blind",
                "cases": [
                    {
                        "case_id": "case_one",
                        "evidence_note_refs": ["knowledgebase/one.md"],
                    },
                    {
                        "case_id": "case_two",
                        "evidence_note_refs": ["knowledgebase/two.md"],
                    },
                ],
                "note_evidence": [
                    {"path": "knowledgebase/one.md"},
                    {"path": "knowledgebase/two.md"},
                ],
            }
        }
        assessment = {
            "schema_version": 1,
            "packet_type": "quality_assessment",
            "packet_id": "packet_fixture",
            "input_sha256": "a" * 64,
            "instance_id": "assessor_fixture",
            "mode": "rag_blind",
            "status": "pass",
            "case_judgments": [
                {
                    "case_id": case_id,
                    "answer_claims": {
                        "decision": "pass",
                        "reason": "Checked",
                        "evidence_refs": refs,
                    },
                    "provenance": {
                        "decision": "pass",
                        "reason": "Checked",
                        "evidence_refs": refs,
                    },
                }
                for case_id, refs in [
                    (
                        "case_one",
                        ["case:case_one", "note:knowledgebase/two.md"],
                    ),
                    (
                        "case_two",
                        ["case:case_two", "note:knowledgebase/two.md"],
                    ),
                ]
            ],
            "findings": [],
            "claim_conflicts": [],
            "unassessed": [],
        }

        with self.assertRaisesRegex(
            AssessorSchemaError, "references unknown evidence"
        ):
            validate_rag_assessment(packet, assessment)

    def test_scores_retrieval_and_builds_assessor_packet(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _vault, config, answers = self._fixture(Path(temp_dir))
            commands: list[list[str]] = []

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                return subprocess.CompletedProcess(
                    command,
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
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0][3], "query")
            self.assertTrue(commands[0][4].startswith("lex: "))
            self.assertIn("\nvec: ", commands[0][4])
            self.assertIn("--no-rerank", commands[0])
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

    def test_lexical_rag_evaluation_uses_bm25_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _vault, config, answers = self._fixture(Path(temp_dir))
            config["index"]["retrieval_mode"] = "lexical"
            commands: list[list[str]] = []

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        [{"file": "qmd://fixture/knowledgebase/rag.md", "score": 12.0}]
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

            self.assertEqual(result["metrics"]["retrieval_passed"], 1)
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0][3], "search")
            self.assertNotIn("--no-rerank", commands[0])

    def test_validates_qmd_snapshot_once_under_governance_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _vault, config, answers = self._fixture(Path(temp_dir))

            def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps(
                        [{"file": "qmd://fixture/knowledgebase/rag.md", "score": 0.9}]
                    ),
                    stderr="",
                )

            calls = 0

            def validate_once(
                _config: dict[str, object], _qmd: str
            ) -> dict[str, object]:
                nonlocal calls
                calls += 1
                return self._index_evidence()

            evaluate_rag(
                config,
                answers_path=answers,
                runner=runner,
                executable="qmd",
                index_validator=validate_once,
            )

            self.assertEqual(calls, 1)

    def test_discovery_run_has_no_assessor_packet(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _vault, config, _answers = self._fixture(Path(temp_dir))

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
                runner=runner,
                executable="qmd",
                index_validator=lambda _config, _qmd: self._index_evidence(),
            )

            self.assertFalse(result["semantic_assessment_ready"])
            self.assertNotIn("quality_assessor_packet", result)
            diagnostic = result["rag_discovery"]
            self.assertEqual(diagnostic["diagnostic_type"], "rag_discovery")
            self.assertEqual(diagnostic["payload"]["mode"], "rag_discovery")
            self.assertEqual(len(diagnostic["payload"]["cases"]), 1)

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
            assessment = self._passed_assessment(packet)
            evaluation_path = root / "evaluation.json"
            assessment_path = root / "assessment.json"
            evaluation_path.write_text(json.dumps(result), encoding="utf-8")
            assessment_path.write_text(json.dumps(assessment), encoding="utf-8")

            with self.assertRaisesRegex(RagEvaluationError, "not eligible"):
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
            assessment = self._passed_assessment(packet)
            evaluation_path = root / "evaluation.json"
            assessment_path = root / "assessment.json"
            output = io.StringIO()
            with (
                patch("vault_pr.cli.load_config", return_value=config),
                patch("vault_pr.cli.evaluate_rag", return_value=evaluation),
                redirect_stdout(output),
            ):
                status = main(
                    [
                        "quality",
                        "rag",
                        "--answers",
                        str(answers),
                        "--config",
                        str(root / "unused.toml"),
                    ]
                )
            self.assertEqual(status, 0)
            evaluation_content = output.getvalue()
            evaluation_path.write_text(evaluation_content, encoding="utf-8")
            assessment_path.write_text(json.dumps(assessment), encoding="utf-8")

            accepted = accept_rag_baseline(
                config,
                evaluation_path=evaluation_path,
                assessment_path=assessment_path,
                apply=True,
                executable="qmd",
                index_validator=lambda _config, _qmd: evidence,
            )
            active_retry = accept_rag_baseline(
                config,
                evaluation_path=evaluation_path,
                assessment_path=assessment_path,
                apply=True,
                executable="qmd",
                index_validator=lambda _config, _qmd: evidence,
            )

            self.assertEqual(accepted["status"], "baseline_applied")
            self.assertEqual(active_retry["status"], "already_active")
            self.assertEqual(
                active_retry["transaction_id"], accepted["transaction_id"]
            )
            self.assertEqual(active_retry["next_action"], "commit")
            record = vault / accepted["path"]
            self.assertTrue(record.is_file())
            self.assertEqual(
                json.loads(record.read_text(encoding="utf-8"))["schema_version"],
                2,
            )
            baseline = json.loads(record.read_text(encoding="utf-8"))
            self.assertNotIn("evaluation", baseline)
            self.assertEqual(baseline["quality_assessor_packet"], packet)
            self.assertEqual(
                baseline["evaluation_sha256"],
                hashlib.sha256(evaluation_content.encode("utf-8")).hexdigest(),
            )
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
            committed_retry = accept_rag_baseline(
                config,
                evaluation_path=evaluation_path,
                assessment_path=assessment_path,
                apply=True,
                executable="qmd",
                index_validator=lambda _config, _qmd: evidence,
            )
            self.assertEqual(committed_retry["status"], "already_committed")
            self.assertEqual(
                committed_retry["transaction_id"], accepted["transaction_id"]
            )
            self.assertFalse(
                subprocess.run(
                    ["git", "-C", str(vault), "status", "--porcelain=v1"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )

    def test_rejects_malformed_quality_rag_cli_envelopes(self) -> None:
        evaluation = {"schema_version": 2}
        valid = {
            "protocol_version": 1,
            "command": "quality.rag",
            "status": "ok",
            "data": evaluation,
        }
        malformed = [
            {**valid, "protocol_version": True},
            {**valid, "protocol_version": 2},
            {**valid, "command": "quality.graph"},
            {**valid, "status": "assessment_ready"},
            {**valid, "extra": "unexpected"},
            {key: value for key, value in valid.items() if key != "status"},
            {**valid, "data": []},
        ]

        self.assertIs(
            _unwrap_quality_rag_cli_evaluation(valid),
            evaluation,
        )
        for envelope in malformed:
            with self.subTest(envelope=envelope):
                with self.assertRaisesRegex(
                    RagEvaluationError,
                    "RAG evaluation CLI envelope is incomplete or inconsistent",
                ):
                    _unwrap_quality_rag_cli_evaluation(envelope)

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
            assessment = self._passed_assessment(packet)
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

    def test_rejects_tampered_assessor_packet_envelope_mode(self) -> None:
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
            assessment = self._passed_assessment(packet)
            packet["mode"] = "rag_discovery"
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

    def test_rejects_unknown_assessor_packet_envelope_field(self) -> None:
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
            assessment = self._passed_assessment(packet)
            packet["unexpected"] = True
            evaluation_path = root / "evaluation.json"
            assessment_path = root / "assessment.json"
            evaluation_path.write_text(json.dumps(evaluation), encoding="utf-8")
            assessment_path.write_text(json.dumps(assessment), encoding="utf-8")

            with self.assertRaisesRegex(
                RagEvaluationError,
                "no Quality Assessor packet",
            ):
                accept_rag_baseline(
                    config,
                    evaluation_path=evaluation_path,
                    assessment_path=assessment_path,
                )

    def test_rejects_unknown_rag_evaluation_projection_field(self) -> None:
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
            assessment = self._passed_assessment(packet)
            evaluation["unexpected"] = "/private/path"
            evaluation_path = root / "evaluation.json"
            assessment_path = root / "assessment.json"
            evaluation_path.write_text(json.dumps(evaluation), encoding="utf-8")
            assessment_path.write_text(json.dumps(assessment), encoding="utf-8")

            with self.assertRaisesRegex(
                RagEvaluationError,
                "projection",
            ):
                accept_rag_baseline(
                    config,
                    evaluation_path=evaluation_path,
                    assessment_path=assessment_path,
                )

    def test_rejects_rehashed_incomplete_or_open_rag_case_payload(self) -> None:
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

            original = evaluate_rag(
                config,
                answers_path=answers,
                runner=runner,
                executable="qmd",
                index_validator=lambda _config, _qmd: self._index_evidence(),
            )
            mutations = {
                "missing_answer": lambda case: case.pop("answer"),
                "unknown_nested_field": lambda case: case.update(
                    {"unexpected": True}
                ),
            }
            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    evaluation = json.loads(json.dumps(original))
                    packet = evaluation["quality_assessor_packet"]
                    mutate(packet["payload"]["cases"][0])
                    self._rehash_packet(packet)
                    assessment = self._passed_assessment(packet)
                    evaluation_path = root / f"{name}-evaluation.json"
                    assessment_path = root / f"{name}-assessment.json"
                    evaluation_path.write_text(
                        json.dumps(evaluation),
                        encoding="utf-8",
                    )
                    assessment_path.write_text(
                        json.dumps(assessment),
                        encoding="utf-8",
                    )

                    with self.assertRaisesRegex(
                        RagEvaluationError,
                        "RAG case",
                    ):
                        accept_rag_baseline(
                            config,
                            evaluation_path=evaluation_path,
                            assessment_path=assessment_path,
                        )

    def test_rejects_rehashed_changes_to_authoritative_rag_semantics(self) -> None:
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

            original = evaluate_rag(
                config,
                answers_path=answers,
                runner=runner,
                executable="qmd",
                index_validator=lambda _config, _qmd: self._index_evidence(),
            )
            mutations = {
                "threshold": lambda case: case["thresholds"].update(
                    {"min_recall_at_k": 0.5}
                ),
                "expected_claim": lambda case: case.update(
                    {"expected_claims": ["Altered expected claim."]}
                ),
                "answer": lambda case: case.update(
                    {"answer": "Altered generated answer."}
                ),
            }
            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    evaluation = json.loads(json.dumps(original))
                    packet = evaluation["quality_assessor_packet"]
                    mutate(packet["payload"]["cases"][0])
                    self._rehash_packet(packet)
                    assessment = self._passed_assessment(packet)
                    evaluation_path = root / f"{name}-authority-evaluation.json"
                    assessment_path = root / f"{name}-authority-assessment.json"
                    evaluation_path.write_text(
                        json.dumps(evaluation),
                        encoding="utf-8",
                    )
                    assessment_path.write_text(
                        json.dumps(assessment),
                        encoding="utf-8",
                    )

                    with self.assertRaisesRegex(
                        RagEvaluationError,
                        "authoritative case set|semantic digest",
                    ):
                        accept_rag_baseline(
                            config,
                            evaluation_path=evaluation_path,
                            assessment_path=assessment_path,
                            executable="qmd",
                            index_validator=lambda _config, _qmd: self._index_evidence(),
                        )

    def test_rechecks_rag_freshness_after_acquiring_mutation_lock(self) -> None:
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
            assessment = self._passed_assessment(packet)
            evaluation_path = root / "evaluation.json"
            assessment_path = root / "assessment.json"
            evaluation_path.write_text(json.dumps(evaluation), encoding="utf-8")
            assessment_path.write_text(json.dumps(assessment), encoding="utf-8")
            from vault_pr import rag_eval as rag_eval_module

            real_execute = rag_eval_module.execute_journaled_mutation

            def commit_change_before_lock(**kwargs: object) -> object:
                note = vault / "knowledgebase" / "rag.md"
                note.write_text(
                    note.read_text(encoding="utf-8") + "\nChanged.\n",
                    encoding="utf-8",
                )
                _git(vault, "add", "knowledgebase/rag.md")
                _git(vault, "commit", "-qm", "concurrent RAG change")
                return real_execute(**kwargs)

            with patch(
                "vault_pr.rag_eval.execute_journaled_mutation",
                side_effect=commit_change_before_lock,
            ):
                with self.assertRaisesRegex(
                    RagEvaluationError,
                    "HEAD no longer matches",
                ):
                    accept_rag_baseline(
                        config,
                        evaluation_path=evaluation_path,
                        assessment_path=assessment_path,
                        apply=True,
                        executable="qmd",
                        index_validator=lambda _config, _qmd: evidence,
                    )

            self.assertFalse(
                (vault / ".vault-pr/quality/rag/baselines").exists()
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
            assessment = self._passed_assessment(packet)
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
            unsafe_note = dict(packet["payload"]["note_evidence"][0])
            unsafe_note["path"] = "/dev/zero"
            packet["payload"]["note_evidence"].append(unsafe_note)
            case = packet["payload"]["cases"][0]
            case["retrieved_paths"].append("/dev/zero")
            case["evidence_note_refs"].append("/dev/zero")
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
            assessment = self._passed_assessment(packet)
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
            assessment = self._passed_assessment(packet)
            evaluation_path = root / "evaluation.json"
            assessment_path = root / "assessment.json"
            evaluation_path.write_text(json.dumps(evaluation), encoding="utf-8")
            assessment_path.write_text(json.dumps(assessment), encoding="utf-8")

            with self.assertRaisesRegex(RagEvaluationError, "not eligible"):
                accept_rag_baseline(
                    config,
                    evaluation_path=evaluation_path,
                    assessment_path=assessment_path,
                    executable="qmd",
                    index_validator=lambda _config, _qmd: evidence,
                )

    def test_diagnostic_dirty_vault_evaluation_cannot_be_recorded_later(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, config, answers = self._fixture(root)
            scratch = vault / "scratch.tmp"
            scratch.write_text("diagnostic dirt", encoding="utf-8")

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
            self.assertFalse(evaluation["assessment_candidate_eligible"])
            packet = evaluation["quality_assessor_packet"]
            assessment = self._passed_assessment(packet)
            scratch.unlink()
            evaluation_path = root / "dirty-evaluation.json"
            assessment_path = root / "dirty-assessment.json"
            evaluation_path.write_text(json.dumps(evaluation), encoding="utf-8")
            assessment_path.write_text(json.dumps(assessment), encoding="utf-8")

            with self.assertRaisesRegex(RagEvaluationError, "not eligible"):
                accept_rag_baseline(
                    config,
                    evaluation_path=evaluation_path,
                    assessment_path=assessment_path,
                    executable="qmd",
                    index_validator=lambda _config, _qmd: evidence,
                )


if __name__ == "__main__":
    unittest.main()
