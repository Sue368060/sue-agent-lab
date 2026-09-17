from __future__ import annotations

from copy import deepcopy
import json
import unittest

from .short_context_contract import (ContractError, assert_runtime_revision, consume_turn,
                                     freeze_contract, new_runtime,
                                     normalize_review_payload,
                                     transition_phase, validate_contract)
from .visible_modes import DEFAULT_CONFIG, resolve_mode


class ShortContextContractTests(unittest.TestCase):
    def setUp(self):
        config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        self.plan = resolve_mode(config, "small")
        self.kwargs = {
            "run_id": "SC-001",
            "source_request": "Plan a calm seven-day trip under CNY 15000.",
            "plan": self.plan,
            "task_plan": {"A": "route", "B": "transport review"},
            "task_plan_source": "leader",
            "hard_constraints": [
                {"id": "HC1", "value": {"budget_max_cny": 15000},
                 "source": "explicit_user"},
            ],
            "success_criteria": {
                "machine_checks": [
                    {"id": "MC1", "description": "budget is within limit",
                     "checker": "budget_max", "required": True},
                ],
                "semantic_goals": [
                    {"id": "SG1", "description": "calm pace", "required": False},
                ],
            },
            "master_owner": "brain-thread-1",
            "master_epoch": 7,
            "max_leader_turns": 3,
        }

    def test_frozen_contract_is_deterministic_and_hash_bound(self):
        first = freeze_contract(**self.kwargs)
        second = freeze_contract(**self.kwargs)
        self.assertEqual(first, second)
        validate_contract(first)
        self.assertEqual(first["initial_phase"], "PLANNED")
        self.assertEqual(first["worker_ids"], ["A", "B"])
        self.assertEqual(first["review_policy"]["mode"], "none")
        self.assertEqual(first["budgets"]["max_worker_deadline_seconds"], 300)
        self.assertEqual(first["delivery_contract"]["minimum_inline_chars"], 800)
        self.assertIn("section_heading_aliases", first["delivery_contract"])
        self.assertEqual(first["final_gate_contract"]["version"], 2)
        self.assertEqual(first["final_gate_contract"]["max_final_revisions"], 1)
        self.assertEqual(first["external_review_contract"]["explicit_request_rounds"], 1)
        self.assertEqual(first["mode_provenance"]["resolved_policy"]["mode"], "small")

    def test_quick_contract_can_move_from_collection_to_synthesis(self):
        contract = freeze_contract(**self.kwargs)
        runtime = new_runtime(contract)
        for phase in ("DISPATCHING", "COLLECTING", "READY_TO_SYNTHESIZE"):
            runtime = transition_phase(runtime, contract, phase, "brain-thread-1", 7)
        self.assertEqual(runtime["phase"], "READY_TO_SYNTHESIZE")

    def test_source_or_task_plan_tamper_is_rejected(self):
        contract = freeze_contract(**self.kwargs)
        for path, value in (("source_request", "changed"), ("assignment", "changed role")):
            with self.subTest(path=path):
                changed = deepcopy(contract)
                if path == "source_request":
                    changed[path] = value
                else:
                    changed["task_plan"]["assignments"]["A"] = value
                with self.assertRaises(ContractError):
                    validate_contract(changed)

    def test_rehashed_semantically_invalid_contract_is_rejected(self):
        from .short_context_contract import sha256_json
        contract = freeze_contract(**self.kwargs)
        for mutation in ("source_type", "duplicate_worker", "empty_assignment",
                         "bad_criteria", "budgets_list", "master_list",
                         "worker_budget_bool", "master_epoch_bool",
                         "review_limits_list", "review_limit_bool",
                         "deadline_bool", "delivery_floor", "final_gate_budget",
                         "external_review_budget", "mode_provenance"):
            with self.subTest(mutation=mutation):
                changed = deepcopy(contract)
                if mutation == "source_type":
                    changed["source_request"] = 123
                elif mutation == "duplicate_worker":
                    changed["workers"][1]["thread_id"] = changed["workers"][0]["thread_id"]
                elif mutation == "empty_assignment":
                    changed["task_plan"]["assignments"]["A"] = ""
                    changed["task_plan"]["sha256"] = sha256_json(changed["task_plan"]["assignments"])
                elif mutation == "bad_criteria":
                    changed["success_criteria"]["machine_checks"][0]["checker"] = ""
                elif mutation == "budgets_list":
                    changed["budgets"] = [1]
                elif mutation == "master_list":
                    changed["master"] = [1]
                elif mutation == "worker_budget_bool":
                    changed["budgets"]["max_worker_turns"] = True
                elif mutation == "master_epoch_bool":
                    changed["master"]["epoch"] = True
                elif mutation == "review_limits_list":
                    changed["review_limits"] = [1]
                elif mutation == "review_limit_bool":
                    changed["review_limits"]["max_blocker_revisions"] = True
                elif mutation == "deadline_bool":
                    changed["budgets"]["max_worker_deadline_seconds"] = True
                elif mutation == "delivery_floor":
                    changed["delivery_contract"]["minimum_inline_chars"] = 1
                elif mutation == "final_gate_budget":
                    changed["final_gate_contract"]["max_final_revisions"] = 99
                elif mutation == "external_review_budget":
                    changed["external_review_contract"]["explicit_request_rounds"] = 9
                else:
                    changed["mode_provenance"]["resolved_policy"]["mode"] = "large"
                body = {key: value for key, value in changed.items() if key != "contract_sha256"}
                changed["contract_sha256"] = sha256_json(body)
                with self.assertRaises(ContractError):
                    validate_contract(changed)

    def test_every_active_worker_needs_exactly_one_assignment(self):
        bad = dict(self.kwargs)
        bad["task_plan"] = {"A": "route"}
        with self.assertRaises(ContractError):
            freeze_contract(**bad)

    def test_inferred_constraint_requires_user_confirmation(self):
        bad = dict(self.kwargs)
        bad["hard_constraints"] = [
            {"id": "HC1", "value": "relaxed", "source": "leader_inference"}
        ]
        with self.assertRaises(ContractError):
            freeze_contract(**bad)
        bad["hard_constraints"][0]["confirmed_by_user"] = True
        contract = freeze_contract(**bad)
        self.assertTrue(contract["hard_constraints"]["items"][0]["confirmed_by_user"])

    def test_required_semantic_goal_needs_verifier(self):
        bad = deepcopy(self.kwargs)
        bad["success_criteria"]["semantic_goals"][0]["required"] = True
        with self.assertRaises(ContractError):
            freeze_contract(**bad)
        bad["success_criteria"]["semantic_goals"][0]["verifier"] = "leader"
        freeze_contract(**bad)

    def test_review_payload_contract_and_deterministic_repairs(self):
        payload = {"verdict": "issues", "issues": [{
            "severity": "non-blocker", "problem": "dense day",
            "evidence": "two distant stops", "recommendation": "remove one stop"}]}
        normalized = normalize_review_payload("CAND-A-V1", json.dumps(payload))
        self.assertEqual(normalized["verdict"], "ISSUES")
        self.assertEqual(normalized["issues"][0]["id"], "CAND-A-V1-I01")
        self.assertEqual(normalized["issues"][0]["severity"], "NON_BLOCKER")

    def test_review_payload_cannot_invent_missing_evidence_or_change_issue_identity(self):
        missing = {"verdict": "ISSUES", "issues": [{
            "severity": "BLOCKER", "problem": "dense", "recommendation": "trim"}]}
        wrong_id = {"verdict": "ISSUES", "issues": [{
            "id": "OTHER-I01", "severity": "BLOCKER", "problem": "dense",
            "evidence": "too far", "recommendation": "trim"}]}
        for payload in (missing, wrong_id, {"verdict": "PASS", "issues": [wrong_id]}):
            with self.subTest(payload=payload), self.assertRaises(ContractError):
                normalize_review_payload("CAND-A-V1", payload)

    def test_reserved_attempt_count_is_never_refunded(self):
        contract = freeze_contract(**self.kwargs)
        self.assertEqual(contract["turn_counting"]["unit"], "reserved_attempt")
        self.assertFalse(contract["turn_counting"]["refund_on_failure"])
        runtime = new_runtime(contract)
        after_reserve = consume_turn(runtime, contract, "worker", "brain-thread-1", 7)
        self.assertEqual(after_reserve["worker_turns_used"], 1)
        self.assertEqual(after_reserve["revision"], 1)

    def test_runtime_is_bound_and_malformed_state_is_rejected(self):
        contract = freeze_contract(**self.kwargs)
        baseline = new_runtime(contract)
        mutations = {
            "run_id": "OTHER", "contract_sha256": "0" * 64,
            "phase": "UNKNOWN", "terminal_status": "DONE",
            "worker_turns_used": -1, "leader_turns_used": "1",
            "revision": True, "master": {"owner_id": "brain-thread-1", "epoch": 6},
        }
        for key, value in mutations.items():
            with self.subTest(key=key):
                runtime = deepcopy(baseline)
                runtime[key] = value
                with self.assertRaises(ContractError):
                    consume_turn(runtime, contract, "worker", "brain-thread-1", 7)
        for runtime in (None, [], "bad", 1):
            with self.subTest(runtime=runtime), self.assertRaises(ContractError):
                transition_phase(runtime, contract, "DISPATCHING", "brain-thread-1", 7)

    def test_stale_runtime_snapshot_is_rejected_against_authoritative_revision(self):
        contract = freeze_contract(**self.kwargs)
        old = new_runtime(contract)
        current = transition_phase(old, contract, "DISPATCHING", "brain-thread-1", 7)
        assert_runtime_revision(current, contract, current["revision"])
        with self.assertRaises(ContractError):
            assert_runtime_revision(old, contract, current["revision"])

    def test_public_entry_points_raise_contract_error_for_bad_types(self):
        bad_plan = dict(self.kwargs)
        bad_plan["plan"] = None
        with self.assertRaises(ContractError):
            freeze_contract(**bad_plan)
        contract = freeze_contract(**self.kwargs)
        runtime = new_runtime(contract)
        for next_phase in (None, [], {}):
            with self.subTest(next_phase=next_phase), self.assertRaises(ContractError):
                transition_phase(runtime, contract, next_phase, "brain-thread-1", 7)

    def test_master_epoch_rejects_stale_or_foreign_writer(self):
        contract = freeze_contract(**self.kwargs)
        runtime = new_runtime(contract)
        for owner, epoch in (("brain-thread-0", 7), ("brain-thread-1", 6)):
            with self.subTest(owner=owner, epoch=epoch), self.assertRaises(ContractError):
                transition_phase(runtime, contract, "DISPATCHING", owner, epoch)
        advanced = transition_phase(runtime, contract, "DISPATCHING", "brain-thread-1", 7)
        self.assertEqual(advanced["phase"], "DISPATCHING")

    def test_leader_and_worker_budgets_are_independent(self):
        contract = freeze_contract(**self.kwargs)
        runtime = new_runtime(contract)
        self.assertEqual(runtime["leader_turns_used"], 1)
        runtime = consume_turn(runtime, contract, "worker", "brain-thread-1", 7)
        self.assertEqual(runtime["worker_turns_used"], 1)
        self.assertEqual(runtime["leader_turns_used"], 1)
        runtime = consume_turn(runtime, contract, "leader", "brain-thread-1", 7)
        runtime = consume_turn(runtime, contract, "leader", "brain-thread-1", 7)
        with self.assertRaises(ContractError):
            consume_turn(runtime, contract, "leader", "brain-thread-1", 7)

    def test_illegal_done_shortcut_and_post_terminal_turn_are_rejected(self):
        contract = freeze_contract(**self.kwargs)
        runtime = new_runtime(contract)
        with self.assertRaises(ContractError):
            transition_phase(runtime, contract, "DONE", "brain-thread-1", 7)
        for phase in ("DISPATCHING", "COLLECTING", "REVIEWING",
                      "READY_TO_SYNTHESIZE", "SYNTHESIZING", "FINAL_CHECK", "DONE"):
            runtime = transition_phase(runtime, contract, phase, "brain-thread-1", 7)
        with self.assertRaises(ContractError):
            consume_turn(runtime, contract, "worker", "brain-thread-1", 7)


if __name__ == "__main__":
    unittest.main()
