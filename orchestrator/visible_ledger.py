"""Append-only run ledger for visible Worker coordination.

This module never starts a model turn.  The main task records a reservation
before dispatch and binds the exact completed turn afterwards.
"""

from __future__ import annotations

from copy import deepcopy
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import time
import uuid

from .delivery_contract import DeliveryError, validate_delivery
from .final_gate import FinalGateError, validate_final_gate
from .short_context_contract import ContractError, validate_contract


class LedgerError(RuntimeError):
    pass


def _now() -> float:
    return time.time()


def _valid_sha256(value: str | None) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _valid_uuid(value: str | None) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def _event_hash(event: dict) -> str:
    payload = {key: value for key, value in event.items() if key != "event_hash"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _next_candidate_version(state: dict, worker_id: str) -> int:
    versions = [candidate["version"] for candidate in state["candidates"].values()
                if candidate["worker_id"] == worker_id]
    return max(versions, default=0) + 1


def _load(handle) -> list[dict]:
    handle.seek(0)
    events = []
    for line_number, line in enumerate(handle, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"invalid ledger JSON at line {line_number}") from exc
        if not isinstance(event, dict):
            raise LedgerError(f"invalid ledger event at line {line_number}")
        if event.get("seq") != len(events) + 1:
            raise LedgerError("ledger sequence is not contiguous")
        expected_previous = events[-1]["event_hash"] if events else "0" * 64
        if event.get("prev_hash") != expected_previous:
            raise LedgerError("ledger hash chain is broken")
        if not _valid_sha256(event.get("event_hash")) or event["event_hash"] != _event_hash(event):
            raise LedgerError("ledger event hash mismatch")
        events.append(event)
    return events


def _append(path: Path, build_event, *, owner_id: str | None = None,
            master_epoch: int | None = None,
            expected_revision: int | None = None,
            creating: bool = False) -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise LedgerError("ledger path cannot be a symlink")
    mode = "a+" if creating else "r+"
    try:
        handle_context = path.open(mode, encoding="utf-8")
    except FileNotFoundError as exc:
        raise LedgerError("ledger does not exist") from exc
    with handle_context as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        events = _load(handle)
        if creating:
            if events:
                raise LedgerError("ledger already exists")
        else:
            state = _state(events)
            if state["ledger_schema_version"] < 3:
                raise LedgerError("legacy schema is read-only")
            if (not isinstance(owner_id, str) or not owner_id.strip() or
                    not isinstance(master_epoch, int) or
                    isinstance(master_epoch, bool) or master_epoch < 1 or
                    owner_id != state["master_owner_id"] or
                    master_epoch != state["master_epoch"]):
                raise LedgerError("stale or foreign master authority")
            if (not isinstance(expected_revision, int) or
                    isinstance(expected_revision, bool) or
                    expected_revision != state["revision"]):
                raise LedgerError("ledger revision compare-and-swap failed")
        event = build_event(events)
        if creating:
            if (event.get("ledger_schema_version") != 3 or
                    event.get("revision") != 0):
                raise LedgerError("new ledger must start with schema v3 revision 0")
        else:
            event = {**event, "master_owner_id": owner_id,
                     "master_epoch": master_epoch,
                     "revision": expected_revision + 1}
        event = {"seq": len(events) + 1,
                 "prev_hash": events[-1]["event_hash"] if events else "0" * 64,
                 **event}
        event["event_hash"] = _event_hash(event)
        handle.seek(0, os.SEEK_END)
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        return event


def _state_impl(events: list[dict]) -> dict:
    if not events or events[0].get("type") != "run_started":
        raise LedgerError("ledger has no run_started event")
    start = events[0]
    run_id = start.get("run_id")
    schema_version = start.get("ledger_schema_version", 1)
    if schema_version not in (1, 2, 3):
        raise LedgerError("unsupported ledger schema")
    workers = start.get("workers")
    cap = start.get("max_worker_turns")
    revisions = start.get("max_blocker_revisions")
    rechecks = start.get("max_blocker_rechecks")
    start_review_mode = "full"
    if schema_version >= 3 and isinstance(start.get("contract"), dict):
        policy = start["contract"].get("review_policy")
        if isinstance(policy, dict):
            start_review_mode = policy.get("mode", "full")
    minimum_turns = (len(workers) if isinstance(workers, dict) and
                     start_review_mode == "none" else
                     2 * len(workers) + revisions + rechecks
                     if isinstance(workers, dict) and isinstance(revisions, int) and
                     isinstance(rechecks, int) else 0)
    if (not isinstance(run_id, str) or not run_id.strip() or
            not isinstance(start.get("at"), (int, float)) or
            isinstance(start.get("at"), bool) or
            not isinstance(start.get("mode"), str) or not start["mode"].strip() or
            not isinstance(workers, dict) or not workers or
            not all(isinstance(key, str) and key and isinstance(value, str) and value
                    for key, value in workers.items()) or
            len(set(workers.values())) != len(workers) or
            not isinstance(cap, int) or isinstance(cap, bool) or
            not isinstance(revisions, int) or isinstance(revisions, bool) or revisions < 0 or
            not isinstance(rechecks, int) or isinstance(rechecks, bool) or rechecks < 0 or
            cap < minimum_turns or
            start.get("final_owner") != "main_task"):
        raise LedgerError("invalid run_started event")
    if schema_version >= 3:
        frozen_contract = start.get("contract")
        try:
            validate_contract(frozen_contract)
        except ContractError as exc:
            raise LedgerError("embedded schema-v3 contract is invalid") from exc
        expected_roster = {worker["worker_id"]: worker["thread_id"]
                           for worker in frozen_contract["workers"]}
        if (not isinstance(start.get("master_owner_id"), str) or
                not start["master_owner_id"].strip() or
                not isinstance(start.get("master_epoch"), int) or
                isinstance(start.get("master_epoch"), bool) or
                start["master_epoch"] < 1 or
                not isinstance(start.get("revision"), int) or
                isinstance(start.get("revision"), bool) or
                start["revision"] != 0 or
                start.get("contract_sha256") != frozen_contract["contract_sha256"] or
                start.get("run_id") != frozen_contract["run_id"] or
                start.get("mode") != frozen_contract["mode"] or
                start.get("workers") != expected_roster or
                start.get("max_worker_turns") !=
                frozen_contract["budgets"]["max_worker_turns"] or
                start.get("max_blocker_revisions") !=
                frozen_contract["review_limits"]["max_blocker_revisions"] or
                start.get("max_blocker_rechecks") !=
                frozen_contract["review_limits"]["max_blocker_rechecks"] or
                start.get("final_owner") != frozen_contract["final_owner"] or
                start.get("master_owner_id") != frozen_contract["master"]["owner_id"] or
                start.get("master_epoch") != frozen_contract["master"]["epoch"]):
            raise LedgerError("invalid schema-v3 run authority")
    else:
        frozen_contract = None
    state = {
        "run_id": run_id,
        "ledger_schema_version": schema_version,
        "contract_sha256": start.get("contract_sha256"),
        "contract": frozen_contract,
        "master_owner_id": start.get("master_owner_id"),
        "master_epoch": start.get("master_epoch"),
        "revision": 0 if schema_version >= 3 else None,
        "mode": start.get("mode"),
        "workers": workers,
        "max_worker_turns": cap,
        "max_blocker_revisions": revisions,
        "max_blocker_rechecks": rechecks,
        "final_owner": start.get("final_owner"),
        "attempts": {},
        "turn_ids": set(),
        "candidates": {},
        "unresolved_blockers": {},
        "revised_blockers": set(),
        "rechecked_blockers": set(),
        "issue_ids": set(),
        "dispatch_closed": False,
        "external_reviews": [],
        "terminal": None,
    }
    for event in events[1:]:
        if event.get("run_id") != run_id:
            raise LedgerError("mixed run IDs in ledger")
        if (not isinstance(event.get("at"), (int, float)) or
                isinstance(event.get("at"), bool)):
            raise LedgerError("ledger event time is invalid")
        if schema_version >= 3:
            event_owner = event.get("master_owner_id")
            event_epoch = event.get("master_epoch")
            event_revision = event.get("revision")
            if (not isinstance(event_owner, str) or not event_owner or
                    event_owner != state["master_owner_id"] or
                    not isinstance(event_epoch, int) or isinstance(event_epoch, bool) or
                    event_epoch < 1 or event_epoch != state["master_epoch"] or
                    not isinstance(event_revision, int) or
                    isinstance(event_revision, bool) or
                    event_revision != state["revision"] + 1):
                raise LedgerError("ledger fencing or revision chain is broken")
            state["revision"] = event_revision
        kind = event.get("type")
        if state["terminal"] is not None:
            raise LedgerError("ledger contains an event after terminal state")
        if kind == "turn_reserved":
            attempt_id = event.get("attempt_id")
            if attempt_id in state["attempts"]:
                raise LedgerError("duplicate attempt ID in ledger")
            maximum_deadline = (((state["contract"] or {}).get("budgets") or {})
                                .get("max_worker_deadline_seconds"))
            if (not isinstance(attempt_id, str) or not attempt_id or
                    (schema_version >= 3 and
                     attempt_id != f"{run_id}-T{len(state['attempts']) + 1:02d}") or
                    state["dispatch_closed"] or
                    event.get("worker_id") not in state["workers"] or
                    event.get("thread_id") != state["workers"][event["worker_id"]] or
                    event.get("stage") not in ("draft", "review", "revision", "recheck") or
                    not isinstance(event.get("deadline_at"), (int, float)) or
                    isinstance(event.get("deadline_at"), bool) or
                    event["deadline_at"] <= event["at"] or
                    (maximum_deadline is not None and
                     event["deadline_at"] - event["at"] > maximum_deadline) or
                    len(state["attempts"]) >= state["max_worker_turns"]):
                raise LedgerError("invalid turn reservation in ledger")
            worker_id = event["worker_id"]
            stage = event["stage"]
            source_candidate_id = event.get("source_candidate_id")
            blocker_id = event.get("blocker_id")
            prior = list(state["attempts"].values())
            if any(value["worker_id"] == worker_id and value["outcome"] is None
                   for value in prior):
                raise LedgerError("worker has overlapping reservations in ledger")
            if stage == "draft":
                valid_stage = (source_candidate_id is None and blocker_id is None and
                               not any(value["stage"] == "draft" and
                                       value["worker_id"] == worker_id for value in prior))
            elif stage == "review":
                source = state["candidates"].get(source_candidate_id)
                review_mode = ((state["contract"] or {}).get("review_policy") or
                               {"mode": "full"}).get("mode", "full")
                valid_stage = (review_mode == "full" and source is not None and
                               source["worker_id"] != worker_id and
                               blocker_id is None and
                               not any(value["stage"] == "review" and
                                       value["worker_id"] == worker_id for value in prior))
            elif stage == "revision":
                source = state["candidates"].get(source_candidate_id)
                blocker = state["unresolved_blockers"].get(blocker_id)
                valid_stage = (source is not None and source["worker_id"] == worker_id and
                               blocker is not None and
                               blocker["source_candidate_id"] == source_candidate_id and
                               blocker_id not in state["revised_blockers"] and
                               sum(value["stage"] == "revision" for value in prior) <
                               state["max_blocker_revisions"])
            else:
                source = state["candidates"].get(source_candidate_id)
                blocker = state["unresolved_blockers"].get(blocker_id)
                valid_stage = (source is not None and blocker is not None and
                               source_candidate_id == blocker.get("revised_candidate_id") and
                               worker_id == blocker.get("reviewer_id") and
                               source["worker_id"] != worker_id and
                               blocker_id not in state["rechecked_blockers"] and
                               sum(value["stage"] == "recheck" for value in prior) <
                               state["max_blocker_rechecks"])
            if not valid_stage:
                raise LedgerError("invalid reservation stage binding in ledger")
            state["attempts"][attempt_id] = {**event, "outcome": None}
        elif kind == "turn_completed":
            attempt = state["attempts"].get(event.get("attempt_id"))
            if attempt is None or attempt["outcome"] is not None:
                raise LedgerError("completion does not bind one reserved attempt")
            turn_id = event.get("turn_id")
            if (not _valid_uuid(turn_id) or turn_id in state["turn_ids"] or
                    event.get("thread_id") != attempt["thread_id"] or
                    not _valid_sha256(event.get("sha256")) or
                    event["at"] > attempt["deadline_at"]):
                raise LedgerError("invalid or late turn completion in ledger")
            state["turn_ids"].add(turn_id)
            attempt["outcome"] = "COMPLETED"
            attempt["result"] = event
            if attempt["stage"] in ("draft", "revision"):
                candidate_id = event.get("candidate_id")
                candidate_version = event.get("candidate_version")
                expected_version = _next_candidate_version(state, attempt["worker_id"])
                if (not isinstance(candidate_id, str) or not candidate_id or
                        candidate_id in state["candidates"] or
                        not isinstance(candidate_version, int) or
                        isinstance(candidate_version, bool) or
                        candidate_version != expected_version or
                        event.get("verdict") is not None or
                        event.get("issues") not in (None, [])):
                    raise LedgerError("invalid candidate identity or version in ledger")
                state["candidates"][candidate_id] = {
                    "worker_id": attempt["worker_id"],
                    "version": candidate_version,
                    "sha256": event["sha256"],
                }
            if attempt["stage"] in ("review", "recheck"):
                verdict = event.get("verdict")
                issues = event.get("issues")
                if (verdict not in ("PASS", "ISSUES") or not isinstance(issues, list) or
                        (verdict == "PASS" and issues) or
                        (verdict == "ISSUES" and not issues) or
                        event.get("source_candidate_id") != attempt["source_candidate_id"] or
                        (attempt["stage"] == "recheck" and
                         event.get("blocker_id") != attempt["blocker_id"])):
                    raise LedgerError("invalid review completion in ledger")
                local_issue_ids = set()
                for issue in issues:
                    if (not isinstance(issue, dict) or
                            not isinstance(issue.get("id"), str) or not issue["id"] or
                            issue["id"] in local_issue_ids or
                            issue.get("severity") not in ("BLOCKER", "NON_BLOCKER") or
                            not isinstance(issue.get("problem"), str) or
                            not issue["problem"].strip()):
                        raise LedgerError("invalid review issue in ledger")
                    local_issue_ids.add(issue["id"])
                    if issue["id"] in state["issue_ids"]:
                        raise LedgerError("duplicate issue ID in ledger")
                    state["issue_ids"].add(issue["id"])
                    if issue["severity"] == "BLOCKER":
                        state["unresolved_blockers"][issue["id"]] = {
                            "source_candidate_id": attempt["source_candidate_id"],
                            "problem": issue["problem"],
                            "reviewer_id": attempt["worker_id"],
                            "revised_candidate_id": None,
                        }
            if attempt["stage"] == "revision":
                blocker_id = attempt["blocker_id"]
                state["revised_blockers"].add(blocker_id)
                state["unresolved_blockers"][blocker_id]["revised_candidate_id"] = event["candidate_id"]
            if attempt["stage"] == "recheck":
                blocker_id = attempt["blocker_id"]
                state["rechecked_blockers"].add(blocker_id)
                if event["verdict"] == "PASS":
                    state["unresolved_blockers"].pop(blocker_id, None)
                else:
                    state["dispatch_closed"] = True
        elif kind == "turn_failed":
            attempt = state["attempts"].get(event.get("attempt_id"))
            if attempt is None or attempt["outcome"] is not None:
                raise LedgerError("failure does not bind one reserved attempt")
            if (event.get("status") not in ("TIMEOUT", "FAILED", "UNAVAILABLE") or
                    not isinstance(event.get("reason"), str) or not event["reason"].strip() or
                    (event["status"] == "TIMEOUT" and event["at"] < attempt["deadline_at"])):
                raise LedgerError("invalid turn failure in ledger")
            attempt["outcome"] = event.get("status")
            attempt["result"] = event
            state["dispatch_closed"] = True
        elif kind == "turn_rejected":
            attempt = state["attempts"].get(event.get("attempt_id"))
            turn_id = event.get("turn_id")
            if (attempt is None or attempt["outcome"] is not None or
                    event.get("thread_id") != attempt["thread_id"] or
                    not _valid_uuid(turn_id) or turn_id in state["turn_ids"] or
                    not _valid_sha256(event.get("sha256")) or
                    event.get("reason_code") not in ("late_result", "semantic_invalid") or
                    not isinstance(event.get("reason"), str) or not event["reason"].strip()):
                raise LedgerError("invalid rejected turn result in ledger")
            if ((event["reason_code"] == "late_result" and
                 event["at"] <= attempt["deadline_at"]) or
                    (event["reason_code"] == "semantic_invalid" and
                     event["at"] > attempt["deadline_at"])):
                raise LedgerError("rejected turn reason does not match timing")
            state["turn_ids"].add(turn_id)
            attempt["outcome"] = "REJECTED"
            attempt["result"] = event
            state["dispatch_closed"] = True
        elif kind == "run_cancelled":
            active = sorted(key for key, value in state["attempts"].items()
                            if value["outcome"] is None)
            if (state["terminal"] is not None or event.get("owner") != "main_task" or
                    event["owner"] != state["final_owner"] or
                    not isinstance(event.get("reason"), str) or not event["reason"].strip() or
                    event.get("active_attempt_ids") != active or event.get("status") != "ABORT"):
                raise LedgerError("invalid run cancellation in ledger")
            for attempt_id in active:
                state["attempts"][attempt_id]["outcome"] = "CANCELLED"
                state["attempts"][attempt_id]["result"] = event
            state["terminal"] = event
            state["dispatch_closed"] = True
        elif kind == "external_review_recorded":
            policy = (state["contract"] or {}).get("external_review_contract")
            review_id = event.get("review_id")
            user_authorized = event.get("user_authorized")
            maximum = (policy.get("explicit_request_rounds") if user_authorized is True
                       else policy.get("default_rounds")) if isinstance(policy, dict) else None
            if (not isinstance(policy, dict) or not isinstance(review_id, str) or
                    not review_id.strip() or
                    any(item["review_id"] == review_id for item in state["external_reviews"]) or
                    not isinstance(event.get("provider"), str) or
                    not event["provider"].strip() or
                    not _valid_sha256(event.get("artifact_sha256")) or
                    not isinstance(user_authorized, bool) or
                    not isinstance(maximum, int) or
                    len(state["external_reviews"]) >= maximum):
                raise LedgerError("invalid or over-budget external review record")
            state["external_reviews"].append(event)
        elif kind == "run_finalized":
            if state["terminal"] is not None:
                raise LedgerError("run finalized more than once")
            if (event.get("owner") != "main_task" or
                    event["owner"] != state["final_owner"] or
                    event.get("status") not in ("DONE", "PARTIAL", "ABORT") or
                    any(value["outcome"] is None for value in state["attempts"].values()) or
                    (event["status"] in ("DONE", "PARTIAL") and
                     not _valid_sha256(event.get("result_sha256")))):
                raise LedgerError("invalid run finalization in ledger")
            if event["status"] == "DONE" and not _done_satisfied(state):
                raise LedgerError("stored DONE acceptance is not satisfied")
            _validate_final_basis(state, event["status"], event.get("basis_candidate_ids"))
            delivery_contract = (state["contract"] or {}).get("delivery_contract")
            final_gate_contract = (state["contract"] or {}).get("final_gate_contract")
            if event["status"] in ("DONE", "PARTIAL") and delivery_contract is not None:
                receipt = event.get("delivery_receipt")
                basis_ids = event.get("basis_candidate_ids") or []
                basis_workers = sorted({state["candidates"][candidate_id]["worker_id"]
                                        for candidate_id in basis_ids})
                worker_hashes = receipt.get("worker_output_sha256") if isinstance(receipt, dict) else None
                hardened_delivery = (
                    "maximum_section_similarity_ratio" in delivery_contract and
                    "minimum_synthesis_to_longest_worker_ratio" in delivery_contract
                )
                expected_validator_version = 3 if hardened_delivery else 2
                if (not isinstance(receipt, dict) or receipt.get("status") != "PASS" or
                        receipt.get("validator_version") != expected_validator_version or
                        receipt.get("final_artifact_sha256") != event.get("result_sha256") or
                        not _valid_sha256(receipt.get("final_message_sha256")) or
                        receipt.get("covered_workers") != basis_workers or
                        receipt.get("required_sections") !=
                        delivery_contract["required_sections"] or
                        not isinstance(receipt.get("final_chars"), int) or
                        receipt["final_chars"] < delivery_contract["minimum_inline_chars"] or
                        not isinstance(worker_hashes, dict) or
                        set(worker_hashes) != set(basis_workers) or
                        any(not _valid_sha256(value) for value in worker_hashes.values()) or
                        (hardened_delivery and (
                            not isinstance(receipt.get("maximum_section_similarity_observed"),
                                           (int, float)) or
                            not isinstance(receipt.get("synthesis_novel_chars"), int) or
                            not isinstance(receipt.get("minimum_synthesis_novel_chars"), int) or
                            receipt["synthesis_novel_chars"] <
                            receipt["minimum_synthesis_novel_chars"]))):
                    raise LedgerError("stored delivery receipt is invalid")
                for worker_id, output_hash in worker_hashes.items():
                    candidate_hashes = {
                        state["candidates"][candidate_id]["sha256"]
                        for candidate_id in basis_ids
                        if state["candidates"][candidate_id]["worker_id"] == worker_id
                    }
                    if output_hash not in candidate_hashes:
                        raise LedgerError("stored Worker output is not bound to final basis")
                gate_receipt = event.get("final_gate_receipt")
                if final_gate_contract is not None:
                    expected_gate_hash = None
                    if isinstance(gate_receipt, dict):
                        gate_body = {key: value for key, value in gate_receipt.items()
                                     if key != "receipt_sha256"}
                        expected_gate_hash = hashlib.sha256(
                            json.dumps(gate_body, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")
                        ).hexdigest()
                    if (not isinstance(gate_receipt, dict) or
                            gate_receipt.get("status") != "PASS" or
                            gate_receipt.get("final_gate_version") != 2 or
                            gate_receipt.get("ledger_head_sha256") != event.get("prev_hash") or
                            gate_receipt.get("final_artifact_sha256") != event.get("result_sha256") or
                            gate_receipt.get("final_message_sha256") !=
                            receipt.get("final_message_sha256") or
                            gate_receipt.get("delivery_receipt") != receipt or
                            not isinstance(gate_receipt.get("final_revision"), int) or
                            isinstance(gate_receipt.get("final_revision"), bool) or
                            not 0 <= gate_receipt["final_revision"] <=
                            final_gate_contract["max_final_revisions"] or
                            not isinstance(gate_receipt.get("claim_ids"), list) or
                            not gate_receipt["claim_ids"] or
                            len(gate_receipt["claim_ids"]) !=
                            len(set(gate_receipt["claim_ids"])) or
                            any(not isinstance(value, str) or not value
                                for value in gate_receipt["claim_ids"]) or
                            any(not _valid_sha256(gate_receipt.get(key)) for key in
                                ("claims_sha256", "machine_results_sha256",
                                 "semantic_results_sha256", "receipt_sha256")) or
                            gate_receipt.get("receipt_sha256") != expected_gate_hash):
                        raise LedgerError("stored final gate receipt is invalid")
                elif gate_receipt is not None:
                    raise LedgerError("unexpected final gate receipt")
            elif event.get("delivery_receipt") is not None:
                raise LedgerError("unexpected delivery receipt")
            elif event.get("final_gate_receipt") is not None:
                raise LedgerError("unexpected final gate receipt")
            state["terminal"] = event
            state["dispatch_closed"] = True
        else:
            raise LedgerError(f"unknown ledger event: {kind}")
    return state


def _state(events: list[dict]) -> dict:
    try:
        return _state_impl(events)
    except LedgerError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, IndexError) as exc:
        raise LedgerError("invalid ledger event structure") from exc


def _done_satisfied(state: dict) -> bool:
    failures = [value for value in state["attempts"].values()
                if value["outcome"] not in (None, "COMPLETED")]
    completed_drafts = {value["worker_id"] for value in state["attempts"].values()
                        if value["stage"] == "draft" and value["outcome"] == "COMPLETED"}
    completed_reviews = {value["worker_id"] for value in state["attempts"].values()
                         if value["stage"] == "review" and value["outcome"] == "COMPLETED"}
    draft_candidates = {value["result"]["candidate_id"] for value in state["attempts"].values()
                        if value["stage"] == "draft" and value["outcome"] == "COMPLETED"}
    reviewed_candidates = {value["result"]["source_candidate_id"]
                           for value in state["attempts"].values()
                           if value["stage"] == "review" and value["outcome"] == "COMPLETED"}
    review_mode = ((state["contract"] or {}).get("review_policy") or
                   {"mode": "full"}).get("mode", "full")
    drafts_satisfied = completed_drafts == set(state["workers"])
    if review_mode == "none":
        reviews_satisfied = not completed_reviews
    else:
        reviews_satisfied = (completed_reviews == set(state["workers"]) and
                             draft_candidates <= reviewed_candidates)
    return (not failures and not state["unresolved_blockers"] and
            drafts_satisfied and reviews_satisfied)


def _validate_final_basis(state: dict, status: str,
                          basis_candidate_ids: list[str] | None) -> None:
    """Bind schema-v2 final output to known, current candidate artifacts."""
    if state["ledger_schema_version"] < 2:
        return
    if status == "ABORT":
        if basis_candidate_ids not in (None, []):
            raise LedgerError("ABORT cannot declare final basis candidates")
        return
    if (not isinstance(basis_candidate_ids, list) or not basis_candidate_ids or
            len(basis_candidate_ids) != len(set(basis_candidate_ids)) or
            not all(isinstance(item, str) and item in state["candidates"]
                    for item in basis_candidate_ids)):
        raise LedgerError("final basis must contain distinct known candidates")
    if status == "PARTIAL":
        return
    latest_by_worker = {}
    for candidate_id, candidate in state["candidates"].items():
        worker_id = candidate["worker_id"]
        current = latest_by_worker.get(worker_id)
        if current is None or candidate["version"] > current["version"]:
            latest_by_worker[worker_id] = {"version": candidate["version"], "ids": {candidate_id}}
        elif candidate["version"] == current["version"]:
            current["ids"].add(candidate_id)
    provided = set(basis_candidate_ids)
    if (set(latest_by_worker) != set(state["workers"]) or
            any(not (details["ids"] & provided) for details in latest_by_worker.values())):
        raise LedgerError("DONE basis must include every Worker's latest candidate")


def _read_events(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        raise LedgerError("ledger does not exist")
    with path.open("r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        return _load(handle)


def read_run(path: Path) -> dict:
    events = _read_events(path)
    state = _state(events)
    terminal = state["terminal"]
    basis = terminal.get("basis_candidate_ids") if terminal else None
    if not terminal or terminal["status"] == "ABORT":
        basis_status = None
    elif state["ledger_schema_version"] < 2:
        basis_status = "LEGACY_UNBOUND"
    else:
        basis_status = "BOUND"
    return {
        "run_id": state["run_id"],
        "started_at": events[0]["at"],
        "ledger_schema_version": state["ledger_schema_version"],
        "contract_sha256": state["contract_sha256"],
        "frozen_contract": deepcopy(state["contract"]),
        "master_owner_id": state["master_owner_id"],
        "master_epoch": state["master_epoch"],
        "revision": state["revision"],
        "writable": state["ledger_schema_version"] == 3,
        "ledger_head_sha256": events[-1]["event_hash"],
        "mode": state["mode"],
        "worker_turns": len(state["attempts"]),
        "max_worker_turns": state["max_worker_turns"],
        "active_attempts": sorted(
            key for key, value in state["attempts"].items() if value["outcome"] is None
        ),
        "rejected_attempts": sorted(
            key for key, value in state["attempts"].items() if value["outcome"] == "REJECTED"
        ),
        "unresolved_blockers": sorted(state["unresolved_blockers"]),
        "dispatch_closed": state["dispatch_closed"],
        "status": terminal["status"] if terminal else "RUNNING",
        "final_basis_candidate_ids": basis,
        "final_basis_status": basis_status,
        "delivery_status": ((terminal.get("delivery_receipt") or {}).get("status")
                            if terminal else None),
        "final_gate_status": ((terminal.get("final_gate_receipt") or {}).get("status")
                              if terminal else None),
        "external_review_rounds": len(state["external_reviews"]),
    }


def read_context_state(path: Path) -> dict:
    """Return a deterministic, JSON-safe replay view for short context building."""
    events = _read_events(path)
    state = _state(events)
    attempts = []
    for attempt_id in sorted(state["attempts"]):
        attempt = state["attempts"][attempt_id]
        item = {
            "attempt_id": attempt_id,
            "worker_id": attempt["worker_id"],
            "stage": attempt["stage"],
            "outcome": attempt["outcome"],
            "source_candidate_id": attempt.get("source_candidate_id"),
            "blocker_id": attempt.get("blocker_id"),
        }
        result = attempt.get("result")
        if isinstance(result, dict):
            item["result"] = {
                key: deepcopy(result[key]) for key in (
                    "candidate_id", "candidate_version", "sha256", "verdict",
                    "issues", "source_candidate_id", "blocker_id",
                    "reason_code", "reason", "status"
                ) if key in result
            }
        attempts.append(item)
    return {
        "run_id": state["run_id"],
        "started_at": events[0]["at"],
        "ledger_schema_version": state["ledger_schema_version"],
        "contract_sha256": state["contract_sha256"],
        "contract": deepcopy(state["contract"]),
        "ledger_head_sha256": events[-1]["event_hash"],
        "revision": state["revision"],
        "mode": state["mode"],
        "workers": deepcopy(state["workers"]),
        "attempts": attempts,
        "candidates": {key: deepcopy(state["candidates"][key])
                       for key in sorted(state["candidates"])},
        "unresolved_blockers": {
            key: deepcopy(state["unresolved_blockers"][key])
            for key in sorted(state["unresolved_blockers"])
        },
        "active_attempts": sorted(
            key for key, value in state["attempts"].items()
            if value["outcome"] is None
        ),
        "dispatch_closed": state["dispatch_closed"],
        "external_reviews": deepcopy(state["external_reviews"]),
        "done_prerequisites_satisfied": _done_satisfied(state),
        "terminal": deepcopy(state["terminal"]),
    }


def start_run(path: Path, contract: dict, at: float | None = None, *,
              owner_id: str, master_epoch: int,
              expected_revision: int) -> dict:
    try:
        validate_contract(contract)
    except ContractError as exc:
        raise LedgerError(f"invalid frozen contract: {exc}") from exc
    if (not isinstance(owner_id, str) or not owner_id.strip() or
            not isinstance(master_epoch, int) or
            isinstance(master_epoch, bool) or master_epoch < 1 or
            owner_id != contract["master"]["owner_id"] or
            master_epoch != contract["master"]["epoch"]):
        raise LedgerError("stale or foreign master authority")
    if expected_revision != -1 or isinstance(expected_revision, bool):
        raise LedgerError("new ledger expected revision must be -1")
    run_id = contract["run_id"]
    roster = {worker["worker_id"]: worker["thread_id"]
              for worker in contract["workers"]}
    cap = contract["budgets"]["max_worker_turns"]
    revisions = contract["review_limits"]["max_blocker_revisions"]
    rechecks = contract["review_limits"]["max_blocker_rechecks"]

    def build(events):
        return {
            "type": "run_started", "run_id": run_id,
            "ledger_schema_version": 3,
            "contract_sha256": contract["contract_sha256"],
            "contract": deepcopy(contract),
            "master_owner_id": contract["master"]["owner_id"],
            "master_epoch": contract["master"]["epoch"],
            "revision": 0,
            "at": _now() if at is None else at,
            "mode": contract["mode"], "workers": roster,
            "max_worker_turns": cap,
            "max_blocker_revisions": revisions,
            "max_blocker_rechecks": rechecks,
            "final_owner": contract["final_owner"],
        }

    return _append(Path(path), build, creating=True)


def reserve_turn(path: Path, run_id: str, worker_id: str, stage: str,
                 deadline_at: float, source_candidate_id: str | None = None,
                 blocker_id: str | None = None, at: float | None = None, *,
                 owner_id: str, master_epoch: int,
                 expected_revision: int) -> dict:
    now = _now() if at is None else at

    def build(events):
        state = _state(events)
        if state["run_id"] != run_id:
            raise LedgerError("run ID mismatch")
        if state["dispatch_closed"]:
            raise LedgerError("dispatch is closed")
        if worker_id not in state["workers"]:
            raise LedgerError("worker is not active in this run")
        if not isinstance(deadline_at, (int, float)) or deadline_at <= now:
            raise LedgerError("deadline must be in the future")
        maximum_deadline = (((state["contract"] or {}).get("budgets") or {})
                            .get("max_worker_deadline_seconds"))
        if maximum_deadline is not None and deadline_at - now > maximum_deadline:
            raise LedgerError("deadline exceeds the frozen Worker limit")
        if len(state["attempts"]) >= state["max_worker_turns"]:
            raise LedgerError("worker turn cap reached")
        if any(value["worker_id"] == worker_id and value["outcome"] is None
               for value in state["attempts"].values()):
            raise LedgerError("worker already has an active attempt")
        if stage not in ("draft", "review", "revision", "recheck"):
            raise LedgerError("invalid stage")
        prior = list(state["attempts"].values())
        if stage == "draft":
            if source_candidate_id is not None or blocker_id is not None:
                raise LedgerError("draft cannot bind a source or blocker")
            if any(value["stage"] == "draft" and value["worker_id"] == worker_id for value in prior):
                raise LedgerError("worker draft already reserved")
        elif stage == "review":
            source = state["candidates"].get(source_candidate_id)
            review_mode = ((state["contract"] or {}).get("review_policy") or
                           {"mode": "full"}).get("mode", "full")
            if review_mode != "full":
                raise LedgerError("peer review is disabled for this run")
            if source is None or source["worker_id"] == worker_id:
                raise LedgerError("review must bind another Worker's candidate")
            if blocker_id is not None:
                raise LedgerError("review cannot pre-bind a blocker")
            if any(value["stage"] == "review" and value["worker_id"] == worker_id for value in prior):
                raise LedgerError("worker review already reserved")
        elif stage == "revision":
            source = state["candidates"].get(source_candidate_id)
            blocker = state["unresolved_blockers"].get(blocker_id)
            if (source is None or source["worker_id"] != worker_id or blocker is None or
                    blocker["source_candidate_id"] != source_candidate_id):
                raise LedgerError("revision must bind its Worker's unresolved BLOCKER")
            if blocker_id in state["revised_blockers"]:
                raise LedgerError("BLOCKER already revised")
            revision_count = sum(value["stage"] == "revision" for value in prior)
            if revision_count >= state["max_blocker_revisions"]:
                raise LedgerError("revision cap reached")
        else:
            source = state["candidates"].get(source_candidate_id)
            blocker = state["unresolved_blockers"].get(blocker_id)
            if (source is None or blocker is None or
                    source_candidate_id != blocker.get("revised_candidate_id") or
                    worker_id != blocker.get("reviewer_id") or
                    source["worker_id"] == worker_id):
                raise LedgerError("recheck must bind the revised candidate and original reviewer")
            if blocker_id in state["rechecked_blockers"]:
                raise LedgerError("BLOCKER already rechecked")
            recheck_count = sum(value["stage"] == "recheck" for value in prior)
            if recheck_count >= state["max_blocker_rechecks"]:
                raise LedgerError("recheck cap reached")
        attempt_id = f"{run_id}-T{len(state['attempts']) + 1:02d}"
        return {
            "type": "turn_reserved", "run_id": run_id, "at": now,
            "attempt_id": attempt_id, "stage": stage, "worker_id": worker_id,
            "thread_id": state["workers"][worker_id], "deadline_at": deadline_at,
            "source_candidate_id": source_candidate_id, "blocker_id": blocker_id,
        }

    return _append(Path(path), build, owner_id=owner_id,
                   master_epoch=master_epoch,
                   expected_revision=expected_revision)


def complete_turn(path: Path, run_id: str, attempt_id: str, thread_id: str,
                  turn_id: str, sha256: str, *, owner_id: str,
                  master_epoch: int, expected_revision: int,
                  candidate_id: str | None = None,
                  candidate_version: int | None = None, verdict: str | None = None,
                  issues: list[dict] | None = None, at: float | None = None) -> dict:
    event_at = _now() if at is None else at

    def build(events):
        state = _state(events)
        if state["run_id"] != run_id or state["terminal"] is not None:
            raise LedgerError("run is unavailable")
        attempt = state["attempts"].get(attempt_id)
        if attempt is None or attempt["outcome"] is not None:
            raise LedgerError("attempt is not awaiting a result")
        if thread_id != attempt["thread_id"]:
            raise LedgerError("result thread does not match reservation")
        if not _valid_uuid(turn_id):
            raise LedgerError("invalid turn ID")
        if turn_id in state["turn_ids"]:
            raise LedgerError("turn ID already bound")
        if not _valid_sha256(sha256):
            raise LedgerError("invalid result SHA-256")
        def rejected(reason_code: str, reason: str) -> dict:
            return {"type": "turn_rejected", "run_id": run_id, "at": event_at,
                    "attempt_id": attempt_id, "thread_id": thread_id,
                    "turn_id": turn_id, "sha256": sha256,
                    "reason_code": reason_code, "reason": reason}
        if event_at > attempt["deadline_at"]:
            return rejected("late_result", "result arrived after the reserved deadline")
        stage = attempt["stage"]
        event = {
            "type": "turn_completed", "run_id": run_id, "at": event_at,
            "attempt_id": attempt_id, "thread_id": thread_id,
            "turn_id": turn_id, "sha256": sha256,
        }
        if stage in ("draft", "revision"):
            if not isinstance(candidate_id, str) or not candidate_id:
                return rejected("semantic_invalid", "candidate ID is missing")
            expected_version = _next_candidate_version(state, attempt["worker_id"])
            if candidate_version != expected_version or verdict is not None or issues not in (None, []):
                return rejected("semantic_invalid", "invalid candidate completion")
            reported_candidate_id = candidate_id
            final_candidate_id = candidate_id
            if final_candidate_id in state["candidates"]:
                base = f"{attempt['worker_id']}-v{expected_version}"
                final_candidate_id = base
                suffix = 2
                while final_candidate_id in state["candidates"]:
                    final_candidate_id = f"{base}-{suffix}"
                    suffix += 1
            event.update(candidate_id=final_candidate_id, candidate_version=candidate_version)
            if final_candidate_id != reported_candidate_id:
                event["reported_candidate_id"] = reported_candidate_id
        else:
            normalized = [] if issues is None else issues
            if verdict not in ("PASS", "ISSUES") or not isinstance(normalized, list):
                return rejected("semantic_invalid", "invalid review verdict")
            ids = set()
            canonical = []
            for index, issue in enumerate(normalized, 1):
                if (not isinstance(issue, dict) or not isinstance(issue.get("id"), str) or
                        not issue["id"] or issue["id"] in ids or
                        issue.get("severity") not in ("BLOCKER", "NON_BLOCKER") or
                        not isinstance(issue.get("problem"), str) or not issue["problem"].strip()):
                    return rejected("semantic_invalid", "invalid review issue")
                issue_id = issue["id"]
                if issue_id in state["issue_ids"] or issue_id in ids:
                    base = f"{attempt['source_candidate_id']}-{attempt['worker_id']}-{index:02d}"
                    issue_id = base
                    suffix = 2
                    while issue_id in state["issue_ids"] or issue_id in ids:
                        issue_id = f"{base}-{suffix}"
                        suffix += 1
                    issue = {**issue, "reported_id": issue["id"], "id": issue_id}
                ids.add(issue_id)
                canonical.append(issue)
            if (verdict == "PASS" and normalized) or (verdict == "ISSUES" and not normalized):
                return rejected("semantic_invalid", "review verdict and issues disagree")
            event.update(verdict=verdict, issues=canonical,
                         source_candidate_id=attempt["source_candidate_id"])
            if stage == "recheck":
                event["blocker_id"] = attempt["blocker_id"]
        return event

    return _append(Path(path), build, owner_id=owner_id,
                   master_epoch=master_epoch,
                   expected_revision=expected_revision)


def fail_turn(path: Path, run_id: str, attempt_id: str, status: str,
              reason: str, at: float | None = None, *, owner_id: str,
              master_epoch: int, expected_revision: int) -> dict:
    event_at = _now() if at is None else at

    def build(events):
        state = _state(events)
        if state["run_id"] != run_id or state["terminal"] is not None:
            raise LedgerError("run is unavailable")
        attempt = state["attempts"].get(attempt_id)
        if attempt is None or attempt["outcome"] is not None:
            raise LedgerError("attempt is not awaiting a result")
        if status not in ("TIMEOUT", "FAILED", "UNAVAILABLE"):
            raise LedgerError("invalid failure status")
        if not isinstance(reason, str) or not reason.strip():
            raise LedgerError("failure reason is required")
        if status == "TIMEOUT" and event_at < attempt["deadline_at"]:
            raise LedgerError("TIMEOUT cannot be recorded before the deadline")
        return {"type": "turn_failed", "run_id": run_id, "at": event_at,
                "attempt_id": attempt_id, "status": status, "reason": reason}

    return _append(Path(path), build, owner_id=owner_id,
                   master_epoch=master_epoch,
                   expected_revision=expected_revision)


def cancel_run(path: Path, run_id: str, owner: str, reason: str,
               at: float | None = None, *, owner_id: str,
               master_epoch: int, expected_revision: int) -> dict:
    def build(events):
        state = _state(events)
        if state["run_id"] != run_id or state["terminal"] is not None:
            raise LedgerError("run is unavailable")
        if owner != "main_task" or owner != state["final_owner"]:
            raise LedgerError("only main_task may cancel")
        if not isinstance(reason, str) or not reason.strip():
            raise LedgerError("cancellation reason is required")
        active = sorted(key for key, value in state["attempts"].items()
                        if value["outcome"] is None)
        return {"type": "run_cancelled", "run_id": run_id,
                "at": _now() if at is None else at, "owner": owner,
                "status": "ABORT", "reason": reason,
                "active_attempt_ids": active}

    return _append(Path(path), build, owner_id=owner_id,
                   master_epoch=master_epoch,
                   expected_revision=expected_revision)


def finalize_run(path: Path, run_id: str, owner: str, status: str,
                 result_sha256: str | None = None,
                 basis_candidate_ids: list[str] | None = None,
                 at: float | None = None, *, owner_id: str,
                 master_epoch: int, expected_revision: int,
                 final_artifact: str | None = None,
                 final_message: str | None = None,
                 worker_outputs: dict[str, str] | None = None,
                 claims: list[dict] | None = None,
                 machine_results: dict[str, dict] | None = None,
                 semantic_results: dict[str, dict] | None = None,
                 final_revision: int = 0) -> dict:
    def build(events):
        state = _state(events)
        if state["run_id"] != run_id or state["terminal"] is not None:
            raise LedgerError("run is unavailable")
        if owner != state["final_owner"] or owner != "main_task":
            raise LedgerError("only main_task may finalize")
        if any(value["outcome"] is None for value in state["attempts"].values()):
            raise LedgerError("active attempts remain")
        if status not in ("DONE", "PARTIAL", "ABORT"):
            raise LedgerError("invalid final status")
        if status == "DONE" and not _done_satisfied(state):
            raise LedgerError("DONE acceptance is not satisfied")
        if status in ("DONE", "PARTIAL") and not _valid_sha256(result_sha256):
            raise LedgerError("final result SHA-256 is required")
        _validate_final_basis(state, status, basis_candidate_ids)
        delivery_receipt = None
        final_gate_receipt = None
        delivery_contract = (state["contract"] or {}).get("delivery_contract")
        if status in ("DONE", "PARTIAL") and delivery_contract is not None:
            basis_ids = basis_candidate_ids or []
            required_workers = sorted({state["candidates"][candidate_id]["worker_id"]
                                       for candidate_id in basis_ids})
            if not isinstance(worker_outputs, dict):
                raise LedgerError("Worker outputs are required for final delivery")
            for worker_id, output in worker_outputs.items():
                candidate_hashes = {
                    state["candidates"][candidate_id]["sha256"]
                    for candidate_id in basis_ids
                    if state["candidates"][candidate_id]["worker_id"] == worker_id
                }
                if not isinstance(output, str) or sha256_text(output) not in candidate_hashes:
                    raise LedgerError("Worker output does not match a final basis candidate")
            try:
                if state["contract"].get("final_gate_contract") is not None:
                    final_gate_receipt = validate_final_gate(
                        contract=state["contract"],
                        ledger_head_sha256=events[-1]["event_hash"],
                        final_artifact=final_artifact,
                        final_message=final_message,
                        worker_outputs=worker_outputs,
                        required_worker_ids=required_workers,
                        claims=claims, machine_results=machine_results,
                        semantic_results=semantic_results,
                        final_revision=final_revision)
                    delivery_receipt = final_gate_receipt["delivery_receipt"]
                else:
                    delivery_receipt = validate_delivery(
                        plan=state["contract"], final_artifact=final_artifact,
                        final_message=final_message, worker_outputs=worker_outputs,
                        required_worker_ids=required_workers)
            except (DeliveryError, FinalGateError) as exc:
                raise LedgerError(f"final delivery rejected: {exc}") from exc
            if delivery_receipt["final_artifact_sha256"] != result_sha256:
                raise LedgerError("final result SHA-256 does not match delivered artifact")
        elif any(value is not None for value in
                 (final_artifact, final_message, worker_outputs, claims,
                  machine_results, semantic_results)):
            raise LedgerError("delivery evidence is not valid for this final status")
        return {"type": "run_finalized", "run_id": run_id,
                "at": _now() if at is None else at,
                "owner": owner, "status": status, "result_sha256": result_sha256,
                "basis_candidate_ids": (sorted(basis_candidate_ids)
                                        if basis_candidate_ids is not None else None),
                "delivery_receipt": delivery_receipt,
                "final_gate_receipt": final_gate_receipt}

    return _append(Path(path), build, owner_id=owner_id,
                   master_epoch=master_epoch,
                   expected_revision=expected_revision)


def record_external_review(path: Path, run_id: str, review_id: str,
                           provider: str, artifact_sha256: str,
                           user_authorized: bool, at: float | None = None, *,
                           owner_id: str, master_epoch: int,
                           expected_revision: int) -> dict:
    """Record one explicitly authorized external review without starting it."""
    def build(events):
        state = _state(events)
        if state["run_id"] != run_id or state["terminal"] is not None:
            raise LedgerError("run is unavailable")
        policy = (state["contract"] or {}).get("external_review_contract")
        maximum = (policy.get("explicit_request_rounds") if user_authorized is True
                   else policy.get("default_rounds")) if isinstance(policy, dict) else None
        if (not isinstance(policy, dict) or not isinstance(review_id, str) or
                not review_id.strip() or
                any(item["review_id"] == review_id for item in state["external_reviews"]) or
                not isinstance(provider, str) or not provider.strip() or
                not _valid_sha256(artifact_sha256) or
                not isinstance(user_authorized, bool) or
                not isinstance(maximum, int) or
                len(state["external_reviews"]) >= maximum):
            raise LedgerError("external review is not authorized or budget is exhausted")
        return {"type": "external_review_recorded", "run_id": run_id,
                "at": _now() if at is None else at, "review_id": review_id,
                "provider": provider.strip(), "artifact_sha256": artifact_sha256,
                "user_authorized": user_authorized}

    return _append(Path(path), build, owner_id=owner_id,
                   master_epoch=master_epoch,
                   expected_revision=expected_revision)


def _validate_raw_ledger(raw: bytes) -> tuple[list[dict], dict]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LedgerError("ledger is not valid UTF-8") from exc
    events = _load(io.StringIO(text))
    return events, _state(events)


def _diagnose_raw(raw: bytes) -> dict:
    file_hash = hashlib.sha256(raw).hexdigest()
    if not raw:
        return {"status": "EMPTY", "healthy": False, "repairable": False,
                "file_sha256": file_hash, "error": "ledger is empty"}
    try:
        events, state = _validate_raw_ledger(raw)
    except LedgerError as exc:
        full_error = str(exc)
    else:
        terminal = state["terminal"]
        return {
            "status": "HEALTHY", "healthy": True, "repairable": False,
            "file_sha256": file_hash, "ledger_schema_version": state["ledger_schema_version"],
            "writable": state["ledger_schema_version"] == 3,
            "run_id": state["run_id"], "event_count": len(events),
            "last_seq": events[-1]["seq"], "revision": state["revision"],
            "ledger_head_sha256": events[-1]["event_hash"],
            "run_status": terminal["status"] if terminal else "RUNNING",
            "active_attempts": sorted(key for key, value in state["attempts"].items()
                                      if value["outcome"] is None),
        }

    # Automatic repair is intentionally narrower than diagnosis: only an
    # unterminated physical tail that is not complete JSON may be removed.
    if not raw.endswith(b"\n"):
        split_at = raw.rfind(b"\n") + 1
        prefix, tail = raw[:split_at], raw[split_at:]
        try:
            json.loads(tail.decode("utf-8"))
            tail_is_complete_json = True
        except (UnicodeDecodeError, json.JSONDecodeError):
            tail_is_complete_json = False
        if prefix and tail and not tail_is_complete_json:
            try:
                prefix_events, prefix_state = _validate_raw_ledger(prefix)
            except LedgerError:
                pass
            else:
                if prefix_state["ledger_schema_version"] == 3:
                    return {
                        "status": "TAIL_TRUNCATED", "healthy": False,
                        "repairable": True, "file_sha256": file_hash,
                        "ledger_schema_version": 3,
                        "run_id": prefix_state["run_id"],
                        "event_count": len(prefix_events),
                        "last_seq": prefix_events[-1]["seq"],
                        "revision": prefix_state["revision"],
                        "ledger_head_sha256": prefix_events[-1]["event_hash"],
                        "valid_prefix_bytes": len(prefix),
                        "truncated_tail_bytes": len(tail),
                        "error": full_error,
                    }
    return {"status": "CORRUPT_NON_REPAIRABLE", "healthy": False,
            "repairable": False, "file_sha256": file_hash,
            "error": full_error}


def doctor_ledger(path: Path) -> dict:
    """Inspect a ledger under a shared lock without changing it or its mtime."""
    path = Path(path)
    if not path.exists():
        return {"status": "MISSING", "healthy": False, "repairable": False,
                "path": str(path)}
    if path.is_symlink() or not path.is_file():
        return {"status": "UNSAFE_PATH", "healthy": False, "repairable": False,
                "path": str(path)}
    with path.open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        raw = handle.read()
    return {"path": str(path), **_diagnose_raw(raw)}


def _durable_backup(path: Path, raw: bytes) -> Path:
    digest = hashlib.sha256(raw).hexdigest()
    backup = path.with_name(f"{path.name}.bak.{time.time_ns()}.{digest[:12]}")
    fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise LedgerError("backup write made no progress")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass
    return backup


def repair_tail(path: Path, *, confirm: bool, expected_file_sha256: str,
                owner_id: str, master_epoch: int,
                expected_revision: int) -> dict:
    """Back up and remove one provably incomplete physical tail fragment."""
    path = Path(path)
    if confirm is not True:
        raise LedgerError("tail repair requires explicit confirm=True")
    if path.is_symlink() or not path.is_file():
        raise LedgerError("ledger repair path is unsafe or missing")
    with path.open("r+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        raw = handle.read()
        diagnosis = _diagnose_raw(raw)
        if diagnosis["file_sha256"] != expected_file_sha256:
            raise LedgerError("ledger changed since doctor inspection")
        if diagnosis["status"] != "TAIL_TRUNCATED" or not diagnosis["repairable"]:
            raise LedgerError("ledger damage is not a repairable truncated tail")
        prefix = raw[:diagnosis["valid_prefix_bytes"]]
        _, state = _validate_raw_ledger(prefix)
        if (not isinstance(owner_id, str) or not owner_id.strip() or
                not isinstance(master_epoch, int) or
                isinstance(master_epoch, bool) or master_epoch < 1 or
                owner_id != state["master_owner_id"] or
                master_epoch != state["master_epoch"]):
            raise LedgerError("stale or foreign master authority")
        if (not isinstance(expected_revision, int) or
                isinstance(expected_revision, bool) or
                expected_revision != state["revision"]):
            raise LedgerError("ledger revision compare-and-swap failed")
        backup = _durable_backup(path, raw)
        handle.seek(diagnosis["valid_prefix_bytes"])
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())
    repaired = doctor_ledger(path)
    if repaired["status"] != "HEALTHY":
        raise LedgerError("tail repair did not restore a healthy ledger")
    return {"status": "REPAIRED", "backup_path": str(backup),
            "removed_bytes": diagnosis["truncated_tail_bytes"],
            "file_sha256_before": diagnosis["file_sha256"],
            "file_sha256_after": repaired["file_sha256"],
            "revision": repaired["revision"]}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
