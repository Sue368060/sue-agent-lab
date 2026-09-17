import hashlib
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest

from .short_context_contract import ContractError, freeze_contract
from .visible_ledger import (LedgerError, cancel_run as _cancel_run,
                             complete_turn as _complete_turn,
                             doctor_ledger, fail_turn as _fail_turn,
                             finalize_run as _finalize_run, read_run,
                             record_external_review as _record_external_review,
                             repair_tail, reserve_turn as _reserve_turn,
                             sha256_text, start_run as _start_run)
from .visible_modes import DEFAULT_CONFIG, resolve_mode


_TEST_CONFIG = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
THREAD_A = _TEST_CONFIG["workers"]["A"]["thread_id"]
THREAD_B = _TEST_CONFIG["workers"]["B"]["thread_id"]
TURN_1 = "01a0a050-c213-7f91-811d-d535163768ca"
TURN_2 = "01a0a050-c213-7f91-811d-d535163768cb"
TURN_3 = "01a0a050-c213-7f91-811d-d535163768cc"
TURN_4 = "01a0a050-c213-7f91-811d-d535163768cd"
TURN_5 = "01a0a050-c213-7f91-811d-d535163768ce"
TURN_6 = "01a0a050-c213-7f91-811d-d535163768cf"
TURN_7 = "01a0a050-c213-7f91-811d-d535163768d0"
TURN_8 = "01a0a050-c213-7f91-811d-d535163768d1"
OWNER_ID = "brain-thread-test"
MASTER_EPOCH = 9


def _candidate_text(candidate_id):
    return "".join(
        f"候选{candidate_id}的第{index}项建议包含不同日期、路线、预算、风险与执行责任，需要在最终方案中保留可核对证据。"
        for index in range(1, 9)
    )


def _delivery_artifact(worker_outputs):
    sections = {
        "推荐结论": "采用信息最完整且风险最低的组合，并保留明确的取舍理由。最终选择必须能追溯到用户要求和候选稿证据。",
        "详细方案": "按顺序说明执行步骤、时间安排、负责人和完成证据。每一步都需要写清开始条件、结束条件和失败后的处理。",
        "预算与资源": "统一列出时间、额度、工具与人工检查成本，避免重复计算。所有数字注明计算范围，并保留合理的安全余量。",
        "备选方案": "主要条件不成立时缩小范围，保留可以独立完成的核心成果。替代路径不能突破用户限制，也不能偷偷增加模型回合。",
        "风险与未知项": "未确认事实必须标记来源与验证动作，不能伪装成已经确定。高影响未知项要在执行前验证，并记录验证结果。",
        "下一步行动": "先完成机械检查，再保存结果和证据，最后才允许标记任务完成。失败时返回具体原因，修复后重新检查一次。",
    }
    sections["详细方案"] += "\n" + "\n".join(worker_outputs.values())
    sections["推荐结论"] += "".join(
        f"第{index}条结论对应清晰的依据和选择。" for index in range(1, 20)
    )
    return "# 最终成果\n\n" + "\n\n".join(
        f"## {heading}\n\n{body}" for heading, body in sections.items()
    )


def _contract(plan, run_id):
    return freeze_contract(
        run_id=run_id, source_request=f"offline test {run_id}", plan=plan,
        task_plan={worker["worker_id"]: f"assignment {worker['worker_id']}"
                   for worker in plan.get("workers", [])},
        task_plan_source="deterministic",
        hard_constraints=[{"id": "HC1", "value": "offline",
                           "source": "explicit_user"}],
        success_criteria={
            "machine_checks": [{"id": "MC1", "description": "ledger valid",
                                "checker": "ledger", "required": True}],
            "semantic_goals": [],
        },
        master_owner=OWNER_ID, master_epoch=MASTER_EPOCH,
        max_leader_turns=2,
    )


def start_run(path, plan, run_id, at=None):
    try:
        contract = _contract(plan, run_id)
    except ContractError as exc:
        raise LedgerError(str(exc)) from exc
    return _start_run(path, contract, at=at, owner_id=OWNER_ID,
                      master_epoch=MASTER_EPOCH, expected_revision=-1)


def _authority(path):
    state = read_run(path)
    return {"owner_id": OWNER_ID, "master_epoch": MASTER_EPOCH,
            "expected_revision": state["revision"]}


def reserve_turn(path, run_id, worker_id, stage, deadline_at,
                 source_candidate_id=None, blocker_id=None, at=None):
    return _reserve_turn(path, run_id, worker_id, stage, deadline_at,
                         source_candidate_id=source_candidate_id,
                         blocker_id=blocker_id, at=at, **_authority(path))


def complete_turn(path, run_id, attempt_id, thread_id, turn_id, sha256, **kwargs):
    return _complete_turn(path, run_id, attempt_id, thread_id, turn_id, sha256,
                          **_authority(path), **kwargs)


def fail_turn(path, run_id, attempt_id, status, reason, at=None):
    return _fail_turn(path, run_id, attempt_id, status, reason, at=at,
                      **_authority(path))


def cancel_run(path, run_id, owner, reason, at=None):
    return _cancel_run(path, run_id, owner, reason, at=at, **_authority(path))


