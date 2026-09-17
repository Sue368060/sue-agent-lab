from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from .programmatic_main import (NextActionError, apply_cost_guard,
                                choose_next_action)
from .short_context_contract import freeze_contract, sha256_text
from .visible_ledger import (cancel_run, complete_turn, read_run,
                             reserve_turn, start_run)
from .visible_modes import DEFAULT_CONFIG, resolve_mode
from .visible_cost_guard import assess_low_cost_dispatch


OWNER = "brain-program-test"
EPOCH = 6
TURNS = [f"00000000-0000-4000-8000-{index:012d}" for index in range(1, 10)]


class ProgrammaticMainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))

    def tearDown(self):
        self.temp.cleanup()

    def start(self, mode="small", run_id="NEXT"):
        plan = resolve_mode(self.config, mode)
        contract = freeze_contract(
            run_id=run_id, source_request="Return one legal action.", plan=plan,
            task_plan={worker["worker_id"]: f"task {worker['worker_id']}"
                       for worker in plan["workers"]},
            task_plan_source="deterministic",
            hard_constraints=[{"id": "HC1", "value": "offline",
                               "source": "explicit_user"}],
            success_criteria={
                "machine_checks": [{"id": "MC1", "description": "ledger valid",
                                    "checker": "ledger", "required": True}],
                "semantic_goals": [],
            }, master_owner=OWNER, master_epoch=EPOCH, max_leader_turns=2)
        path = self.root / f"{run_id}.jsonl"
        start_run(path, contract, at=1, owner_id=OWNER,
                  master_epoch=EPOCH, expected_revision=-1)
        return path, plan

    def authority(self, path):
        return {"owner_id": OWNER, "master_epoch": EPOCH,
                "expected_revision": read_run(path)["revision"]}

    def draft(self, path, plan, worker, turn_index):
        reserved = reserve_turn(path, read_run(path)["run_id"], worker, "draft",
                                100, at=2, **self.authority(path))
        candidate_id = f"{worker}1"
        complete_turn(
            path, read_run(path)["run_id"], reserved["attempt_id"],
            next(item["thread_id"] for item in plan["workers"]
                 if item["worker_id"] == worker), TURNS[turn_index],
            sha256_text(candidate_id), candidate_id=candidate_id,
            candidate_version=1, at=3, **self.authority(path))

    def test_initial_action_is_one_parallel_draft_batch_and_is_stable(self):
        path, _ = self.start()
        first = choose_next_action(ledger_path=path, now=2)
        second = choose_next_action(ledger_path=path, now=2)
        self.assertEqual(first, second)
        self.assertEqual(first["type"], "DISPATCH_DRAFT_BATCH")
        self.assertEqual(first["details"]["worker_ids"], ["A", "B"])
        with self.assertRaisesRegex(NextActionError, "stale"):
            choose_next_action(ledger_path=path, expected_ledger_head="0" * 64, now=2)

    def test_low_cost_report_stops_dispatch_and_tamper_fails(self):
        path, plan = self.start(run_id="COST")
        action = choose_next_action(ledger_path=path, now=2)
        report = assess_low_cost_dispatch(
            plan, {"workers": {"A": {"input_tokens": 90000},
                                "B": {"input_tokens": 80000}},
                   "missing_worker_ids": []}, 2)
        stopped = apply_cost_guard(action, report)
        self.assertEqual(stopped["type"], "STOP_FOR_WORKER_ROTATION")
        self.assertEqual(stopped["details"]["oversized_worker_ids"], ["A", "B"])
        report["threshold_input_tokens"] = 999999
        with self.assertRaisesRegex(NextActionError, "hash mismatch"):
            apply_cost_guard(action, report)

    def test_active_attempt_means_wait_and_quick_drafts_mean_synthesize(self):
        path, plan = self.start(run_id="QUICK")
        reserved = reserve_turn(path, "QUICK", "A", "draft", 100, at=2,
                                **self.authority(path))
        action = choose_next_action(ledger_path=path, now=3)
        self.assertEqual(action["type"], "WAIT_FOR_RESULTS")
        complete_turn(path, "QUICK", reserved["attempt_id"],
                      plan["workers"][0]["thread_id"], TURNS[0],
                      sha256_text("A1"), candidate_id="A1", candidate_version=1,
                      at=3, **self.authority(path))
        self.draft(path, plan, "B", 1)
        action = choose_next_action(ledger_path=path, now=4)
        self.assertEqual(action["type"], "SYNTHESIZE")
        self.assertEqual(action["details"]["basis_candidate_ids"], ["A1", "B1"])

    def test_elapsed_target_stops_new_calls(self):
        path, _ = self.start(run_id="TIME")
        action = choose_next_action(ledger_path=path, now=1000)
        self.assertEqual(action["type"], "ABORT")
        self.assertEqual(action["details"]["reason"],
                         "elapsed_or_turn_budget_exhausted")

    def test_reviewed_mode_uses_deterministic_ring_then_synthesizes(self):
        path, plan = self.start("small_reviewed", "REVIEW")
        self.draft(path, plan, "A", 0)
        self.draft(path, plan, "B", 1)
        action = choose_next_action(ledger_path=path, now=4)
        self.assertEqual(action["type"], "DISPATCH_REVIEW")
        self.assertEqual(action["details"], {"worker_id": "A",
                                             "source_candidate_id": "B1"})
        for worker, source, turn_index in (("A", "B1", 2), ("B", "A1", 3)):
            reserved = reserve_turn(path, "REVIEW", worker, "review", 100,
                                    source_candidate_id=source, at=4,
                                    **self.authority(path))
            complete_turn(path, "REVIEW", reserved["attempt_id"],
                          next(item["thread_id"] for item in plan["workers"]
                               if item["worker_id"] == worker),
                          TURNS[turn_index], sha256_text(f"review-{worker}"),
                          verdict="PASS", issues=[], at=5, **self.authority(path))
            if worker == "A":
                next_step = choose_next_action(ledger_path=path, now=5)
                self.assertEqual(next_step["details"],
                                 {"worker_id": "B", "source_candidate_id": "A1"})
        self.assertEqual(choose_next_action(ledger_path=path, now=6)["type"],
                         "SYNTHESIZE")

    def test_blocker_routes_revision_then_original_reviewer_recheck(self):
        path, plan = self.start("small_reviewed", "BLOCKER")
        self.draft(path, plan, "A", 0)
        self.draft(path, plan, "B", 1)
        review = reserve_turn(path, "BLOCKER", "A", "review", 100,
                              source_candidate_id="B1", at=4,
                              **self.authority(path))
        complete_turn(path, "BLOCKER", review["attempt_id"],
                      plan["workers"][0]["thread_id"], TURNS[2],
                      sha256_text("block"), verdict="ISSUES",
                      issues=[{"id": "B1-X", "severity": "BLOCKER",
                               "problem": "missing evidence"}], at=5,
                      **self.authority(path))
        action = choose_next_action(ledger_path=path, now=5)
        self.assertEqual(action["type"], "DISPATCH_REVISION")
        self.assertEqual(action["details"]["worker_id"], "B")
        revision = reserve_turn(path, "BLOCKER", "B", "revision", 100,
                                source_candidate_id="B1", blocker_id="B1-X",
                                at=5, **self.authority(path))
        complete_turn(path, "BLOCKER", revision["attempt_id"],
                      plan["workers"][1]["thread_id"], TURNS[3],
                      sha256_text("B2"), candidate_id="B2", candidate_version=2,
                      at=6, **self.authority(path))
        action = choose_next_action(ledger_path=path, now=6)
        self.assertEqual(action["type"], "DISPATCH_RECHECK")
        self.assertEqual(action["details"]["worker_id"], "A")
        self.assertEqual(action["details"]["source_candidate_id"], "B2")

    def test_dispatch_closed_and_terminal_states_stop_cleanly(self):
        path, _ = self.start(run_id="STOP")
        cancel_run(path, "STOP", "main_task", "user cancelled", at=2,
                   **self.authority(path))
        action = choose_next_action(ledger_path=path, now=3)
        self.assertEqual(action["type"], "STOP")
        self.assertEqual(action["details"]["terminal_status"], "ABORT")


if __name__ == "__main__":
    unittest.main()
