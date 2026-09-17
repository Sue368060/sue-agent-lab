"""Deterministically select one legal next action from a replayed visible run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from .short_context_contract import sha256_json
from .visible_ledger import LedgerError, read_context_state


class NextActionError(ValueError):
    pass


def apply_capacity_guard(action: dict, capacity_report: dict | None) -> dict:
    """Apply a hash-bound capacity decision before any visible dispatch."""
    if capacity_report is None:
        return action
    if not isinstance(capacity_report, dict):
        raise NextActionError("capacity report is invalid")
    supplied = capacity_report.get("report_sha256")
    body = {key: value for key, value in capacity_report.items()
            if key != "report_sha256"}
    if supplied != sha256_json(body):
        raise NextActionError("capacity report hash mismatch")
    decision = capacity_report.get("decision")
    if decision not in ("ALLOW", "BACKOFF", "DEGRADED", "STOP"):
        raise NextActionError("capacity decision is invalid")
    if decision == "ALLOW" or not action.get("type", "").startswith("DISPATCH_"):
        return action
    base = {key: value for key, value in action.items()
            if key not in ("type", "details", "action_id")}
    common = {
        "capacity_report_sha256": supplied,
        "available_worker_ids": capacity_report.get("available_worker_ids", []),
        "busy_worker_ids": capacity_report.get("busy_worker_ids", []),
        "unavailable_worker_ids": capacity_report.get(
            "unavailable_worker_ids", []),
        "capacity_floor": capacity_report.get("capacity_floor"),
    }
    if decision == "BACKOFF":
        base["type"] = "WAIT_FOR_CAPACITY"
        base["details"] = {**common,
                           "backoff_step": capacity_report.get("backoff_step"),
                           "retry_after_seconds": capacity_report.get(
                               "retry_after_seconds")}
    elif decision == "STOP":
        base["type"] = "STOP_FOR_CAPACITY"
        base["details"] = common
    else:
        available = set(capacity_report.get("available_worker_ids", []))
        details = dict(action.get("details", {}))
        if "worker_ids" in details:
            details["worker_ids"] = [worker_id for worker_id in
                                     details["worker_ids"]
                                     if worker_id in available]
            if not details["worker_ids"]:
                base["type"] = "STOP_FOR_CAPACITY"
                base["details"] = common
                base["action_id"] = sha256_json(base)
                return base
        elif details.get("worker_id") not in available:
            base["type"] = "STOP_FOR_CAPACITY"
            base["details"] = common
            base["action_id"] = sha256_json(base)
            return base
        details.update(common)
        details["must_finalize_partial"] = True
        base["type"] = action["type"]
        base["details"] = details
    base["action_id"] = sha256_json(base)
    return base


def apply_cost_guard(action: dict, cost_report: dict | None) -> dict:
    """Replace a dispatch action with a deterministic stop when cost is too high."""
    if cost_report is None:
        return action
    if not isinstance(cost_report, dict):
        raise NextActionError("cost report is invalid")
    supplied = cost_report.get("report_sha256")
    body = {key: value for key, value in cost_report.items()
            if key != "report_sha256"}
    if supplied != sha256_json(body):
        raise NextActionError("cost report hash mismatch")
    allowed = cost_report.get("dispatch_allowed")
    if not isinstance(allowed, bool):
        raise NextActionError("cost report decision is invalid")
    if allowed or not action.get("type", "").startswith("DISPATCH_"):
        return action
    stopped = {key: value for key, value in action.items()
               if key not in ("type", "details", "action_id")}
    stopped["type"] = "STOP_FOR_WORKER_ROTATION"
    stopped["details"] = {
        "reason": cost_report.get("reason"),
        "cost_decision": cost_report.get("decision"),
        "cost_report_sha256": supplied,
        "oversized_worker_ids": cost_report.get("oversized_worker_ids", []),
        "missing_worker_ids": cost_report.get("missing_worker_ids", []),
    }
    stopped["action_id"] = sha256_json(stopped)
    return stopped


def _completed_attempts(state: dict, stage: str) -> list[dict]:
    return [attempt for attempt in state["attempts"]
            if attempt["stage"] == stage and attempt["outcome"] == "COMPLETED"]


def _latest_candidates(state: dict) -> dict[str, str]:
    latest = {}
    for candidate_id, candidate in state["candidates"].items():
        worker_id = candidate["worker_id"]
        current = latest.get(worker_id)
        if current is None or candidate["version"] > current[0]:
            latest[worker_id] = (candidate["version"], candidate_id)
    return {worker_id: details[1] for worker_id, details in sorted(latest.items())}


def _action(state: dict, action_type: str, **details) -> dict:
    body = {
        "schema_version": 1,
        "run_id": state["run_id"],
        "contract_sha256": state["contract_sha256"],
        "ledger_head_sha256": state["ledger_head_sha256"],
        "ledger_revision": state["revision"],
        "type": action_type,
        "details": details,
    }
    body["action_id"] = sha256_json(body)
    return body


def choose_next_action(*, ledger_path: Path,
                       expected_ledger_head: str | None = None,
                       now: float | None = None) -> dict:
    try:
        state = read_context_state(Path(ledger_path))
    except LedgerError as exc:
        raise NextActionError(f"ledger cannot be replayed: {exc}") from exc
    if state["ledger_schema_version"] != 3 or not isinstance(state["contract"], dict):
        raise NextActionError("programmatic next action requires schema-v3 contract")
    if expected_ledger_head is not None and expected_ledger_head != state["ledger_head_sha256"]:
        raise NextActionError("stale ledger head")
    terminal = state["terminal"]
    if terminal is not None:
        return _action(state, "STOP", terminal_status=terminal["status"])
    if state["active_attempts"]:
        return _action(state, "WAIT_FOR_RESULTS",
                       attempt_ids=state["active_attempts"])

    contract = state["contract"]
    cap = contract["budgets"]["max_worker_turns"]
    turns_used = len(state["attempts"])
    current_time = time.time() if now is None else now
    elapsed_target = contract["budgets"].get("target_elapsed_seconds")
    elapsed = max(0, current_time - state["started_at"])
    time_exhausted = elapsed_target is not None and elapsed >= elapsed_target

    completed_candidates = _latest_candidates(state)
    if state["dispatch_closed"]:
        action = "FINALIZE_PARTIAL" if completed_candidates else "ABORT"
        return _action(state, action, reason="dispatch_closed",
                       basis_candidate_ids=sorted(completed_candidates.values()))

    workers = sorted(state["workers"])
    completed_draft_workers = {
        attempt["worker_id"] for attempt in _completed_attempts(state, "draft")
    }
    missing_drafts = [worker for worker in workers
                      if worker not in completed_draft_workers]
    if missing_drafts:
        if time_exhausted or turns_used + len(missing_drafts) > cap:
            action = "FINALIZE_PARTIAL" if completed_candidates else "ABORT"
            return _action(state, action,
                           reason="elapsed_or_turn_budget_exhausted",
                           basis_candidate_ids=sorted(completed_candidates.values()))
        return _action(state, "DISPATCH_DRAFT_BATCH", worker_ids=missing_drafts)

    blockers = state["unresolved_blockers"]
    if blockers:
        blocker_id = sorted(blockers)[0]
        blocker = blockers[blocker_id]
        if time_exhausted or turns_used >= cap:
            return _action(state, "FINALIZE_PARTIAL",
                           reason="blocker_budget_exhausted",
                           unresolved_blocker_ids=sorted(blockers),
                           basis_candidate_ids=sorted(completed_candidates.values()))
        if blocker.get("revised_candidate_id") is None:
            source = state["candidates"][blocker["source_candidate_id"]]
            return _action(state, "DISPATCH_REVISION",
                           worker_id=source["worker_id"], blocker_id=blocker_id,
                           source_candidate_id=blocker["source_candidate_id"])
        return _action(state, "DISPATCH_RECHECK",
                       worker_id=blocker["reviewer_id"], blocker_id=blocker_id,
                       source_candidate_id=blocker["revised_candidate_id"])

    review_mode = (contract.get("review_policy") or {"mode": "full"})["mode"]
    if review_mode == "full":
        reviews = _completed_attempts(state, "review")
        completed_reviewers = {attempt["worker_id"] for attempt in reviews}
        reviewed_candidates = {
            attempt["result"]["source_candidate_id"] for attempt in reviews
        }
        missing_reviewers = [worker for worker in workers
                             if worker not in completed_reviewers]
        if missing_reviewers:
            if time_exhausted or turns_used >= cap:
                return _action(state, "FINALIZE_PARTIAL",
                               reason="review_budget_exhausted",
                               basis_candidate_ids=sorted(completed_candidates.values()))
            reviewer = missing_reviewers[0]
            reviewer_index = workers.index(reviewer)
            ordered_targets = workers[reviewer_index + 1:] + workers[:reviewer_index]
            target = next((worker for worker in ordered_targets
                           if completed_candidates[worker] not in reviewed_candidates),
                          ordered_targets[0])
            return _action(state, "DISPATCH_REVIEW", worker_id=reviewer,
                           source_candidate_id=completed_candidates[target])
        if set(completed_candidates.values()) - reviewed_candidates:
            return _action(state, "ABORT",
                           reason="review_coverage_impossible_without_extra_turns")

    if time_exhausted and not state["done_prerequisites_satisfied"]:
        return _action(state, "FINALIZE_PARTIAL",
                       reason="elapsed_target_reached",
                       basis_candidate_ids=sorted(completed_candidates.values()))
    if state["done_prerequisites_satisfied"]:
        return _action(state, "SYNTHESIZE",
                       basis_candidate_ids=sorted(completed_candidates.values()))
    return _action(state, "ABORT", reason="no_legal_transition")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sue-next-action")
    parser.add_argument("ledger", type=Path)
    parser.add_argument("--expected-ledger-head")
    parser.add_argument("--cost-report", type=Path)
    parser.add_argument("--capacity-report", type=Path)
    args = parser.parse_args(argv)
    try:
        result = choose_next_action(
            ledger_path=args.ledger,
            expected_ledger_head=args.expected_ledger_head)
        if args.capacity_report is not None:
            capacity_report = json.loads(
                args.capacity_report.read_text(encoding="utf-8"))
            result = apply_capacity_guard(result, capacity_report)
        if args.cost_report is not None:
            cost_report = json.loads(args.cost_report.read_text(encoding="utf-8"))
            result = apply_cost_guard(result, cost_report)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
