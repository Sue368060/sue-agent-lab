from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from .visible_cost_guard import (assess_low_cost_dispatch,
                                 collect_visible_usage,
                                 read_latest_thread_usage)
from .visible_modes import DEFAULT_CONFIG, resolve_mode


class VisibleCostGuardTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        self.plan = resolve_mode(self.config, "small")

    def test_oversized_context_requires_rotation(self):
        snapshot = {"workers": {
            "A": {"input_tokens": 50001}, "B": {"input_tokens": 10}},
            "missing_worker_ids": []}
        report = assess_low_cost_dispatch(self.plan, snapshot, 2)
        self.assertFalse(report["dispatch_allowed"])
        self.assertEqual(report["decision"], "ROTATE_REQUIRED")
        self.assertEqual(report["oversized_worker_ids"], ["A"])

    def test_small_context_allows_bounded_batch(self):
        snapshot = {"workers": {
            "A": {"input_tokens": 1000}, "B": {"input_tokens": 2000}},
            "missing_worker_ids": []}
        report = assess_low_cost_dispatch(self.plan, snapshot, 2)
        self.assertTrue(report["dispatch_allowed"])
        self.assertEqual(report["decision"], "ALLOW")

    def test_missing_usage_falls_back_to_max_llm_calls(self):
        snapshot = {"workers": {}, "missing_worker_ids": ["A", "B"]}
        allowed = assess_low_cost_dispatch(self.plan, snapshot, 2)
        refused = assess_low_cost_dispatch(self.plan, snapshot, 3)
        self.assertEqual(allowed["decision"], "CALL_CAP_FALLBACK")
        self.assertTrue(allowed["dispatch_allowed"])
        self.assertFalse(refused["dispatch_allowed"])

    def test_latest_metadata_is_read_without_message_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            thread_id = self.plan["workers"][0]["thread_id"]
            path = root / f"rollout-{thread_id}.jsonl"
            rows = [
                {"type": "response_item", "payload": {"text": "secret body"}},
                {"timestamp": "first", "type": "token_usage_record",
                 "payload": {"thread_id": thread_id, "turn_id": "one",
                             "turn_token_usage": {"input_tokens": 10,
                             "cached_input_tokens": 1, "output_tokens": 2,
                             "reasoning_output_tokens": 3, "total_tokens": 15}}},
                {"timestamp": "latest", "type": "token_usage_record",
                 "payload": {"thread_id": thread_id, "turn_id": "two",
                             "turn_token_usage": {"input_tokens": 60000,
                             "cached_input_tokens": 59000, "output_tokens": 4,
                             "reasoning_output_tokens": 5, "total_tokens": 60009}}},
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows),
                            encoding="utf-8")
            usage = read_latest_thread_usage(path, thread_id)
            self.assertEqual(usage["turn_id"], "two")
            self.assertEqual(usage["input_tokens"], 60000)
            self.assertNotIn("text", usage)
            snapshot = collect_visible_usage(self.plan, root)
            self.assertIn("A", snapshot["workers"])
            self.assertIn("B", snapshot["missing_worker_ids"])


if __name__ == "__main__":
    unittest.main()