def finalize_run(path, run_id, owner, status, result_sha256=None,
                 basis_candidate_ids=None, at=None):
    evidence = {}
    if status in ("DONE", "PARTIAL") and basis_candidate_ids and all(
            isinstance(candidate_id, str) and candidate_id[0] in "ABCD"
            for candidate_id in basis_candidate_ids):
        worker_outputs = {candidate_id[0]: _candidate_text(candidate_id)
                          for candidate_id in basis_candidate_ids}
        artifact = _delivery_artifact(worker_outputs)
        result_sha256 = sha256_text(artifact)
        source_worker = sorted(worker_outputs)[0]
        source_claim = worker_outputs[source_worker].split("。", 1)[0] + "。"
        evidence = {"final_artifact": artifact,
                    "final_message": "完整结果如下：\n" + artifact,
                    "worker_outputs": worker_outputs,
                    "claims": [{"id": "CL1", "text": source_claim,
                                "support_type": "source",
                                "source_worker_ids": [source_worker],
                                "evidence_quotes": {source_worker: source_claim}}],
                    "machine_results": {
                        "MC1": {"status": "PASS", "evidence": "ledger replay passed"}},
                    "semantic_results": {}, "final_revision": 0}
    return _finalize_run(path, run_id, owner, status, result_sha256,
                         basis_candidate_ids, at=at, **_authority(path), **evidence)


def record_external_review(path, run_id, review_id, provider, artifact_sha256,
                           user_authorized, at=None):
    return _record_external_review(
        path, run_id, review_id, provider, artifact_sha256, user_authorized,
        at=at, **_authority(path))


def _race_reserve(path, worker_id, gate, queue):
    gate.wait()
    try:
        event = _reserve_turn(Path(path), "RACE", worker_id, "draft", 100,
                              at=2, owner_id=OWNER_ID,
                              master_epoch=MASTER_EPOCH, expected_revision=0)
    except Exception as exc:
        queue.put(("error", type(exc).__name__, str(exc)))
    else:
        queue.put(("ok", event["attempt_id"]))


def _race_start(path, contract, gate, queue):
    gate.wait()
    try:
        event = _start_run(Path(path), contract, at=1, owner_id=OWNER_ID,
                           master_epoch=MASTER_EPOCH, expected_revision=-1)
    except Exception as exc:
        queue.put(("error", type(exc).__name__, str(exc)))
    else:
        queue.put(("ok", event["run_id"]))


