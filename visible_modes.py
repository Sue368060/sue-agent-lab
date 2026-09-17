"""Resolve the editable visible-Worker roster without starting model turns."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


DEFAULT_CONFIG = Path(__file__).with_name("visible_modes.json")


class ModeConfigError(ValueError):
    pass


def _sha256_json(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def resolve_mode(config: dict, mode: str | None = None,
                 role_overrides: dict[str, str] | None = None) -> dict:
    schema_version = config.get("schema_version")
    if schema_version not in (1, 2):
        raise ModeConfigError("unsupported visible-mode schema")
    workers = config.get("workers")
    modes = config.get("modes")
    if not isinstance(workers, dict) or not isinstance(modes, dict):
        raise ModeConfigError("workers and modes must be objects")
    selected = mode or config.get("default_mode")
    spec = modes.get(selected)
    if not isinstance(spec, dict):
        raise ModeConfigError(f"unknown mode: {selected}")
    ids = spec.get("worker_ids")
    if (not isinstance(ids, list) or not ids or
            not all(isinstance(worker_id, str) for worker_id in ids) or
            len(ids) != len(set(ids))):
        raise ModeConfigError("mode needs distinct worker IDs")
    all_thread_ids = [w.get("thread_id") for w in workers.values() if isinstance(w, dict)]
    if len(all_thread_ids) != len(workers) or len(all_thread_ids) != len(set(all_thread_ids)):
        raise ModeConfigError("worker thread IDs must be unique")
    overrides = role_overrides or {}
    if any(worker_id not in ids or not isinstance(role, str) or not role.strip()
           for worker_id, role in overrides.items()):
        raise ModeConfigError("role override must name an active worker and a nonempty role")
    roster = []
    thinking_overrides = spec.get("thinking_overrides", {})
    if (not isinstance(thinking_overrides, dict) or
            any(worker_id not in ids or not isinstance(value, str) or not value
                for worker_id, value in thinking_overrides.items())):
        raise ModeConfigError("thinking overrides must name active workers")
    for worker_id in ids:
        worker = workers.get(worker_id)
        if not isinstance(worker, dict) or not isinstance(worker.get("thread_id"), str) or not worker["thread_id"]:
            raise ModeConfigError(f"missing thread ID for Worker {worker_id}")
        role = overrides.get(worker_id, worker.get("role"))
        if role is not None and (not isinstance(role, str) or not role.strip()):
            raise ModeConfigError(f"invalid role for Worker {worker_id}")
        model = worker.get("model")
        thinking = thinking_overrides.get(worker_id, worker.get("thinking"))
        if (not isinstance(model, str) or not model or
                not isinstance(thinking, str) or not thinking):
            raise ModeConfigError(f"model request missing for Worker {worker_id}")
        generation = worker.get("generation", 1)
        if (not isinstance(generation, int) or isinstance(generation, bool) or
                generation < 1):
            raise ModeConfigError(f"invalid generation for Worker {worker_id}")
        if schema_version == 2:
            history = worker.get("takeover_history")
            previous = worker.get("previous_thread_ids")
            archive = worker.get("chat_archive_path")
            if (worker.get("exclusive_owner") is not True or
                    not isinstance(history, list) or
                    not isinstance(previous, list) or
                    not all(isinstance(value, str) and value for value in previous) or
                    len(previous) != len(set(previous)) or
                    not isinstance(archive, str) or not archive):
                raise ModeConfigError(
                    f"invalid continuity metadata for Worker {worker_id}")
            valid_history = all(
                isinstance(item, dict) and
                isinstance(item.get("generation"), int) and
                not isinstance(item.get("generation"), bool) and
                item["generation"] >= 2 and
                all(isinstance(item.get(key), str) and item[key]
                    for key in ("from_thread_id", "to_thread_id",
                                "archived_chat_path", "claimed_at", "basis"))
                for item in history)
            generations = [item.get("generation") for item in history
                           if isinstance(item, dict)]
            from_threads = [item.get("from_thread_id") for item in history
                            if isinstance(item, dict)]
            chain_valid = all(
                history[index - 1]["to_thread_id"] ==
                history[index]["from_thread_id"]
                for index in range(1, len(history))) if valid_history else False
            if (not valid_history or len(history) != generation - 1 or
                    len(previous) != generation - 1 or
                    generations != list(range(2, generation + 1)) or
                    from_threads != previous or
                    (history and (not chain_valid or
                                  history[-1]["to_thread_id"] !=
                                  worker["thread_id"]))):
                raise ModeConfigError(
                    f"invalid takeover history for Worker {worker_id}")
        roster.append({"worker_id": worker_id, "thread_id": worker["thread_id"],
                       "role": role, "requested_model": model,
                       "requested_thinking": thinking,
                       "worker_generation": generation})
    turn_cap = spec.get("max_worker_turns")
    review_rounds = spec.get("max_review_rounds")
    revisions = spec.get("max_blocker_revisions")
    rechecks = spec.get("max_blocker_rechecks")
    review_policy = spec.get("review_policy", "full")
    minimum_turns = (len(ids) if review_policy == "none" else
                     2 * len(ids) + revisions + rechecks)
    if (not isinstance(turn_cap, int) or isinstance(turn_cap, bool) or
            review_policy not in ("none", "full") or
            review_rounds != (0 if review_policy == "none" else 1) or
            not isinstance(revisions, int) or isinstance(revisions, bool) or revisions < 0 or
            not isinstance(rechecks, int) or isinstance(rechecks, bool) or
            rechecks != revisions or
            (review_policy == "none" and (revisions != 0 or rechecks != 0)) or
            turn_cap < minimum_turns):
        raise ModeConfigError("invalid turn budget")
    worker_deadline = spec.get("worker_deadline_seconds")
    target_elapsed = spec.get("target_elapsed_seconds")
    if (not isinstance(worker_deadline, int) or isinstance(worker_deadline, bool) or
            worker_deadline < 60 or
            not isinstance(target_elapsed, int) or isinstance(target_elapsed, bool) or
            target_elapsed < worker_deadline):
        raise ModeConfigError("invalid elapsed-time budget")
    capacity_floor = spec.get("capacity_floor")
    runtime_backoff = config.get("runtime_backoff")
    if (not isinstance(capacity_floor, int) or isinstance(capacity_floor, bool) or
            not 1 <= capacity_floor <= len(ids)):
        raise ModeConfigError("invalid capacity floor")
    if (not isinstance(runtime_backoff, dict) or
            not isinstance(runtime_backoff.get("max_steps"), int) or
            isinstance(runtime_backoff.get("max_steps"), bool) or
            runtime_backoff["max_steps"] < 0 or
            not isinstance(runtime_backoff.get("retry_after_seconds"), list) or
            len(runtime_backoff["retry_after_seconds"]) !=
            runtime_backoff["max_steps"] or
            not all(isinstance(value, int) and not isinstance(value, bool) and
                    value > 0 for value in
                    runtime_backoff["retry_after_seconds"]) or
            runtime_backoff.get("busy_state") != "BACKOFF" or
            runtime_backoff.get("unavailable_state") != "DEGRADE_OR_STOP"):
        raise ModeConfigError("invalid runtime backoff policy")
    external_review = config.get("external_review")
    delivery_contract = config.get("delivery_contract")
    context_policy = config.get("context_policy")
    if (not isinstance(external_review, dict) or
            external_review.get("default_rounds") != 0 or
            external_review.get("explicit_request_rounds") != 1 or
            external_review.get("automatic_recheck") is not False):
        raise ModeConfigError("invalid external-review budget")
    if (not isinstance(delivery_contract, dict) or
            delivery_contract.get("inline_full_result") is not True or
            delivery_contract.get("final_artifact_must_be_inline") is not True or
            delivery_contract.get("files_are_supplements") is not True or
            delivery_contract.get("must_cover_every_worker") is not True or
            not isinstance(delivery_contract.get("minimum_inline_chars"), int) or
            isinstance(delivery_contract.get("minimum_inline_chars"), bool) or
            delivery_contract["minimum_inline_chars"] < 300 or
            not isinstance(delivery_contract.get("minimum_final_to_longest_worker_ratio"),
                           (int, float)) or
            isinstance(delivery_contract.get("minimum_final_to_longest_worker_ratio"), bool) or
            not 0.5 <= delivery_contract["minimum_final_to_longest_worker_ratio"] <= 1 or
            not isinstance(delivery_contract.get("minimum_worker_output_chars"), int) or
            isinstance(delivery_contract.get("minimum_worker_output_chars"), bool) or
            delivery_contract["minimum_worker_output_chars"] < 100 or
            not isinstance(delivery_contract.get("minimum_worker_overlap_chars"), int) or
            isinstance(delivery_contract.get("minimum_worker_overlap_chars"), bool) or
            not 16 <= delivery_contract["minimum_worker_overlap_chars"] <= 200 or
            not isinstance(delivery_contract.get("minimum_section_chars"), int) or
            isinstance(delivery_contract.get("minimum_section_chars"), bool) or
            delivery_contract["minimum_section_chars"] < 20 or
            not isinstance(delivery_contract.get("maximum_repeated_ngram_ratio"),
                           (int, float)) or
            isinstance(delivery_contract.get("maximum_repeated_ngram_ratio"), bool) or
            not 0.01 <= delivery_contract["maximum_repeated_ngram_ratio"] <= 0.25 or
            not isinstance(delivery_contract.get("maximum_section_similarity_ratio"),
                           (int, float)) or
            isinstance(delivery_contract.get("maximum_section_similarity_ratio"), bool) or
            not 0.1 <= delivery_contract["maximum_section_similarity_ratio"] <= 0.9 or
            not isinstance(delivery_contract.get("minimum_synthesis_to_longest_worker_ratio"),
                           (int, float)) or
            isinstance(delivery_contract.get("minimum_synthesis_to_longest_worker_ratio"), bool) or
            not 0.1 <= delivery_contract["minimum_synthesis_to_longest_worker_ratio"] <= 1 or
            not isinstance(delivery_contract.get("required_sections"), list) or
            len(delivery_contract["required_sections"]) < 6 or
            len(set(delivery_contract["required_sections"])) !=
            len(delivery_contract["required_sections"]) or
            not isinstance(delivery_contract.get("section_heading_aliases"), dict) or
            set(delivery_contract["section_heading_aliases"]) !=
            set(delivery_contract["required_sections"]) or
            any(not isinstance(values, list) or not values or
                not all(isinstance(value, str) and value.strip() for value in values)
                for values in delivery_contract["section_heading_aliases"].values())):
        raise ModeConfigError("invalid final-delivery contract")
    if (not isinstance(context_policy, dict) or
            not isinstance(context_policy.get("rotation_recommended_after_completed_turns"), int) or
            context_policy["rotation_recommended_after_completed_turns"] < 1 or
            context_policy.get("send_self_contained_prompt") is not True or
            context_policy.get("do_not_relay_full_peer_transcript") is not True):
        raise ModeConfigError("invalid context policy")
    low_cost_guard = context_policy.get("low_cost_guard")
    if (not isinstance(low_cost_guard, dict) or
            low_cost_guard.get("enabled_on_explicit_low_cost_request") is not True or
            not isinstance(low_cost_guard.get("max_recent_input_tokens"), int) or
            isinstance(low_cost_guard.get("max_recent_input_tokens"), bool) or
            low_cost_guard["max_recent_input_tokens"] < 1000 or
            low_cost_guard.get("on_exceed") != "rotate_worker_task" or
            low_cost_guard.get("usage_unavailable_fallback") != "max_llm_calls"):
        raise ModeConfigError("invalid low-cost context guard")
    if config.get("auto_retry") != 0 or config.get("auto_model_fallback") is not False:
        raise ModeConfigError("automatic retry and fallback must stay disabled")
    if config.get("final_owner") != "main_task":
        raise ModeConfigError("the main task must own the final result")
    result = {"mode": selected, "workers": roster, "review_policy": review_policy,
            "max_worker_turns": turn_cap,
            "max_review_rounds": review_rounds, "max_blocker_revisions": revisions,
            "max_blocker_rechecks": rechecks,
            "worker_deadline_seconds": worker_deadline,
            "target_elapsed_seconds": target_elapsed,
            "capacity_policy": {
                "capacity_floor": capacity_floor,
                "runtime_backoff": runtime_backoff,
            },
            "external_review": external_review,
            "delivery_contract": delivery_contract,
            "context_policy": context_policy,
            "final_owner": config.get("final_owner")}
    policy = {key: result[key] for key in (
        "mode", "workers", "review_policy", "max_worker_turns",
        "max_review_rounds", "max_blocker_revisions", "max_blocker_rechecks",
        "worker_deadline_seconds", "target_elapsed_seconds")}
    policy["capacity_policy"] = result["capacity_policy"]
    result["mode_provenance"] = {
        "schema_version": 1,
        "config_sha256": _sha256_json(config),
        "resolved_policy": policy,
        "resolved_policy_sha256": _sha256_json(policy),
    }
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sue-visible-mode")
    parser.add_argument("mode", nargs="?", help="small, large, small_reviewed, or large_reviewed")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--role", action="append", default=[], metavar="WORKER=ROLE",
                        help="one-run role override, e.g. A=research")
    args = parser.parse_args(argv)
    overrides = {}
    for entry in args.role:
        if "=" not in entry:
            parser.error("--role requires WORKER=ROLE")
        worker_id, role = entry.split("=", 1)
        overrides[worker_id] = role
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        result = resolve_mode(config, args.mode, overrides)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
