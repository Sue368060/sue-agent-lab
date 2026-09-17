"""Frozen schema-v3 contract for a short-context Programmatic Main.

This module is deterministic and starts no model turns.  It defines what a
future ledger-backed coordinator must persist before visible dispatch begins.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json


class ContractError(ValueError):
    pass


ACTIVE_PHASES = (
    "CREATED", "CONSTRAINTS_FROZEN", "PLANNED", "DISPATCHING",
    "COLLECTING", "REVIEWING", "REVISING", "READY_TO_SYNTHESIZE",
    "SYNTHESIZING", "FINAL_CHECK",
)
TERMINAL_PHASES = ("DONE", "PARTIAL", "ABORT")
LEGAL_TRANSITIONS = {
    "CREATED": {"CONSTRAINTS_FROZEN", "ABORT"},
    "CONSTRAINTS_FROZEN": {"PLANNED", "ABORT"},
    "PLANNED": {"DISPATCHING", "ABORT"},
    "DISPATCHING": {"COLLECTING", "ABORT"},
    "COLLECTING": {"REVIEWING", "READY_TO_SYNTHESIZE", "PARTIAL", "ABORT"},
    "REVIEWING": {"REVISING", "READY_TO_SYNTHESIZE", "PARTIAL", "ABORT"},
    "REVISING": {"REVIEWING", "PARTIAL", "ABORT"},
    "READY_TO_SYNTHESIZE": {"SYNTHESIZING", "ABORT"},
    "SYNTHESIZING": {"FINAL_CHECK", "ABORT"},
    "FINAL_CHECK": {"SYNTHESIZING", "DONE", "PARTIAL", "ABORT"},
}


def _json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ContractError("contract value is not JSON serializable") from exc


def sha256_text(value: str) -> str:
    if not isinstance(value, str):
        raise ContractError("hash input must be text")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: object) -> str:
    return sha256_text(_json(value))


def _validate_workers(plan: dict) -> tuple[list[str], list[dict]]:
    if not isinstance(plan, dict):
        raise ContractError("plan must be an object")
    mode = plan.get("mode")
    if not isinstance(mode, str) or not mode.strip():
        raise ContractError("plan mode is invalid")
    workers = plan.get("workers")
    if not isinstance(workers, list) or not workers:
        raise ContractError("plan needs workers")
    ids = []
    thread_ids = []
    for worker in workers:
        if not isinstance(worker, dict):
            raise ContractError("worker entry is invalid")
        worker_id = worker.get("worker_id")
        thread_id = worker.get("thread_id")
        if not isinstance(worker_id, str) or not worker_id:
            raise ContractError("worker ID is invalid")
        if not isinstance(thread_id, str) or not thread_id:
            raise ContractError("worker thread ID is invalid")
        ids.append(worker_id)
        thread_ids.append(thread_id)
    if len(ids) != len(set(ids)) or len(thread_ids) != len(set(thread_ids)):
        raise ContractError("worker and thread IDs must be unique")
    revisions = plan.get("max_blocker_revisions")
    rechecks = plan.get("max_blocker_rechecks")
    cap = plan.get("max_worker_turns")
    review_policy = plan.get("review_policy", "full")
    if (not isinstance(revisions, int) or isinstance(revisions, bool) or revisions < 0 or
            not isinstance(rechecks, int) or isinstance(rechecks, bool) or rechecks < 0 or
            review_policy not in ("none", "full") or
            (review_policy == "none" and (revisions != 0 or rechecks != 0)) or
            not isinstance(cap, int) or isinstance(cap, bool)):
        raise ContractError("worker turn cap cannot cover the frozen plan")
    minimum_turns = (len(ids) if review_policy == "none" else
                     2 * len(ids) + revisions + rechecks)
    if cap < minimum_turns:
        raise ContractError("worker turn cap cannot cover the frozen plan")
    if plan.get("final_owner") != "main_task":
        raise ContractError("final owner must be main_task")
    deadline = plan.get("worker_deadline_seconds")
    target_elapsed = plan.get("target_elapsed_seconds")
    if (not isinstance(deadline, int) or isinstance(deadline, bool) or
            deadline < 60 or not isinstance(target_elapsed, int) or
            isinstance(target_elapsed, bool) or target_elapsed < deadline):
        raise ContractError("elapsed-time budget is invalid")
    capacity_policy = plan.get("capacity_policy")
    backoff = (capacity_policy or {}).get("runtime_backoff")
    if (not isinstance(capacity_policy, dict) or
            not isinstance(capacity_policy.get("capacity_floor"), int) or
            isinstance(capacity_policy.get("capacity_floor"), bool) or
            not 1 <= capacity_policy["capacity_floor"] <= len(ids) or
            not isinstance(backoff, dict) or
            not isinstance(backoff.get("max_steps"), int) or
            isinstance(backoff.get("max_steps"), bool) or
            backoff["max_steps"] < 0 or
            not isinstance(backoff.get("retry_after_seconds"), list) or
            len(backoff["retry_after_seconds"]) != backoff["max_steps"]):
        raise ContractError("capacity policy is invalid")
    provenance = plan.get("mode_provenance")
    expected_policy = {key: deepcopy(plan[key]) for key in (
        "mode", "workers", "review_policy", "max_worker_turns",
        "max_review_rounds", "max_blocker_revisions", "max_blocker_rechecks",
        "worker_deadline_seconds", "target_elapsed_seconds")}
    expected_policy["capacity_policy"] = deepcopy(capacity_policy)
    if (not isinstance(provenance, dict) or provenance.get("schema_version") != 1 or
            not isinstance(provenance.get("config_sha256"), str) or
            len(provenance["config_sha256"]) != 64 or
            provenance.get("resolved_policy") != expected_policy or
            provenance.get("resolved_policy_sha256") != sha256_json(expected_policy)):
        raise ContractError("mode provenance is invalid")
    return ids, deepcopy(workers)


def _validate_frozen_workers(workers: object, worker_ids: object) -> None:
    if not isinstance(workers, list) or not workers or not isinstance(worker_ids, list):
        raise ContractError("frozen worker roster is invalid")
    ids = []
    thread_ids = []
    for worker in workers:
        if not isinstance(worker, dict):
            raise ContractError("frozen worker entry is invalid")
        worker_id = worker.get("worker_id")
        thread_id = worker.get("thread_id")
        if (not isinstance(worker_id, str) or not worker_id or
                not isinstance(thread_id, str) or not thread_id):
            raise ContractError("frozen worker identity is invalid")
        ids.append(worker_id)
        thread_ids.append(thread_id)
    if ids != worker_ids or len(ids) != len(set(ids)) or len(thread_ids) != len(set(thread_ids)):
        raise ContractError("frozen worker roster is inconsistent")


def _freeze_constraints(source_request: str, items: list[dict]) -> dict:
    if not isinstance(items, list):
        raise ContractError("hard constraints must be a list")
    frozen = []
    ids = set()
    for item in items:
        if not isinstance(item, dict):
            raise ContractError("hard constraint is invalid")
        item_id = item.get("id")
        source = item.get("source")
        if (not isinstance(item_id, str) or not item_id or item_id in ids or
                source not in ("explicit_user", "leader_inference") or
                "value" not in item):
            raise ContractError("hard constraint identity/source is invalid")
        if source == "leader_inference" and item.get("confirmed_by_user") is not True:
            raise ContractError("inferred constraint needs user confirmation before freeze")
        normalized = {"id": item_id, "value": deepcopy(item["value"]),
                      "source": source, "status": "FROZEN"}
        if source == "leader_inference":
            normalized["confirmed_by_user"] = True
        _json(normalized)
        frozen.append(normalized)
        ids.add(item_id)
    return {"source_text": source_request,
            "source_sha256": sha256_text(source_request),
            "items": frozen, "status": "FROZEN"}


def _freeze_criteria(criteria: dict) -> dict:
    if not isinstance(criteria, dict):
        raise ContractError("success criteria must be an object")
    machine = criteria.get("machine_checks")
    semantic = criteria.get("semantic_goals")
    if not isinstance(machine, list) or not machine:
        raise ContractError("at least one machine check is required")
    if not isinstance(semantic, list):
        raise ContractError("semantic goals must be a list")
    seen = set()
    machine_out = []
    for check in machine:
        if (not isinstance(check, dict) or not isinstance(check.get("id"), str) or
                not check["id"] or check["id"] in seen or
                not isinstance(check.get("description"), str) or
                not check["description"].strip() or
                not isinstance(check.get("checker"), str) or
                not check["checker"].strip() or
                not isinstance(check.get("required"), bool)):
            raise ContractError("machine check is not executable")
        seen.add(check["id"])
        machine_out.append(deepcopy(check))
    semantic_out = []
    for goal in semantic:
        if (not isinstance(goal, dict) or not isinstance(goal.get("id"), str) or
                not goal["id"] or goal["id"] in seen or
                not isinstance(goal.get("description"), str) or
                not goal["description"].strip()):
            raise ContractError("semantic goal is invalid")
        if goal.get("required") is True and not isinstance(goal.get("verifier"), str):
            raise ContractError("required semantic goal needs an explicit verifier")
        seen.add(goal["id"])
        semantic_out.append(deepcopy(goal))
    return {"machine_checks": machine_out, "semantic_goals": semantic_out}


def _freeze_delivery_contract(plan: dict) -> dict:
    delivery = plan.get("delivery_contract")
    if not isinstance(delivery, dict):
        raise ContractError("delivery contract is required")
    required = delivery.get("required_sections")
    aliases = delivery.get("section_heading_aliases")
    hardened_values = (
        delivery.get("maximum_section_similarity_ratio"),
        delivery.get("minimum_synthesis_to_longest_worker_ratio"),
    )
    hardened_valid = (
        hardened_values == (None, None) or
        (isinstance(hardened_values[0], (int, float)) and
         not isinstance(hardened_values[0], bool) and
         0.1 <= hardened_values[0] <= 0.9 and
         isinstance(hardened_values[1], (int, float)) and
         not isinstance(hardened_values[1], bool) and
         0.1 <= hardened_values[1] <= 1)
    )
    if (delivery.get("inline_full_result") is not True or
            delivery.get("final_artifact_must_be_inline") is not True or
            delivery.get("files_are_supplements") is not True or
            delivery.get("must_cover_every_worker") is not True or
            not isinstance(delivery.get("minimum_inline_chars"), int) or
            isinstance(delivery.get("minimum_inline_chars"), bool) or
            delivery["minimum_inline_chars"] < 300 or
            not isinstance(delivery.get("minimum_final_to_longest_worker_ratio"),
                           (int, float)) or
            isinstance(delivery.get("minimum_final_to_longest_worker_ratio"), bool) or
            not 0.5 <= delivery["minimum_final_to_longest_worker_ratio"] <= 1 or
            not isinstance(delivery.get("minimum_worker_output_chars"), int) or
            isinstance(delivery.get("minimum_worker_output_chars"), bool) or
            delivery["minimum_worker_output_chars"] < 100 or
            not isinstance(delivery.get("minimum_worker_overlap_chars"), int) or
            isinstance(delivery.get("minimum_worker_overlap_chars"), bool) or
            not 16 <= delivery["minimum_worker_overlap_chars"] <= 200 or
            not isinstance(delivery.get("minimum_section_chars"), int) or
            isinstance(delivery.get("minimum_section_chars"), bool) or
            delivery["minimum_section_chars"] < 20 or
            not isinstance(delivery.get("maximum_repeated_ngram_ratio"),
                           (int, float)) or
            isinstance(delivery.get("maximum_repeated_ngram_ratio"), bool) or
            not 0.01 <= delivery["maximum_repeated_ngram_ratio"] <= 0.25 or
            not hardened_valid or
            not isinstance(required, list) or len(required) < 6 or
            len(required) != len(set(required)) or
            not all(isinstance(value, str) and value for value in required) or
            not isinstance(aliases, dict) or set(aliases) != set(required) or
            any(not isinstance(values, list) or not values or
                not all(isinstance(value, str) and value.strip() for value in values)
                for values in aliases.values())):
        raise ContractError("delivery contract is invalid")
    return deepcopy(delivery)


def _freeze_final_gate_contract(max_leader_turns: int) -> dict:
    """Freeze the smallest deterministic evidence policy needed before DONE."""
    return {
        "version": 2,
        "max_final_revisions": max_leader_turns - 2,
        "source_claim_min_overlap_chars": 24,
        "evidence_quote_min_chars": 16,
        "synthesis_min_sources": 2,
        "synthesis_min_rationale_chars": 30,
        "require_all_machine_checks": True,
    }


def _freeze_external_review_contract(plan: dict) -> dict:
    policy = plan.get("external_review")
    if (not isinstance(policy, dict) or policy.get("default_rounds") != 0 or
            policy.get("explicit_request_rounds") != 1 or
            policy.get("automatic_recheck") is not False):
        raise ContractError("external review contract is invalid")
    return deepcopy(policy)


def _validate_final_gate_contract(gate: object, max_leader_turns: int) -> None:
    if gate != _freeze_final_gate_contract(max_leader_turns):
        raise ContractError("final gate contract is invalid")


def normalize_review_payload(candidate_id: str, payload: object) -> dict:
    """Apply only the deterministic review repairs permitted by schema v3."""
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ContractError("candidate ID is required")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise ContractError("FORMAT_INVALID") from exc
    if not isinstance(payload, dict):
        raise ContractError("FORMAT_INVALID")
    verdict = payload.get("verdict")
    if isinstance(verdict, str):
        verdict = verdict.strip().upper()
    if verdict == "BLOCKER":
        verdict = "ISSUES"
    if verdict not in ("PASS", "ISSUES"):
        raise ContractError("FORMAT_INVALID")
    issues = payload.get("issues")
    if not isinstance(issues, list) or (verdict == "PASS" and issues) or (verdict == "ISSUES" and not issues):
        raise ContractError("FORMAT_INVALID")
    normalized = []
    for index, issue in enumerate(issues, start=1):
        if not isinstance(issue, dict):
            raise ContractError("FORMAT_INVALID")
        severity = issue.get("severity")
        if isinstance(severity, str):
            severity = severity.strip().upper().replace("-", "_")
        if severity not in ("BLOCKER", "NON_BLOCKER"):
            raise ContractError("FORMAT_INVALID")
        problem = issue.get("problem")
        evidence = issue.get("evidence")
        recommendation = issue.get("recommendation")
        if not all(isinstance(value, str) and value.strip()
                   for value in (problem, evidence, recommendation)):
            raise ContractError("FORMAT_INVALID")
        expected_id = f"{candidate_id}-I{index:02d}"
        issue_id = issue.get("id", expected_id)
        if issue_id != expected_id:
            raise ContractError("FORMAT_INVALID")
        normalized.append({"id": issue_id, "severity": severity,
                           "problem": problem.strip(), "evidence": evidence.strip(),
                           "recommendation": recommendation.strip()})
    return {"verdict": verdict, "issues": normalized}


def freeze_contract(*, run_id: str, source_request: str, plan: dict,
                    task_plan: dict[str, str], task_plan_source: str,
                    hard_constraints: list[dict], success_criteria: dict,
                    master_owner: str, master_epoch: int = 1,
                    max_leader_turns: int = 3) -> dict:
    """Create the immutable pre-dispatch contract and its content hash."""
    if not isinstance(run_id, str) or not run_id or not run_id.replace("-", "").isalnum():
        raise ContractError("run ID is invalid")
    if not isinstance(source_request, str) or not source_request.strip():
        raise ContractError("source request is required")
    worker_ids, workers = _validate_workers(plan)
    if (not isinstance(task_plan, dict) or set(task_plan) != set(worker_ids) or
            not all(isinstance(value, str) and value.strip()
                    for value in task_plan.values())):
        raise ContractError("task plan must assign every active Worker exactly once")
    if task_plan_source not in ("user", "leader", "deterministic"):
        raise ContractError("task plan source is invalid")
    if not isinstance(master_owner, str) or not master_owner.strip():
        raise ContractError("master owner is required")
    if (not isinstance(master_epoch, int) or isinstance(master_epoch, bool) or
            master_epoch < 1):
        raise ContractError("master epoch must be positive")
    if not isinstance(max_leader_turns, int) or not 2 <= max_leader_turns <= 3:
        raise ContractError("V1 leader budget must be two or three turns")
    constraints = _freeze_constraints(source_request, hard_constraints)
    criteria = _freeze_criteria(success_criteria)
    delivery_contract = _freeze_delivery_contract(plan)
    external_review_contract = _freeze_external_review_contract(plan)
    frozen_task_plan = {key: task_plan[key] for key in sorted(task_plan)}
    body = {
        "schema_version": 3,
        "run_id": run_id,
        "source_request": source_request,
        "source_request_sha256": sha256_text(source_request),
        "mode": plan.get("mode"),
        "mode_provenance": deepcopy(plan["mode_provenance"]),
        "capacity_policy": deepcopy(plan["capacity_policy"]),
        "workers": workers,
        "worker_ids": worker_ids,
        "final_owner": "main_task",
        "review_policy": {"mode": plan.get("review_policy", "full"),
                          "required_candidate_coverage":
                          plan.get("review_policy", "full") == "full"},
        "review_limits": {"max_blocker_revisions": plan["max_blocker_revisions"],
                          "max_blocker_rechecks": plan["max_blocker_rechecks"]},
        "budgets": {"max_worker_turns": plan["max_worker_turns"],
                    "max_leader_turns": max_leader_turns,
                    "max_worker_deadline_seconds": plan["worker_deadline_seconds"],
                    "target_elapsed_seconds": plan["target_elapsed_seconds"]},
        "turn_counting": {"unit": "reserved_attempt",
                          "refund_on_failure": False,
                          "counted_outcomes": ["DONE", "FAILED", "TIMEOUT",
                                               "CANCELLED", "OUTCOME_UNKNOWN"]},
        "review_contract": {"verdicts": ["PASS", "ISSUES"],
                            "severities": ["BLOCKER", "NON_BLOCKER"],
                            "issue_id_pattern": "<candidate_id>-I<two_digit_sequence>",
                            "invalid_status": "FORMAT_INVALID",
                            "automatic_model_retry": False},
        "task_plan": {"assignments": frozen_task_plan,
                      "source": task_plan_source, "status": "FROZEN",
                      "sha256": sha256_json(frozen_task_plan)},
        "hard_constraints": constraints,
        "success_criteria": criteria,
        "delivery_contract": delivery_contract,
        "external_review_contract": external_review_contract,
        "final_gate_contract": _freeze_final_gate_contract(max_leader_turns),
        "initial_phase": "PLANNED",
        "dispatch_frozen": True,
        "master": {"owner_id": master_owner, "epoch": master_epoch},
    }
    body["contract_sha256"] = sha256_json(body)
    validate_contract(body)
    return body


def validate_contract(contract: dict) -> None:
    if not isinstance(contract, dict) or contract.get("schema_version") != 3:
        raise ContractError("unsupported contract schema")
    supplied_hash = contract.get("contract_sha256")
    body = {key: value for key, value in contract.items() if key != "contract_sha256"}
    if supplied_hash != sha256_json(body):
        raise ContractError("contract hash mismatch")
    source_request = contract.get("source_request")
    if (not isinstance(source_request, str) or not source_request.strip() or
            contract.get("source_request_sha256") != sha256_text(source_request)):
        raise ContractError("source request hash mismatch")
    workers = contract.get("workers")
    worker_ids = contract.get("worker_ids")
    _validate_frozen_workers(workers, worker_ids)
    if not isinstance(contract.get("mode"), str) or not contract["mode"].strip():
        raise ContractError("frozen mode is invalid")
    if contract.get("final_owner") != "main_task":
        raise ContractError("frozen final owner is invalid")
    task_plan = contract.get("task_plan")
    if not isinstance(task_plan, dict):
        raise ContractError("frozen task plan is invalid")
    assignments = task_plan.get("assignments")
    if (not isinstance(assignments, dict) or set(assignments) != set(worker_ids) or
            not all(isinstance(value, str) and value.strip() for value in assignments.values()) or
            task_plan.get("source") not in ("user", "leader", "deterministic") or
            task_plan.get("sha256") != sha256_json(assignments) or
            task_plan.get("status") != "FROZEN"):
        raise ContractError("frozen task plan is inconsistent")
    constraints = contract.get("hard_constraints")
    if not isinstance(constraints, dict):
        raise ContractError("hard constraints are invalid")
    try:
        normalized_constraints = _freeze_constraints(source_request, constraints.get("items"))
    except ContractError as exc:
        raise ContractError("hard constraints are not frozen to the source") from exc
    if constraints != normalized_constraints:
        raise ContractError("hard constraints are not frozen to the source")
    criteria = contract.get("success_criteria")
    if criteria != _freeze_criteria(criteria):
        raise ContractError("success criteria are not normalized")
    budgets = contract.get("budgets")
    if not isinstance(budgets, dict):
        raise ContractError("contract budgets are invalid")
    review_limits = contract.get("review_limits")
    if not isinstance(review_limits, dict):
        raise ContractError("contract review limits are invalid")
    review_policy = contract.get(
        "review_policy", {"mode": "full", "required_candidate_coverage": True}
    )
    if review_policy not in (
            {"mode": "none", "required_candidate_coverage": False},
            {"mode": "full", "required_candidate_coverage": True}):
        raise ContractError("contract review policy is invalid")
    if (not _plain_nonnegative_int(review_limits.get("max_blocker_revisions")) or
            not _plain_nonnegative_int(review_limits.get("max_blocker_rechecks")) or
            (review_policy["mode"] == "none" and
             (review_limits["max_blocker_revisions"] != 0 or
              review_limits["max_blocker_rechecks"] != 0))):
        raise ContractError("contract review limits are invalid")
    minimum_worker_turns = (len(worker_ids) if review_policy["mode"] == "none" else
                            2 * len(worker_ids) +
                            review_limits["max_blocker_revisions"] +
                            review_limits["max_blocker_rechecks"])
    if (not isinstance(budgets.get("max_worker_turns"), int) or
            isinstance(budgets.get("max_worker_turns"), bool) or
            budgets["max_worker_turns"] < minimum_worker_turns):
        raise ContractError("contract budgets are invalid")
    if (not isinstance(budgets.get("max_worker_turns"), int) or
            isinstance(budgets.get("max_worker_turns"), bool) or
            budgets["max_worker_turns"] < len(worker_ids) or
            not isinstance(budgets.get("max_leader_turns"), int) or
            isinstance(budgets.get("max_leader_turns"), bool) or
            not 2 <= budgets["max_leader_turns"] <= 3):
        raise ContractError("contract budgets are invalid")
    maximum_deadline = budgets.get("max_worker_deadline_seconds")
    if maximum_deadline is not None and (
            not isinstance(maximum_deadline, int) or
            isinstance(maximum_deadline, bool) or maximum_deadline < 60):
        raise ContractError("contract worker deadline is invalid")
    target_elapsed = budgets.get("target_elapsed_seconds")
    if target_elapsed is not None and (
            not isinstance(target_elapsed, int) or isinstance(target_elapsed, bool) or
            target_elapsed < (maximum_deadline or 0)):
        raise ContractError("contract elapsed-time target is invalid")
    provenance = contract.get("mode_provenance")
    if provenance is not None:
        expected_policy = {
            "mode": contract["mode"], "workers": contract["workers"],
            "review_policy": review_policy["mode"],
            "max_worker_turns": budgets["max_worker_turns"],
            "max_review_rounds": (0 if review_policy["mode"] == "none" else 1),
            "max_blocker_revisions": review_limits["max_blocker_revisions"],
            "max_blocker_rechecks": review_limits["max_blocker_rechecks"],
            "worker_deadline_seconds": maximum_deadline,
            "target_elapsed_seconds": target_elapsed,
        }
        capacity_policy = contract.get("capacity_policy")
        if capacity_policy is not None:
            backoff = (capacity_policy or {}).get("runtime_backoff")
            if (not isinstance(capacity_policy, dict) or
                    not isinstance(capacity_policy.get("capacity_floor"), int) or
                    isinstance(capacity_policy.get("capacity_floor"), bool) or
                    not 1 <= capacity_policy["capacity_floor"] <= len(worker_ids) or
                    not isinstance(backoff, dict) or
                    not isinstance(backoff.get("max_steps"), int) or
                    isinstance(backoff.get("max_steps"), bool) or
                    backoff["max_steps"] < 0 or
                    not isinstance(backoff.get("retry_after_seconds"), list) or
                    len(backoff["retry_after_seconds"]) != backoff["max_steps"]):
                raise ContractError("frozen capacity policy is invalid")
            expected_policy["capacity_policy"] = capacity_policy
        if (not isinstance(provenance, dict) or provenance.get("schema_version") != 1 or
                not isinstance(provenance.get("config_sha256"), str) or
                len(provenance["config_sha256"]) != 64 or
                provenance.get("resolved_policy") != expected_policy or
                provenance.get("resolved_policy_sha256") != sha256_json(expected_policy)):
            raise ContractError("frozen mode provenance is invalid")
    delivery = contract.get("delivery_contract")
    if delivery is not None:
        try:
            normalized_delivery = _freeze_delivery_contract(
                {"delivery_contract": delivery}
            )
        except ContractError as exc:
            raise ContractError("frozen delivery contract is invalid") from exc
        if delivery != normalized_delivery:
            raise ContractError("frozen delivery contract is not normalized")
    final_gate = contract.get("final_gate_contract")
    if final_gate is not None:
        _validate_final_gate_contract(final_gate, budgets["max_leader_turns"])
    external_review = contract.get("external_review_contract")
    if external_review is not None:
        if external_review != {
                "default_rounds": 0, "explicit_request_rounds": 1,
                "automatic_recheck": False}:
            raise ContractError("external review contract is invalid")
    if contract.get("turn_counting") != {
            "unit": "reserved_attempt", "refund_on_failure": False,
            "counted_outcomes": ["DONE", "FAILED", "TIMEOUT", "CANCELLED",
                                 "OUTCOME_UNKNOWN"]}:
        raise ContractError("turn counting rule is invalid")
    if contract.get("review_contract") != {
            "verdicts": ["PASS", "ISSUES"],
            "severities": ["BLOCKER", "NON_BLOCKER"],
            "issue_id_pattern": "<candidate_id>-I<two_digit_sequence>",
            "invalid_status": "FORMAT_INVALID", "automatic_model_retry": False}:
        raise ContractError("review contract is invalid")
    master = contract.get("master")
    if (not isinstance(master, dict) or
            not isinstance(master.get("owner_id"), str) or not master["owner_id"] or
            not isinstance(master.get("epoch"), int) or
            isinstance(master.get("epoch"), bool) or master["epoch"] < 1):
        raise ContractError("master authority is invalid")
    if contract.get("initial_phase") != "PLANNED" or contract.get("dispatch_frozen") is not True:
        raise ContractError("contract is not frozen at PLANNED")


def new_runtime(contract: dict) -> dict:
    validate_contract(contract)
    leader_used = 1 if contract["task_plan"]["source"] == "leader" else 0
    return {"schema_version": 3, "run_id": contract["run_id"],
            "contract_sha256": contract["contract_sha256"],
            "phase": contract["initial_phase"], "worker_turns_used": 0,
            "leader_turns_used": leader_used, "terminal_status": None,
            "master": deepcopy(contract["master"]), "revision": 0}


def _plain_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_runtime(runtime: object, contract: dict) -> None:
    if not isinstance(runtime, dict):
        raise ContractError("runtime must be an object")
    if (runtime.get("schema_version") != 3 or
            runtime.get("run_id") != contract["run_id"] or
            runtime.get("contract_sha256") != contract["contract_sha256"] or
            runtime.get("master") != contract["master"]):
        raise ContractError("runtime is not bound to this contract and master epoch")
    phase = runtime.get("phase")
    if phase not in ACTIVE_PHASES + TERMINAL_PHASES:
        raise ContractError("runtime phase is invalid")
    terminal_status = runtime.get("terminal_status")
    if ((phase in TERMINAL_PHASES and terminal_status != phase) or
            (phase in ACTIVE_PHASES and terminal_status is not None)):
        raise ContractError("runtime terminal status is inconsistent")
    if not _plain_nonnegative_int(runtime.get("revision")):
        raise ContractError("runtime revision is invalid")
    for kind in ("worker", "leader"):
        used = runtime.get(f"{kind}_turns_used")
        maximum = contract["budgets"][f"max_{kind}_turns"]
        if not _plain_nonnegative_int(used) or used > maximum:
            raise ContractError(f"runtime {kind} turn count is invalid")


def assert_runtime_revision(runtime: object, contract: dict,
                            authoritative_revision: int) -> None:
    """Reject an old snapshot before storage performs its atomic CAS update."""
    validate_contract(contract)
    _validate_runtime(runtime, contract)
    if (not _plain_nonnegative_int(authoritative_revision) or
            runtime["revision"] != authoritative_revision):
        raise ContractError("stale runtime revision")


def _authorize(contract: dict, owner_id: str, epoch: int) -> None:
    master = contract["master"]
    if (not isinstance(owner_id, str) or not isinstance(epoch, int) or
            isinstance(epoch, bool) or owner_id != master["owner_id"] or
            epoch != master["epoch"]):
        raise ContractError("stale or foreign master authority")


def consume_turn(runtime: dict, contract: dict, kind: str,
                 owner_id: str, epoch: int) -> dict:
    validate_contract(contract)
    _validate_runtime(runtime, contract)
    _authorize(contract, owner_id, epoch)
    if runtime.get("terminal_status") is not None:
        raise ContractError("terminal run cannot reserve another turn")
    if kind not in ("worker", "leader"):
        raise ContractError("turn kind must be worker or leader")
    key = f"{kind}_turns_used"
    maximum = contract["budgets"][f"max_{kind}_turns"]
    if runtime.get(key, 0) >= maximum:
        raise ContractError(f"{kind} turn budget exhausted")
    updated = deepcopy(runtime)
    updated[key] += 1
    updated["revision"] += 1
    return updated


def transition_phase(runtime: dict, contract: dict, next_phase: str,
                     owner_id: str, epoch: int) -> dict:
    validate_contract(contract)
    _validate_runtime(runtime, contract)
    _authorize(contract, owner_id, epoch)
    if not isinstance(next_phase, str):
        raise ContractError("next phase must be text")
    current = runtime.get("phase")
    if next_phase not in LEGAL_TRANSITIONS.get(current, set()):
        raise ContractError(f"illegal phase transition: {current} -> {next_phase}")
    updated = deepcopy(runtime)
    updated["phase"] = next_phase
    updated["revision"] += 1
    if next_phase in TERMINAL_PHASES:
        updated["terminal_status"] = next_phase
    return updated
