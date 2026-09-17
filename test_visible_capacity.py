from __future__ import annotations

import json
import unittest

from .programmatic_main import (NextActionError, apply_capacity_guard)
from .visible_capacity import CapacityError, assess_runtime_capacity
from .visible_modes import DEFAULT_CONFIG, resolve_mode


class VisibleCapacityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        cls.plan = resolve_mode(config, "large")
        cls.action = {
            "schema_version": 1,
            "run_id": "CAPACITY",
            "contract_sha256": "a" * 64,
            "ledger_head_sha256": "b" * 64,
            "ledger_revision": 0,
            "type": "DISPATCH_DRAFT_BATCH",
            "details": {"worker_ids": ["A", "B", "C", "D"]},
            "action_id": "old",
        }

    def snapshot(self, **states):
        return {"workers": {worker_id: {"state": states.get(
            worker_id, "AVAILABLE")} for worker_id in ("A", "B", "C", "D")}}

    def test_all_available_allows_original_dispatch(self):
        report = assess_runtime_capacity(self.plan, self.snapshot())
        self.assertEqual(report["decision"], "ALLOW")
        self.assertEqual(apply_capacity_guard(self.action, report), self.action)

    def test_busy_worker_uses_bounded_backoff_without_dispatch(self):
        report = assess_runtime_capacity(
            self.plan, self.snapshot(D="BUSY"), backoff_step=0)
        guarded = apply_capacity_guard(self.action, report)
        self.assertEqual(report["decision"], "BACKOFF")
        self.assertEqual(guarded["type"], "WAIT_FOR_CAPACITY")
        self.assertEqual(guarded["details"]["retry_after_seconds"], 5)

    def test_after_backoff_degrades_only_above_floor(self):
        report = assess_runtime_capacity(
            self.plan, self.snapshot(C="UNAVAILABLE", D="BUSY"),
            backoff_step=2)
        guarded = apply_capacity_guard(self.action, report)
        self.assertEqual(report["decision"], "DEGRADED")
        self.assertEqual(guarded["type"], "DISPATCH_DRAFT_BATCH")
        self.assertEqual(guarded["details"]["worker_ids"], ["A", "B"])
        self.assertTrue(guarded["details"]["must_finalize_partial"])

    def test_below_floor_stops(self):
        report = assess_runtime_capacity(
            self.plan, self.snapshot(B="UNAVAILABLE", C="UNAVAILABLE",
                                     D="UNAVAILABLE"), backoff_step=2)
        guarded = apply_capacity_guard(self.action, report)
        self.assertEqual(report["decision"], "STOP")
        self.assertEqual(guarded["type"], "STOP_FOR_CAPACITY")

    def test_incomplete_snapshot_and_tamper_fail_closed(self):
        with self.assertRaises(CapacityError):
            assess_runtime_capacity(self.plan, {"workers": {"A": {
                "state": "AVAILABLE"}}})
        report = assess_runtime_capacity(self.plan, self.snapshot())
        report["capacity_floor"] = 99
        with self.assertRaisesRegex(NextActionError, "hash mismatch"):
            apply_capacity_guard(self.action, report)


if __name__ == "__main__":
    unittest.main()