def _rehash_event(event):
    body = {key: value for key, value in event.items() if key != "event_hash"}
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class VisibleLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "run.jsonl"
        config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        self.plan = resolve_mode(config, "small_reviewed")
        start_run(self.path, self.plan, "RUN-1", at=1)

    def tearDown(self):
        self.temp.cleanup()

    def reserve(self, worker, stage, **kwargs):
        return reserve_turn(self.path, "RUN-1", worker, stage, 100, at=2, **kwargs)

    def complete_candidate(self, attempt, thread, turn, candidate, version=1):
        return complete_turn(self.path, "RUN-1", attempt, thread, turn,
                             sha256_text(_candidate_text(candidate)), candidate_id=candidate,
                             candidate_version=version, at=3)

    def prepare_drafts(self):
        a = self.reserve("A", "draft")
        self.complete_candidate(a["attempt_id"], THREAD_A, TURN_1, "A1")
        b = self.reserve("B", "draft")
        self.complete_candidate(b["attempt_id"], THREAD_B, TURN_2, "B1")

    def test_happy_path_requires_both_drafts_and_reviews(self):
        self.prepare_drafts()
        a_review = self.reserve("A", "review", source_candidate_id="B1")
        complete_turn(self.path, "RUN-1", a_review["attempt_id"], THREAD_A, TURN_3,
                      sha256_text("pass-a"), verdict="PASS", issues=[], at=4)
        b_review = self.reserve("B", "review", source_candidate_id="A1")
        complete_turn(self.path, "RUN-1", b_review["attempt_id"], THREAD_B, TURN_4,
                      sha256_text("pass-b"), verdict="PASS", issues=[], at=4)
        with self.assertRaises(LedgerError):
            finalize_run(self.path, "RUN-1", "main_task", "DONE", sha256_text("final"),
                         basis_candidate_ids=["A1", "unknown"], at=5)
        finalize_run(self.path, "RUN-1", "main_task", "DONE", sha256_text("final"),
                     basis_candidate_ids=["A1", "B1"], at=5)
        state = read_run(self.path)
        self.assertEqual(state["status"], "DONE")
        self.assertEqual(state["final_basis_status"], "BOUND")

    def test_done_requires_every_draft_candidate_to_be_reviewed(self):
        config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        plan = resolve_mode(config, "large_reviewed")
        path = Path(self.temp.name) / "large.jsonl"
        start_run(path, plan, "LARGE", at=1)
        candidates = {}
        turn_number = 1
        for worker in ("A", "B", "C", "D"):
            reserved = reserve_turn(path, "LARGE", worker, "draft", 100, at=2)
            candidate_id = f"{worker}1"
            candidates[worker] = candidate_id
            complete_turn(path, "LARGE", reserved["attempt_id"],
                          next(item["thread_id"] for item in plan["workers"]
                               if item["worker_id"] == worker),
                          f"00000000-0000-4000-8000-{turn_number:012d}",
                          sha256_text(_candidate_text(candidate_id)), candidate_id=candidate_id,
                          candidate_version=1, at=3)
            turn_number += 1
        # All reviewers complete, but nobody reviews D1.
        sources = {"A": "B1", "B": "A1", "C": "B1", "D": "C1"}
        for worker, source in sources.items():
            reserved = reserve_turn(path, "LARGE", worker, "review", 100,
                                    source_candidate_id=source, at=4)
            complete_turn(path, "LARGE", reserved["attempt_id"],
                          next(item["thread_id"] for item in plan["workers"]
                               if item["worker_id"] == worker),
                          f"00000000-0000-4000-8000-{turn_number:012d}",
                          sha256_text(f"review-{worker}"), verdict="PASS", issues=[], at=5)
            turn_number += 1
        with self.assertRaises(LedgerError):
            finalize_run(path, "LARGE", "main_task", "DONE", sha256_text("final"),
                         basis_candidate_ids=list(candidates.values()), at=6)

    def test_quick_mode_finishes_after_one_parallel_draft_wave(self):
        config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        plan = resolve_mode(config, "small")
        path = Path(self.temp.name) / "quick.jsonl"
        start_run(path, plan, "QUICK", at=1)
        a = reserve_turn(path, "QUICK", "A", "draft", 100, at=2)
        complete_turn(path, "QUICK", a["attempt_id"], THREAD_A, TURN_1,
                      sha256_text(_candidate_text("A1")), candidate_id="A1", candidate_version=1, at=3)
        b = reserve_turn(path, "QUICK", "B", "draft", 100, at=2)
        complete_turn(path, "QUICK", b["attempt_id"], THREAD_B, TURN_2,
                      sha256_text(_candidate_text("B1")), candidate_id="B1", candidate_version=1, at=3)
        with self.assertRaises(LedgerError):
            reserve_turn(path, "QUICK", "A", "review", 100,
                         source_candidate_id="B1", at=4)
        finalize_run(path, "QUICK", "main_task", "DONE", sha256_text("final"),
                     basis_candidate_ids=["A1", "B1"], at=5)
        self.assertEqual(read_run(path)["status"], "DONE")
        self.assertEqual(read_run(path)["delivery_status"], "PASS")

    def test_new_run_cannot_finalize_without_delivery_evidence(self):
        config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        plan = resolve_mode(config, "small")
        path = Path(self.temp.name) / "delivery-required.jsonl"
        start_run(path, plan, "DELIVERY", at=1)
        for worker, thread, turn in (("A", THREAD_A, TURN_1),
                                     ("B", THREAD_B, TURN_2)):
            candidate_id = f"{worker}1"
            reserved = reserve_turn(path, "DELIVERY", worker, "draft", 100, at=2)
            complete_turn(path, "DELIVERY", reserved["attempt_id"], thread, turn,
                          sha256_text(_candidate_text(candidate_id)),
                          candidate_id=candidate_id, candidate_version=1, at=3)
        with self.assertRaisesRegex(LedgerError, "outputs are required"):
            _finalize_run(path, "DELIVERY", "main_task", "DONE",
                          sha256_text("placeholder"), ["A1", "B1"], at=4,
                          **_authority(path))

    def test_delivery_worker_text_must_match_candidate_hash(self):
        config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        plan = resolve_mode(config, "small")
        path = Path(self.temp.name) / "delivery-binding.jsonl"
        start_run(path, plan, "DELIVERY-BIND", at=1)
        for worker, thread, turn in (("A", THREAD_A, TURN_1),
                                     ("B", THREAD_B, TURN_2)):
            candidate_id = f"{worker}1"
            reserved = reserve_turn(path, "DELIVERY-BIND", worker, "draft", 100, at=2)
            complete_turn(path, "DELIVERY-BIND", reserved["attempt_id"], thread, turn,
                          sha256_text(_candidate_text(candidate_id)),
                          candidate_id=candidate_id, candidate_version=1, at=3)
        outputs = {"A": _candidate_text("A1"), "B": "伪造内容" * 100}
        artifact = _delivery_artifact(outputs)
        with self.assertRaisesRegex(LedgerError, "does not match"):
            _finalize_run(path, "DELIVERY-BIND", "main_task", "DONE",
                          sha256_text(artifact), ["A1", "B1"], at=4,
                          final_artifact=artifact,
                          final_message="结果如下\n" + artifact,
                          worker_outputs=outputs, **_authority(path))

    def test_reservation_cannot_exceed_frozen_worker_deadline(self):
        with self.assertRaisesRegex(LedgerError, "exceeds the frozen"):
            reserve_turn(self.path, "RUN-1", "A", "draft", 303, at=2)

    def test_external_review_requires_user_authorization_and_is_capped(self):
        artifact_hash = sha256_text("external review input")
        with self.assertRaisesRegex(LedgerError, "not authorized"):
            record_external_review(self.path, "RUN-1", "EXT-0", "claude",
                                   artifact_hash, False, at=2)
        event = record_external_review(self.path, "RUN-1", "EXT-1", "claude",
                                       artifact_hash, True, at=2)
        self.assertEqual(event["type"], "external_review_recorded")
        self.assertEqual(read_run(self.path)["external_review_rounds"], 1)
        with self.assertRaisesRegex(LedgerError, "budget is exhausted"):
            record_external_review(self.path, "RUN-1", "EXT-2", "claude",
                                   artifact_hash, True, at=3)

    def test_wrong_thread_and_duplicate_turn_are_rejected(self):
        a = self.reserve("A", "draft")
        with self.assertRaises(LedgerError):
            self.complete_candidate(a["attempt_id"], THREAD_B, TURN_1, "A1")
        self.complete_candidate(a["attempt_id"], THREAD_A, TURN_1, "A1")
        b = self.reserve("B", "draft")
        with self.assertRaises(LedgerError):
            self.complete_candidate(b["attempt_id"], THREAD_B, TURN_1, "B1")
        remapped = self.complete_candidate(b["attempt_id"], THREAD_B, TURN_2, "A1")
        self.assertEqual(remapped["reported_candidate_id"], "A1")
        self.assertNotEqual(remapped["candidate_id"], "A1")
        self.assertEqual(read_run(self.path)["worker_turns"], 2)

    def test_non_blocker_cannot_open_revision(self):
        self.prepare_drafts()
        review = self.reserve("A", "review", source_candidate_id="B1")
        complete_turn(self.path, "RUN-1", review["attempt_id"], THREAD_A, TURN_3,
                      sha256_text("style"), verdict="ISSUES",
                      issues=[{"id": "N1", "severity": "NON_BLOCKER", "problem": "style"}], at=4)
        with self.assertRaises(LedgerError):
            self.reserve("B", "revision", source_candidate_id="B1", blocker_id="N1")

    def test_colliding_reported_issue_ids_are_namespaced_without_losing_result(self):
        self.prepare_drafts()
        review_a = self.reserve("A", "review", source_candidate_id="B1")
        complete_turn(self.path, "RUN-1", review_a["attempt_id"], THREAD_A, TURN_3,
                      sha256_text("block-a"), verdict="ISSUES",
                      issues=[{"id": "SHARED", "severity": "BLOCKER", "problem": "first"}], at=4)
        review_b = self.reserve("B", "review", source_candidate_id="A1")
        completed = complete_turn(self.path, "RUN-1", review_b["attempt_id"], THREAD_B, TURN_4,
                                  sha256_text("block-b"), verdict="ISSUES",
                                  issues=[{"id": "SHARED", "severity": "BLOCKER",
                                           "problem": "second"}], at=4)
        self.assertEqual(completed["issues"][0]["reported_id"], "SHARED")
        self.assertNotEqual(completed["issues"][0]["id"], "SHARED")
        self.assertEqual(len(read_run(self.path)["unresolved_blockers"]), 2)

    def test_candidate_versions_increase_across_two_blocker_revisions(self):
        self.prepare_drafts()
        review = self.reserve("A", "review", source_candidate_id="B1")
        complete_turn(self.path, "RUN-1", review["attempt_id"], THREAD_A, TURN_3,
                      sha256_text("two-blockers"), verdict="ISSUES", issues=[
                          {"id": "B1-X", "severity": "BLOCKER", "problem": "first"},
                          {"id": "B1-Y", "severity": "BLOCKER", "problem": "second"}], at=4)
        other = self.reserve("B", "review", source_candidate_id="A1")
        complete_turn(self.path, "RUN-1", other["attempt_id"], THREAD_B, TURN_4,
                      sha256_text("pass-a"), verdict="PASS", issues=[], at=4)
        first = self.reserve("B", "revision", source_candidate_id="B1", blocker_id="B1-X")
        self.complete_candidate(first["attempt_id"], THREAD_B, TURN_5, "B2", version=2)
        check_first = self.reserve("A", "recheck", source_candidate_id="B2", blocker_id="B1-X")
        complete_turn(self.path, "RUN-1", check_first["attempt_id"], THREAD_A, TURN_6,
                      sha256_text("first-fixed"), verdict="PASS", issues=[], at=5)
        second = self.reserve("B", "revision", source_candidate_id="B1", blocker_id="B1-Y")
        self.complete_candidate(second["attempt_id"], THREAD_B, TURN_7, "B3", version=3)
        check_second = self.reserve("A", "recheck", source_candidate_id="B3", blocker_id="B1-Y")
        complete_turn(self.path, "RUN-1", check_second["attempt_id"], THREAD_A, TURN_8,
                      sha256_text("second-fixed"), verdict="PASS", issues=[], at=5)
        finalize_run(self.path, "RUN-1", "main_task", "DONE", sha256_text("final"),
                     basis_candidate_ids=["A1", "B3"], at=6)
        self.assertEqual(read_run(self.path)["status"], "DONE")

    def test_blocker_revision_requires_original_reviewer_recheck(self):
        self.prepare_drafts()
        review = self.reserve("A", "review", source_candidate_id="B1")
        complete_turn(self.path, "RUN-1", review["attempt_id"], THREAD_A, TURN_3,
                      sha256_text("block"), verdict="ISSUES",
                      issues=[{"id": "B1-X", "severity": "BLOCKER", "problem": "missing evidence"}], at=4)
        other_review = self.reserve("B", "review", source_candidate_id="A1")
        complete_turn(self.path, "RUN-1", other_review["attempt_id"], THREAD_B, TURN_4,
                      sha256_text("pass-a"), verdict="PASS", issues=[], at=4)
        revision = self.reserve("B", "revision", source_candidate_id="B1", blocker_id="B1-X")
        self.complete_candidate(revision["attempt_id"], THREAD_B, TURN_5, "B2", version=2)
        self.assertEqual(read_run(self.path)["unresolved_blockers"], ["B1-X"])
        with self.assertRaises(LedgerError):
            self.reserve("B", "revision", source_candidate_id="B1", blocker_id="B1-X")
        with self.assertRaises(LedgerError):
            self.reserve("B", "recheck", source_candidate_id="B2", blocker_id="B1-X")
        recheck = self.reserve("A", "recheck", source_candidate_id="B2", blocker_id="B1-X")
        complete_turn(self.path, "RUN-1", recheck["attempt_id"], THREAD_A, TURN_6,
                      sha256_text("fixed"), verdict="PASS", issues=[], at=5)
        self.assertEqual(read_run(self.path)["unresolved_blockers"], [])
        with self.assertRaises(LedgerError):
            finalize_run(self.path, "RUN-1", "main_task", "DONE", sha256_text("stale"),
                         basis_candidate_ids=["A1", "B1"], at=6)
        finalize_run(self.path, "RUN-1", "main_task", "DONE", sha256_text("current"),
                     basis_candidate_ids=["A1", "B2"], at=6)

    def test_failed_recheck_closes_dispatch_for_escalation(self):
        self.prepare_drafts()
        review = self.reserve("A", "review", source_candidate_id="B1")
        complete_turn(self.path, "RUN-1", review["attempt_id"], THREAD_A, TURN_3,
                      sha256_text("block"), verdict="ISSUES",
                      issues=[{"id": "B1-X", "severity": "BLOCKER", "problem": "missing"}], at=4)
        revision = self.reserve("B", "revision", source_candidate_id="B1", blocker_id="B1-X")
        self.complete_candidate(revision["attempt_id"], THREAD_B, TURN_4, "B2", version=2)
        recheck = self.reserve("A", "recheck", source_candidate_id="B2", blocker_id="B1-X")
        complete_turn(self.path, "RUN-1", recheck["attempt_id"], THREAD_A, TURN_5,
                      sha256_text("still broken"), verdict="ISSUES",
                      issues=[{"id": "B2-X", "severity": "BLOCKER", "problem": "still missing"}], at=5)
        self.assertTrue(read_run(self.path)["dispatch_closed"])
        self.assertEqual(read_run(self.path)["unresolved_blockers"], ["B1-X", "B2-X"])

    def test_failure_closes_dispatch_and_allows_partial_finalize(self):
        b = self.reserve("B", "draft")
        self.complete_candidate(b["attempt_id"], THREAD_B, TURN_2, "B1")
        a = self.reserve("A", "draft")
        fail_turn(self.path, "RUN-1", a["attempt_id"], "TIMEOUT", "deadline", at=101)
        with self.assertRaises(LedgerError):
            self.reserve("B", "draft")
        finalize_run(self.path, "RUN-1", "main_task", "PARTIAL",
                     sha256_text("partial"), basis_candidate_ids=["B1"], at=102)
        self.assertEqual(read_run(self.path)["status"], "PARTIAL")

    def test_late_result_is_recorded_and_closes_dispatch(self):
        a = self.reserve("A", "draft")
        event = complete_turn(self.path, "RUN-1", a["attempt_id"], THREAD_A, TURN_1,
                              sha256_text("late"), candidate_id="A1", candidate_version=1, at=101)
        self.assertEqual(event["type"], "turn_rejected")
        state = read_run(self.path)
        self.assertTrue(state["dispatch_closed"])
        self.assertEqual(state["rejected_attempts"], [a["attempt_id"]])
        with self.assertRaises(LedgerError):
            fail_turn(self.path, "RUN-1", a["attempt_id"], "TIMEOUT", "deadline", at=101)

    def test_user_cancel_aborts_active_attempts_without_retry(self):
        self.reserve("A", "draft")
        self.reserve("B", "draft")
        cancel_run(self.path, "RUN-1", "main_task", "user stopped", at=4)
        state = read_run(self.path)
        self.assertEqual(state["status"], "ABORT")
        self.assertEqual(state["active_attempts"], [])
        self.assertTrue(state["dispatch_closed"])
        with self.assertRaises(LedgerError):
            self.reserve("A", "draft")

    def test_changed_prior_event_breaks_hash_chain(self):
        a = self.reserve("A", "draft")
        self.complete_candidate(a["attempt_id"], THREAD_A, TURN_1, "A1")
        lines = self.path.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[0])
        event["max_worker_turns"] = 999
        lines[0] = json.dumps(event, ensure_ascii=False, sort_keys=True)
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(LedgerError):
            read_run(self.path)

    def test_turn_cap_and_final_owner_are_enforced(self):
        self.plan["max_worker_turns"] = 2
        other = Path(self.temp.name) / "capped.jsonl"
        with self.assertRaises(LedgerError):
            start_run(other, self.plan, "CAP", at=1)
        missing_owner = dict(resolve_mode(
            json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8")), "small"))
        missing_owner.pop("final_owner")
        with self.assertRaises(LedgerError):
            start_run(other, missing_owner, "NO-OWNER", at=1)
        with self.assertRaises(LedgerError):
            finalize_run(self.path, "RUN-1", "worker", "ABORT", at=3)

    def test_schema_v3_all_mutations_are_fenced_and_revisioned(self):
        contract = _contract(self.plan, "AUTH")
        new_path = Path(self.temp.name) / "auth.jsonl"
        for owner, epoch, revision in (("wrong", MASTER_EPOCH, -1),
                                       (OWNER_ID, MASTER_EPOCH - 1, -1),
                                       (OWNER_ID, float(MASTER_EPOCH), -1),
                                       (OWNER_ID, True, -1),
                                       (OWNER_ID, MASTER_EPOCH, 0)):
            with self.subTest(start=(owner, epoch, revision)), self.assertRaises(LedgerError):
                _start_run(new_path, contract, at=1, owner_id=owner,
                           master_epoch=epoch, expected_revision=revision)
        self.assertFalse(new_path.exists())
        _start_run(new_path, contract, at=1, owner_id=OWNER_ID,
                   master_epoch=MASTER_EPOCH, expected_revision=-1)

        original = new_path.read_bytes()
        for owner, epoch, revision in (("wrong", MASTER_EPOCH, 0),
                                       (OWNER_ID, MASTER_EPOCH - 1, 0),
                                       (OWNER_ID, float(MASTER_EPOCH), 0),
                                       (OWNER_ID, True, 0),
                                       (OWNER_ID, MASTER_EPOCH, 1)):
            with self.subTest(reserve=(owner, epoch, revision)), self.assertRaises(LedgerError):
                _reserve_turn(new_path, "AUTH", "A", "draft", 100, at=2,
                              owner_id=owner, master_epoch=epoch,
                              expected_revision=revision)
            self.assertEqual(new_path.read_bytes(), original)
        reserved = _reserve_turn(new_path, "AUTH", "A", "draft", 100, at=2,
                                 owner_id=OWNER_ID, master_epoch=MASTER_EPOCH,
                                 expected_revision=0)
        self.assertEqual(reserved["revision"], 1)

        after_reserve = new_path.read_bytes()
        with self.assertRaises(LedgerError):
            _complete_turn(new_path, "AUTH", reserved["attempt_id"], THREAD_A,
                           TURN_1, sha256_text("A1"), owner_id=OWNER_ID,
                           master_epoch=MASTER_EPOCH, expected_revision=0,
                           candidate_id="A1", candidate_version=1, at=3)
        self.assertEqual(new_path.read_bytes(), after_reserve)
        completed = _complete_turn(
            new_path, "AUTH", reserved["attempt_id"], THREAD_A, TURN_1,
            sha256_text("A1"), owner_id=OWNER_ID, master_epoch=MASTER_EPOCH,
            expected_revision=1, candidate_id="A1", candidate_version=1, at=3)
        self.assertEqual(completed["revision"], 2)

        b = _reserve_turn(new_path, "AUTH", "B", "draft", 100, at=3,
                          owner_id=OWNER_ID, master_epoch=MASTER_EPOCH,
                          expected_revision=2)
        before_fail = new_path.read_bytes()
        with self.assertRaises(LedgerError):
            _fail_turn(new_path, "AUTH", b["attempt_id"], "FAILED", "x", at=4,
                       owner_id=OWNER_ID, master_epoch=MASTER_EPOCH - 1,
                       expected_revision=3)
        self.assertEqual(new_path.read_bytes(), before_fail)
        _fail_turn(new_path, "AUTH", b["attempt_id"], "FAILED", "x", at=4,
                   owner_id=OWNER_ID, master_epoch=MASTER_EPOCH,
                   expected_revision=3)
        before_cancel = new_path.read_bytes()
        with self.assertRaises(LedgerError):
            _cancel_run(new_path, "AUTH", "main_task", "stop", at=5,
                        owner_id="wrong", master_epoch=MASTER_EPOCH,
                        expected_revision=4)
        self.assertEqual(new_path.read_bytes(), before_cancel)
        cancelled = _cancel_run(new_path, "AUTH", "main_task", "stop", at=5,
                                owner_id=OWNER_ID, master_epoch=MASTER_EPOCH,
                                expected_revision=4)
        self.assertEqual(cancelled["revision"], 5)

        final_path = Path(self.temp.name) / "final-auth.jsonl"
        final_contract = _contract(self.plan, "FINAL-AUTH")
        _start_run(final_path, final_contract, at=1, owner_id=OWNER_ID,
                   master_epoch=MASTER_EPOCH, expected_revision=-1)
        before_final = final_path.read_bytes()
        with self.assertRaises(LedgerError):
            _finalize_run(final_path, "FINAL-AUTH", "main_task", "ABORT",
                          owner_id=OWNER_ID, master_epoch=MASTER_EPOCH,
                          expected_revision=1, at=2)
        self.assertEqual(final_path.read_bytes(), before_final)
        finalized = _finalize_run(final_path, "FINAL-AUTH", "main_task", "ABORT",
                                  owner_id=OWNER_ID, master_epoch=MASTER_EPOCH,
                                  expected_revision=0, at=2)
        self.assertEqual(finalized["revision"], 1)

    def test_two_process_start_and_reserve_cas_allow_one_writer(self):
        ctx = multiprocessing.get_context("fork")
        start_path = Path(self.temp.name) / "start-race.jsonl"
        contract = _contract(self.plan, "START-RACE")
        gate, queue = ctx.Event(), ctx.Queue()
        processes = [ctx.Process(target=_race_start,
                                 args=(str(start_path), contract, gate, queue))
                     for _ in range(2)]
        for process in processes:
            process.start()
        gate.set()
        for process in processes:
            process.join(5)
            self.assertEqual(process.exitcode, 0)
        outcomes = [queue.get(timeout=2)[0] for _ in processes]
        self.assertEqual(sorted(outcomes), ["error", "ok"])
        self.assertEqual(read_run(start_path)["revision"], 0)

        race_path = Path(self.temp.name) / "reserve-race.jsonl"
        start_run(race_path, self.plan, "RACE", at=1)
        gate, queue = ctx.Event(), ctx.Queue()
        processes = [ctx.Process(target=_race_reserve,
                                 args=(str(race_path), "A", gate, queue))
                     for _ in range(2)]
        for process in processes:
            process.start()
        gate.set()
        for process in processes:
            process.join(5)
            self.assertEqual(process.exitcode, 0)
        outcomes = [queue.get(timeout=2)[0] for _ in processes]
        self.assertEqual(sorted(outcomes), ["error", "ok"])
        state = read_run(race_path)
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["worker_turns"], 1)

    def test_doctor_is_read_only_and_tail_repair_is_backup_first(self):
        before = self.path.read_bytes()
        mtime = self.path.stat().st_mtime_ns
        healthy = doctor_ledger(self.path)
        self.assertEqual(healthy["status"], "HEALTHY")
        self.assertTrue(healthy["writable"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)

        with self.path.open("ab") as handle:
            handle.write(b'{"seq":2,"type":"turn_res')
        damaged = self.path.read_bytes()
        diagnosis = doctor_ledger(self.path)
        self.assertEqual(diagnosis["status"], "TAIL_TRUNCATED")
        self.assertTrue(diagnosis["repairable"])
        with self.assertRaises(LedgerError):
            repair_tail(self.path, confirm=False,
                        expected_file_sha256=diagnosis["file_sha256"],
                        owner_id=OWNER_ID, master_epoch=MASTER_EPOCH,
                        expected_revision=0)
        for invalid_epoch in (float(MASTER_EPOCH), True):
            with self.subTest(repair_epoch=invalid_epoch), self.assertRaises(LedgerError):
                repair_tail(
                    self.path, confirm=True,
                    expected_file_sha256=diagnosis["file_sha256"],
                    owner_id=OWNER_ID, master_epoch=invalid_epoch,
                    expected_revision=0)
            self.assertEqual(self.path.read_bytes(), damaged)
        repaired = repair_tail(
            self.path, confirm=True,
            expected_file_sha256=hashlib.sha256(damaged).hexdigest(),
            owner_id=OWNER_ID, master_epoch=MASTER_EPOCH,
            expected_revision=0)
        self.assertEqual(repaired["status"], "REPAIRED")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(Path(repaired["backup_path"]).read_bytes(), damaged)
        self.assertEqual(doctor_ledger(self.path)["status"], "HEALTHY")

    def test_doctor_refuses_middle_or_complete_tail_corruption(self):
        self.reserve("A", "draft")
        lines = self.path.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["mode"] = "tampered"
        lines[0] = json.dumps(first, ensure_ascii=False, sort_keys=True)
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        middle = doctor_ledger(self.path)
        self.assertEqual(middle["status"], "CORRUPT_NON_REPAIRABLE")
        self.assertFalse(middle["repairable"])
        with self.assertRaises(LedgerError):
            repair_tail(self.path, confirm=True,
                        expected_file_sha256=middle["file_sha256"],
                        owner_id=OWNER_ID, master_epoch=MASTER_EPOCH,
                        expected_revision=0)

        complete_tail = Path(self.temp.name) / "complete-tail.jsonl"
        start_run(complete_tail, self.plan, "COMPLETE-TAIL", at=1)
        with complete_tail.open("ab") as handle:
            handle.write(b'{"complete":"json"}')
        diagnosis = doctor_ledger(complete_tail)
        self.assertEqual(diagnosis["status"], "CORRUPT_NON_REPAIRABLE")
        self.assertFalse(diagnosis["repairable"])

        semantic = Path(self.temp.name) / "semantic.jsonl"
        start_run(semantic, self.plan, "SEMANTIC", at=1)
        events = semantic.read_text(encoding="utf-8").splitlines()
        start = json.loads(events[0])
        bad = {"seq": 2, "prev_hash": start["event_hash"],
               "type": "turn_reserved", "run_id": "SEMANTIC", "at": 2,
               "attempt_id": "SEMANTIC-T01", "stage": "draft",
               "worker_id": "NOT-A-WORKER", "thread_id": "wrong",
               "deadline_at": 100, "source_candidate_id": None,
               "blocker_id": None, "master_owner_id": OWNER_ID,
               "master_epoch": MASTER_EPOCH, "revision": 1}
        bad["event_hash"] = _rehash_event(bad)
        with semantic.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(bad, ensure_ascii=False, sort_keys=True) + "\n")
        semantic_result = doctor_ledger(semantic)
        self.assertEqual(semantic_result["status"], "CORRUPT_NON_REPAIRABLE")
        self.assertFalse(semantic_result["repairable"])

        bad_turn = Path(self.temp.name) / "bad-turn.jsonl"
        start_run(bad_turn, self.plan, "BAD-TURN", at=1)
        reserved = reserve_turn(bad_turn, "BAD-TURN", "A", "draft", 100, at=2)
        complete_turn(bad_turn, "BAD-TURN", reserved["attempt_id"], THREAD_A,
                      TURN_1, sha256_text("A1"), candidate_id="A1",
                      candidate_version=1, at=3)
        turn_lines = bad_turn.read_text(encoding="utf-8").splitlines()
        invalid_turn = json.loads(turn_lines[-1])
        invalid_turn["turn_id"] = "BAD"
        invalid_turn["event_hash"] = _rehash_event(invalid_turn)
        turn_lines[-1] = json.dumps(invalid_turn, ensure_ascii=False, sort_keys=True)
        bad_turn.write_text("\n".join(turn_lines) + "\n", encoding="utf-8")
        bad_turn_result = doctor_ledger(bad_turn)
        self.assertEqual(bad_turn_result["status"], "CORRUPT_NON_REPAIRABLE")
        self.assertFalse(bad_turn_result["repairable"])

        bool_revision = Path(self.temp.name) / "bool-revision.jsonl"
        start_run(bool_revision, self.plan, "BOOL-REV", at=1)
        reserve_turn(bool_revision, "BOOL-REV", "A", "draft", 100, at=2)
        revision_lines = bool_revision.read_text(encoding="utf-8").splitlines()
        invalid_revision = json.loads(revision_lines[-1])
        invalid_revision["revision"] = True
        invalid_revision["event_hash"] = _rehash_event(invalid_revision)
        revision_lines[-1] = json.dumps(invalid_revision, ensure_ascii=False,
                                        sort_keys=True)
        bool_revision.write_text("\n".join(revision_lines) + "\n", encoding="utf-8")
        revision_result = doctor_ledger(bool_revision)
        self.assertEqual(revision_result["status"], "CORRUPT_NON_REPAIRABLE")
        self.assertFalse(revision_result["repairable"])

        bool_start_revision = Path(self.temp.name) / "bool-start-revision.jsonl"
        start_run(bool_start_revision, self.plan, "BOOL-START-REV", at=1)
        start_lines = bool_start_revision.read_text(encoding="utf-8").splitlines()
        invalid_start_revision = json.loads(start_lines[0])
        invalid_start_revision["revision"] = False
        invalid_start_revision["event_hash"] = _rehash_event(invalid_start_revision)
        bool_start_revision.write_text(
            json.dumps(invalid_start_revision, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8")
        bool_start_result = doctor_ledger(bool_start_revision)
        self.assertEqual(bool_start_result["status"], "CORRUPT_NON_REPAIRABLE")
        self.assertFalse(bool_start_result["repairable"])

        contract_tamper = Path(self.temp.name) / "contract-tamper.jsonl"
        start_run(contract_tamper, self.plan, "CONTRACT-TAMPER", at=1)
        contract_lines = contract_tamper.read_text(encoding="utf-8").splitlines()
        invalid_contract = json.loads(contract_lines[0])
        invalid_contract["contract"]["mode"] = "large"
        invalid_contract["event_hash"] = _rehash_event(invalid_contract)
        contract_tamper.write_text(
            json.dumps(invalid_contract, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8")
        contract_result = doctor_ledger(contract_tamper)
        self.assertEqual(contract_result["status"], "CORRUPT_NON_REPAIRABLE")
        self.assertFalse(contract_result["repairable"])

        missing = Path(self.temp.name) / "missing.jsonl"
        with self.assertRaisesRegex(LedgerError, "does not exist"):
            _reserve_turn(missing, "MISSING", "A", "draft", 100, at=2,
                          owner_id=OWNER_ID, master_epoch=MASTER_EPOCH,
                          expected_revision=0)
        self.assertFalse(missing.exists())

    def test_schema_v2_fixture_is_readable_but_not_writable(self):
        event = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        for key in ("contract", "contract_sha256", "master_owner_id",
                    "master_epoch", "revision"):
            event.pop(key, None)
        event["ledger_schema_version"] = 2
        event["event_hash"] = _rehash_event(event)
        legacy = Path(self.temp.name) / "legacy-v2.jsonl"
        legacy.write_text(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n",
                          encoding="utf-8")
        state = read_run(legacy)
        self.assertEqual(state["ledger_schema_version"], 2)
        self.assertFalse(state["writable"])
        self.assertIsNone(state["revision"])
        before = legacy.read_bytes()
        with self.assertRaisesRegex(LedgerError, "legacy schema is read-only"):
            _reserve_turn(legacy, "RUN-1", "A", "draft", 100, at=2,
                          owner_id=OWNER_ID, master_epoch=MASTER_EPOCH,
                          expected_revision=0)
        self.assertEqual(legacy.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
