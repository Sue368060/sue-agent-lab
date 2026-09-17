"""Read token-usage metadata and stop low-cost dispatch into oversized Worker chats."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .visible_modes import DEFAULT_CONFIG, resolve_mode


class CostGuardError(ValueError):
    pass


def _sha256_json(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _usage_record(payload: object) -> dict | None:
    if not isinstance(payload, dict):
        return None
    usage = payload.get("turn_token_usage") or payload.get("usage")
    if not isinstance(usage, dict):
        return None
    fields = ("input_tokens", "cached_input_tokens", "output_tokens",
              "reasoning_output_tokens", "total_tokens")
    if any(not isinstance(usage.get(key), int) or isinstance(usage.get(key), bool)
           or usage[key] < 0 for key in fields):
        return None
    return {key: usage[key] for key in fields}


def read_latest_thread_usage(session_path: Path, thread_id: str) -> dict:
    """Return only the newest token metadata; never copy prompts or answers."""
    path = Path(session_path)
    if not path.is_file() or not isinstance(thread_id, str) or not thread_id:
        raise CostGuardError("session file or thread ID is invalid")
    latest = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = record.get("payload") if isinstance(record, dict) else None
        if (record.get("type") != "token_usage_record" or
                not isinstance(payload, dict) or payload.get("thread_id") != thread_id):
            continue
        usage = _usage_record(payload)
        if usage is None:
            continue
        latest = {"thread_id": thread_id, "turn_id": payload.get("turn_id"),
                  "recorded_at": record.get("timestamp"), **usage,
                  "source": "local_token_usage_record"}
    if latest is None:
        raise CostGuardError("no valid token-usage record for Worker thread")
    return latest


def collect_visible_usage(plan: dict, sessions_root: Path) -> dict:
    root = Path(sessions_root)
    if not root.is_dir():
        raise CostGuardError("sessions root is unavailable")
    found = {}
    missing = []
    for worker in plan["workers"]:
        worker_id = worker["worker_id"]
        thread_id = worker["thread_id"]
        matches = sorted(root.rglob(f"*{thread_id}.jsonl"),
                         key=lambda item: item.stat().st_mtime, reverse=True)
        if not matches:
            missing.append(worker_id)
            continue
        try:
            found[worker_id] = read_latest_thread_usage(matches[0], thread_id)
        except CostGuardError:
            missing.append(worker_id)
    return {"workers": found, "missing_worker_ids": sorted(missing)}


def assess_low_cost_dispatch(plan: dict, usage_snapshot: dict,
                             requested_calls: int) -> dict:
    if (not isinstance(requested_calls, int) or isinstance(requested_calls, bool) or
            requested_calls < 1):
        raise CostGuardError("requested call count is invalid")
    context = plan.get("context_policy")
    policy = context.get("low_cost_guard") if isinstance(context, dict) else None
    if not isinstance(policy, dict):
        raise CostGuardError("low-cost policy is unavailable")
    workers = usage_snapshot.get("workers") if isinstance(usage_snapshot, dict) else None
    missing = usage_snapshot.get("missing_worker_ids") if isinstance(usage_snapshot, dict) else None
    if not isinstance(workers, dict) or not isinstance(missing, list):
        raise CostGuardError("usage snapshot is invalid")
    threshold = policy["max_recent_input_tokens"]
    oversized = sorted(worker_id for worker_id, usage in workers.items()
                       if isinstance(usage, dict) and
                       isinstance(usage.get("input_tokens"), int) and
                       usage["input_tokens"] > threshold)
    max_calls = plan["max_worker_turns"]
    if oversized:
        decision = "ROTATE_REQUIRED"
        allowed = False
        reason = "recent Worker context exceeds the configured low-cost threshold"
    elif missing:
        decision = "CALL_CAP_FALLBACK"
        allowed = requested_calls <= max_calls
        reason = "token usage unavailable; enforce max_llm_calls fallback"
    else:
        decision = "ALLOW"
        allowed = requested_calls <= max_calls
        reason = "recent Worker context is within the low-cost threshold"
    report = {
        "schema_version": 1,
        "decision": decision,
        "dispatch_allowed": allowed,
        "reason": reason,
        "threshold_input_tokens": threshold,
        "requested_calls": requested_calls,
        "max_llm_calls": max_calls,
        "oversized_worker_ids": oversized,
        "missing_worker_ids": sorted(missing),
        "worker_usage": workers,
    }
    report["report_sha256"] = _sha256_json(report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sue-visible-cost-guard")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--mode", default="small")
    parser.add_argument("--sessions-root", type=Path,
                        default=Path.home() / ".codex" / "sessions")
    parser.add_argument("--requested-calls", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        plan = resolve_mode(config, args.mode)
        snapshot = collect_visible_usage(plan, args.sessions_root)
        report = assess_low_cost_dispatch(plan, snapshot, args.requested_calls)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["dispatch_allowed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
