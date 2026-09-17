import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

from .brain_takeover import TakeoverError, claim_brain


class BrainTakeoverTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tempdir.name) / "BRAIN_STATE.json"
        self.base = {
            "schema_version": 1,
            "project_id": "SUE-AGENT-LAB-2026",
            "active_brain_thread_id": "thread-old",
            "active_brain_label": "old",
            "exclusive_main_owner": True,
            "supersedes_thread_id": None,
            "current_runtime_status": {"active_visible_run_id": None},
        }
        self.state_path.write_text(json.dumps(self.base), encoding="utf-8")
        self.now = datetime(2026, 9, 15, 7, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_claim_transfers_owner_and_records_history(self):
        state = claim_brain(
            self.state_path,
            "thread-new",
            expected_owner="thread-old",
            now=self.now,
        )
        self.assertEqual(state["active_brain_thread_id"], "thread-new")
        self.assertEqual(state["supersedes_thread_id"], "thread-old")
        self.assertEqual(state["ownership_history"][-1]["to_thread_id"], "thread-new")
        self.assertEqual(
            json.loads(self.state_path.read_text())["active_brain_thread_id"],
            "thread-new",
        )

    def test_stale_expected_owner_is_rejected_without_change(self):
        with self.assertRaises(TakeoverError):
            claim_brain(
                self.state_path,
                "thread-new",
                expected_owner="someone-else",
                now=self.now,
            )
        self.assertEqual(json.loads(self.state_path.read_text()), self.base)
        with self.assertRaises(TakeoverError):
            claim_brain(self.state_path, "thread-new", now=self.now)
        self.assertEqual(json.loads(self.state_path.read_text()), self.base)

    def test_active_visible_run_blocks_different_owner(self):
        self.base["current_runtime_status"]["active_visible_run_id"] = "run-1"
        self.state_path.write_text(json.dumps(self.base), encoding="utf-8")
        with self.assertRaises(TakeoverError):
            claim_brain(
                self.state_path,
                "thread-new",
                expected_owner="thread-old",
                now=self.now,
            )

    def test_same_owner_claim_is_idempotent(self):
        state = claim_brain(
            self.state_path,
            "thread-old",
            expected_owner="thread-old",
            now=self.now,
        )
        self.assertEqual(state["active_brain_thread_id"], "thread-old")
        self.assertNotIn("ownership_history", state)
        broken = dict(self.base)
        broken["schema_version"] = 99
        self.state_path.write_text(json.dumps(broken), encoding="utf-8")
        with self.assertRaises(TakeoverError):
            claim_brain(
                self.state_path,
                "thread-old",
                expected_owner="thread-old",
                now=self.now,
            )

    def test_hidden_running_ledger_blocks_transfer(self):
        runs_dir = Path(self.tempdir.name) / "runs"
        runs_dir.mkdir()
        ledger = runs_dir / "hidden.jsonl"
        ledger.write_text("placeholder\n", encoding="utf-8")
        diagnosis = {"healthy": True, "status": "HEALTHY",
                     "run_status": "RUNNING", "run_id": "hidden-run"}
        with patch("orchestrator.brain_takeover.doctor_ledger",
                   return_value=diagnosis):
            with self.assertRaisesRegex(TakeoverError, "hidden-run is active"):
                claim_brain(
                    self.state_path, "thread-new", expected_owner="thread-old",
                    now=self.now, runs_dir=runs_dir,
                )
        self.assertEqual(json.loads(self.state_path.read_text()), self.base)

    def test_only_terminal_ledgers_allow_transfer(self):
        runs_dir = Path(self.tempdir.name) / "runs"
        runs_dir.mkdir()
        (runs_dir / "done.jsonl").write_text("placeholder\n", encoding="utf-8")
        diagnosis = {"healthy": True, "status": "HEALTHY",
                     "run_status": "DONE", "run_id": "done-run"}
        with patch("orchestrator.brain_takeover.doctor_ledger",
                   return_value=diagnosis):
            state = claim_brain(
                self.state_path, "thread-new", expected_owner="thread-old",
                now=self.now, runs_dir=runs_dir,
            )
        self.assertEqual(state["active_brain_thread_id"], "thread-new")

    def test_unverifiable_ledger_blocks_transfer(self):
        runs_dir = Path(self.tempdir.name) / "runs"
        runs_dir.mkdir()
        (runs_dir / "broken.jsonl").write_text("not-json\n", encoding="utf-8")
        with self.assertRaisesRegex(TakeoverError, "cannot verify visible ledger"):
            claim_brain(
                self.state_path, "thread-new", expected_owner="thread-old",
                now=self.now, runs_dir=runs_dir,
            )


if __name__ == "__main__":
    unittest.main()
