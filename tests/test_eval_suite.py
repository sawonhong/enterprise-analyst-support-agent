"""Golden Dataset Regression Evaluation Suite for the Enterprise Support Agent."""

import json
from pathlib import Path
import unittest

from enterprise_support_agent.agent import orchestrator


class TestGoldenDatasetEvaluation(unittest.TestCase):
    """Benchmarks agent accuracy, intent routing, and latency SLA across golden cases."""

    def test_golden_benchmark_suite(self) -> None:
        """Execute all golden evaluation prompts and assert >=95% accuracy and <5000ms latency."""
        eval_path = Path(__file__).resolve().parent / "eval_dataset.json"
        self.assertTrue(eval_path.exists(), "eval_dataset.json must exist")

        with open(eval_path, "r", encoding="utf-8") as file_obj:
            golden_cases = json.load(file_obj)

        passed_cases = 0
        total_cases = len(golden_cases)

        for case in golden_cases:
            eval_id = case["eval_id"]
            prompt = case["prompt"]
            expected_intent = case["expected_intent"]
            expected_keywords = case["expected_keywords"]

            turn = orchestrator.chat(prompt, session_id=f"eval_{eval_id}")
            response_text = turn["response"]
            routed_intent = turn["agent_state"]["routed_intent"]

            # Assert turn latency is well under the 5000ms SLA
            self.assertLess(
                turn["total_duration_ms"],
                5000.0,
                f"[{eval_id}] Turn latency {turn['total_duration_ms']}ms exceeded 5000ms SLA",
            )

            # Assert intent routing matches expected intent
            self.assertEqual(
                routed_intent,
                expected_intent,
                f"[{eval_id}] Expected intent {expected_intent}, got {routed_intent}",
            )

            # Assert all expected keywords appear in the synthesized response
            matched = [
                kw for kw in expected_keywords if kw.lower() in response_text.lower()
            ]
            score = len(matched) / max(len(expected_keywords), 1)
            if score >= 0.8:
                passed_cases += 1

        accuracy_pct = (passed_cases / max(total_cases, 1)) * 100.0
        print(
            f"\n📊 Golden Dataset Evaluation Accuracy: {accuracy_pct:.1f}% "
            f"({passed_cases}/{total_cases} passed)"
        )
        self.assertGreaterEqual(accuracy_pct, 95.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
