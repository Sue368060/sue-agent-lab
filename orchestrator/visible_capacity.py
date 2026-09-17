"""Deterministic pre-dispatch capacity and bounded-backoff decisions."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path

from .short_context_contract import sha256_json
from .visible_modes import DEFAULT_CONFIG, resolve_mode


class CapacityError(ValueError):
    pass


def assess_runtime_capacity(plan: dict, snapshot: dict,
                            backoff_step: int = 0) -> dict:
    """Assess only supplied runtime state; this function never calls a model."""
    workers = plan.get("workers")
    policy = plan.get("capacity_policy")
    if not isinstance(workers, list) or not workers or not isinstance(policy, dict):
        raise CapacityError("resolved plan has no capacity policy")
    worker_ids = [worker.get("worker_id") for worker in workers]
    if any(not isinstance(worker_id, str) or not worker_id for worker_id in worker_ids):
        raise CapacityError("resolved Worker roster is invalid")
    states = snapshot.get("workers") if isinstance(snapshot, dict) else None
    if not isinstance(states, dict) or set(states) != set(worker_ids):
        raise CapacityError("capacity snapshot must cover the frozen Worker roster")
    if not isinstance(backoff_step, int) or isinstance(backoff_step, bool) or backoff_step < 0:
        raise CapacityError("backoff step is invalid")

    allowed_states = {"AVAILABLE", "BUSY", "UNAVAILABLE"}
    normalized = {}
    for worker_id in worker_ids:
        entry = states[worker_id]
        if not isinstance(entry, dict) or entry.get("state") not in allowed_states:
            raise CapacityError(f"invalid capacity state for Worker {worker_id}")
        normalized[worker_id] = {"state": entry["state"]}
        if "observed_at" in entry:
            if not isinstance(entry["observed_at"], (int, float)):
                raise CapacityError(f"invalid observation time for Worker {worker_id}")
            normalized[worker_id]["observed_at"] = entry["observed_at"]

    available = [worker_id for worker_id in worker_ids
                 if normalized[worker_id]["state"] == "AVAILABLE"]
    busy = [worker_id for worker_id in worker_ids
            if normalized[worker_id]["state"] == "BUSY"]
    unavailable = [worker_id for worker_id in worker_ids
                   if normalized[worker_id]["state"] == "UNAVAILABLE"]
    floor = policy.get("capacity_floor")
    backoff = policy.get("runtime_backoff")
    if (not isinstance(floor, int) or isinstance(floor, bool) or
            not 1 <= floor <= len(worker_ids) or not isinstance(backoff, dict)):
        raise CapacityError("capacity policy is invalid")
    schedule = backoff.get("retry_after_seconds")
    max_steps = backoff.get("max_steps")
    if (not isinstance(max_steps, int) or isinstance(max_steps, bool) or
            max_steps < 0 or not isinstance(schedule, list) or
            len(schedule) != max_steps or
            not all(isinstance(value, int) and not isinstance(value, bool) and
                    value > 0 for value in schedule)):
        raise CapacityError("runtime backoff policy is invalid")

    if len(available) == len(worker_ids):
        decision = "ALLOW"
        retry_after = None
    elif busy and backoff_step < max_steps:
        decision = "BACKOFF"
        retry_after = schedule[backoff_step]
    elif len(available) >= floor:
        decision = "DEGRADED"
        retry_after = None
    else:
        decision = "STOP"
        retry_after = None

    body = {
        "schema_version": 1,
        "mode": plan.get("mode"),
        "mode_policy_sha256": plan.get("mode_provenance", {}).get(
            "resolved_policy_sha256"),
        "decision": decision,
        "dispatch_allowed": decision in ("ALLOW", "DEGRADED"),
        "must_finalize_partial": decision == "DEGRADED",
        "capacity_floor": floor,
        "available_worker_ids": available,
        "busy_worker_ids": busy,
        "unavailable_worker_ids": unavailable,
        "backoff_step": backoff_step,
        "retry_after_seconds": retry_after,
        "snapshot": {"workers": deepcopy(normalized)},
    }
    body["report_sha256"] = sha256_json(body)
    return body


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sue-visible-capacity")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--mode")
    parser.add_argument("--backoff-step", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        result = assess_runtime_capacity(
            resolve_mode(config, args.mode), snapshot, args.backoff_step)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
