from __future__ import annotations

import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sqlite3
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from .backend import AppServerBackend, BackendError, FakeBackend, Request, Result, profile_overrides
from .lab import Lab, read_result, read_run_status, recover_stale_run
from .cli import main as cli_main


class LabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _seed_running(self, run_id: str, updated_at: float, lease_expires_at: float | None,
                      with_completed_artifact: bool = False) -> FakeBackend:
        backend = FakeBackend()
        lab = Lab(self.root, backend, "fake", "none")
        run_dir = self.root.resolve() / "lab/runs" / run_id
        run_dir.mkdir(parents=True)
        db = lab._connect(run_dir)
        db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)",
                   (run_id, "recover me", "RUNNING", "fake", "none", 2, None,
                    updated_at - 10, updated_at))
        if lease_expires_at is not None:
            db.execute("INSERT INTO run_leases VALUES(?,?,?,?)",
                       (run_id, "owner-old", updated_at, lease_expires_at))
        if with_completed_artifact:
            content = {"answer": "preserved", "evidence": [], "uncertainties": []}
            raw = json.dumps(content, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
            artifact_id = "draft-w1-a1"
            path = run_dir / "artifacts" / f"{artifact_id}.json"
            path.parent.mkdir()
            path.write_bytes(raw)
            db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (run_id, "draft-w1", 1, "draft", "w1", "DONE", None,
                        str(path), None, None))
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?)",
                       (artifact_id, run_id, "draft-w1", "draft", str(path),
                        hashlib.sha256(raw).hexdigest()))
        db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (run_id, "draft-w2", 1, "draft", "w2", "RUNNING", None,
                    None, None, None))
        db.commit()
        db.close()
        return backend

    def test_expired_lease_recovery_aborts_without_retry_and_preserves_artifacts(self):
        backend = self._seed_running("stale-lease", 900, 950, with_completed_artifact=True)
        recovered = recover_stale_run(self.root, "stale-lease", now=1000)
        self.assertEqual(recovered["status"], "ABORT")
        self.assertEqual(recovered["attempts_marked_outcome_unknown"], 1)
        self.assertEqual(backend.calls, [])
        result = read_result(self.root, "stale-lease")
        self.assertEqual(result["status"], "ABORT")
        self.assertIsNone(result["final"])
        self.assertEqual(result["attempts"][0]["status"], "DONE")
        self.assertEqual(result["attempts"][1]["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(result["attempts"][1]["error_kind"], "outcome_unknown")
        self.assertEqual(len(result["artifacts"]), 1)
        self.assertEqual(result["recoveries"][0]["attempts_affected"], 1)

    def test_active_lease_refuses_recovery_and_leaves_state_unchanged(self):
        self._seed_running("active-lease", 990, 1100)
        with self.assertRaises(BackendError) as raised:
            recover_stale_run(self.root, "active-lease", now=1000)
        self.assertEqual(raised.exception.kind, "active_lease")
        result = read_result(self.root, "active-lease")
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(result["attempts"][0]["status"], "RUNNING")
        self.assertEqual(result["recoveries"], [])

    def test_missing_lease_needs_staleness_threshold(self):
        self._seed_running("recent-no-lease", 950, None)
        with self.assertRaises(BackendError) as raised:
            recover_stale_run(self.root, "recent-no-lease", stale_after_seconds=100, now=1000)
        self.assertEqual(raised.exception.kind, "recent_run")
        self._seed_running("stale-no-lease", 800, None)
        recovered = recover_stale_run(self.root, "stale-no-lease",
                                      stale_after_seconds=100, now=1000)
        self.assertTrue(recovered["recovered"])
        self.assertEqual(read_result(self.root, "stale-no-lease")["status"], "ABORT")

    def test_recover_cli_reports_active_lease_without_mutating_it(self):
        current = time.time()
        self._seed_running("cli-active", current, current + 300)
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli_main(["--root", str(self.root), "recover", "cli-active",
                             "--stale-after-seconds", "100"])
        response = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertFalse(response["recovered"])
        self.assertIn("active_lease", response["error"])
        self.assertEqual(read_run_status(self.root, "cli-active"), "RUNNING")

    def test_recovered_abort_never_exposes_completed_synthesis_as_final(self):
        self._seed_running("late-final", 800, 900)
        run_dir = self.root.resolve() / "lab/runs/late-final"
        content = {"answer": "committed before crash", "limitations": []}
        raw = json.dumps(content, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
        artifact_id = "synthesis-a1"
        path = run_dir / "artifacts" / f"{artifact_id}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        db = sqlite3.connect(run_dir / "state.sqlite")
        db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
                   ("late-final", "synthesis", 1, "synthesis", "leader", "DONE",
                    None, str(path), None, None))
        db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?)",
                   (artifact_id, "late-final", "synthesis", "synthesis", str(path),
                    hashlib.sha256(raw).hexdigest()))
        db.commit()
        db.close()
        recover_stale_run(self.root, "late-final", now=1000)
        result = read_result(self.root, "late-final")
        self.assertEqual(result["status"], "ABORT")
        self.assertIsNone(result["final"])
        self.assertTrue(any(a["artifact_id"] == artifact_id for a in result["artifacts"]))

    def test_second_recovery_is_rejected_without_new_audit_event(self):
        self._seed_running("recover-once", 800, 900)
        recover_stale_run(self.root, "recover-once", now=1000)
        with self.assertRaises(BackendError) as raised:
            recover_stale_run(self.root, "recover-once", now=1100)
        self.assertEqual(raised.exception.kind, "not_running")
        self.assertEqual(len(read_result(self.root, "recover-once")["recoveries"]), 1)

    def test_recover_cli_unknown_run_returns_stable_json(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli_main(["--root", str(self.root), "recover", "unknown-run"])
        response = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(response["status"], "UNKNOWN")
        self.assertFalse(response["recovered"])

    def test_recovery_revokes_old_owner_before_it_can_continue(self):
        self._seed_running("revoked-owner", 800, 900)
        lab = Lab(self.root, FakeBackend(), "fake", "none")
        lab._owner_id = "owner-old"
        lab._lease_seconds = 300
        db = lab._connect(self.root.resolve() / "lab/runs/revoked-owner")
        recover_stale_run(self.root, "revoked-owner", now=1000)
        try:
            with self.assertRaises(BackendError) as raised:
                lab._touch_lease(db, "revoked-owner")
        finally:
            db.close()
        self.assertEqual(raised.exception.kind, "lease_lost")
        result = read_result(self.root, "revoked-owner")
        self.assertEqual(result["status"], "ABORT")
        self.assertEqual(result["attempts"][0]["status"], "OUTCOME_UNKNOWN")

    def test_fake_full_chain_and_isolated_inputs(self):
        fake = FakeBackend()
        result = Lab(self.root, fake, "fake", "none").run("compare three tools", "full")
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["calls"], 5)
        self.assertEqual([phase for phase, _, _ in fake.calls],
                         ["draft", "draft", "review", "review", "synthesis"])
        run_dir = self.root / "lab/runs/full"
        w2_input = json.loads((run_dir / "workspaces/draft-w2/1/input.json").read_text())
        self.assertNotIn("candidate", w2_input)
        self.assertNotIn("w1", json.dumps(w2_input))
        review_input = json.loads((run_dir / "workspaces/review-w1/1/input.json").read_text())
        self.assertEqual(review_input["candidate_id"], "w1")
        for artifact in read_result(self.root, "full")["artifacts"]:
            data = Path(artifact["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), artifact["sha256"])

    def test_retry_is_new_attempt_and_counts_toward_budget(self):
        fake = FakeBackend(fail_once={"draft-w1"})
        result = Lab(self.root, fake, "fake", "none").run("compare tools", "retry")
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["calls"], 6)
        self.assertIn(("draft", "draft-w1", 2), fake.calls)
        db = sqlite3.connect(self.root / "lab/runs/retry/state.sqlite")
        rows = db.execute("SELECT attempt,status FROM attempts WHERE task_id='draft-w1' ORDER BY attempt").fetchall()
        db.close()
        self.assertEqual(rows, [(1, "FAILED"), (2, "DONE")])

    def test_replaying_same_attempt_does_not_call_backend(self):
        fake = FakeBackend()
        lab = Lab(self.root, fake, "fake", "none")
        lab.run("compare tools", "repeat")
        count = len(fake.calls)
        run_dir = self.root / "lab/runs/repeat"
        db = lab._connect(run_dir)
        try:
            output = lab._execute(db, run_dir, "repeat", "draft", "draft-w1", "w1",
                                  {"goal": "compare tools"}, reserve_after=2)
        finally:
            db.close()
        self.assertIn("answer", output)
        self.assertEqual(len(fake.calls), count)

    def test_tampered_draft_cannot_be_replayed_or_returned(self):
        fake = FakeBackend()
        lab = Lab(self.root, fake, "fake", "none")
        lab.run("compare tools", "tampered-draft")
        count = len(fake.calls)
        run_dir = self.root / "lab/runs/tampered-draft"
        (run_dir / "artifacts/draft-w1-a1.json").write_text('{"answer":"forged"}', encoding="utf-8")
        db = lab._connect(run_dir)
        try:
            with self.assertRaises(BackendError) as raised:
                lab._execute(db, run_dir, "tampered-draft", "draft", "draft-w1", "w1",
                             {"goal": "compare tools"}, reserve_after=2)
        finally:
            db.close()
        self.assertEqual(raised.exception.kind, "artifact_integrity")
        self.assertEqual(len(fake.calls), count)
        with self.assertRaises(BackendError) as raised:
            read_result(self.root, "tampered-draft")
        self.assertEqual(raised.exception.kind, "artifact_integrity")

    def test_tampered_final_result_fails_cleanly_and_stop_reads_status(self):
        Lab(self.root, FakeBackend(), "fake", "none").run("compare tools", "tampered-final")
        final_path = self.root / "lab/runs/tampered-final/artifacts/synthesis-a1.json"
        final_path.write_text('{"answer":"forged","limitations":[]}', encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli_main(["--root", str(self.root), "result", "tampered-final"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "INTEGRITY_ERROR")
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli_main(["--root", str(self.root), "stop", "tampered-final"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output.getvalue())["cancel_requested"])

    def test_symlink_artifact_is_rejected_even_with_matching_hash(self):
        Lab(self.root, FakeBackend(), "fake", "none").run("compare tools", "linked")
        final_path = self.root / "lab/runs/linked/artifacts/synthesis-a1.json"
        outside = self.root / "same-content.json"
        outside.write_bytes(final_path.read_bytes())
        final_path.unlink()
        final_path.symlink_to(outside)
        with self.assertRaises(BackendError) as raised:
            read_result(self.root, "linked")
        self.assertEqual(raised.exception.kind, "artifact_integrity")

    def test_missing_artifact_record_cannot_make_completed_run_look_valid(self):
        Lab(self.root, FakeBackend(), "fake", "none").run("compare tools", "missing-record")
        db = sqlite3.connect(self.root / "lab/runs/missing-record/state.sqlite")
        try:
            db.execute("DELETE FROM artifacts WHERE artifact_id='draft-w1-a1'")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(BackendError) as raised:
            read_result(self.root, "missing-record")
        self.assertEqual(raised.exception.kind, "artifact_integrity")

    def test_total_failure_aborts_without_final(self):
        class FailingBackend(FakeBackend):
            def run(self, request, stop_file):
                self.calls.append((request.phase, request.task_id, request.attempt))
                raise BackendError("backend_failure", "offline")
        fake = FailingBackend()
        result = Lab(self.root, fake, "fake", "none").run("compare tools", "failed")
        self.assertEqual(result["status"], "ABORT")
        self.assertIsNone(read_result(self.root, "failed")["final"])

    def test_timeout_retries_once(self):
        fake = FakeBackend(error_once={"review-w1": "timeout"})
        result = Lab(self.root, fake, "fake", "none").run("compare tools", "timed")
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["calls"], 6)
        self.assertIn(("review", "review-w1", 2), fake.calls)

    def test_invalid_candidate_is_dropped_and_disclosed(self):
        fake = FakeBackend(invalid_once={"draft-w1"})
        result = Lab(self.root, fake, "fake", "none").run("compare tools", "invalid")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("draft w1: invalid_output", result["errors"])
        self.assertEqual(result["final"]["answer"], "Synthesis of w2")
        rejected = read_result(self.root, "invalid")["rejected_results"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["error_kind"], "invalid_output")
        self.assertEqual(rejected[0]["result"]["content"], {})

    def test_wrong_review_target_is_rejected(self):
        class WrongReview(FakeBackend):
            def run(self, request, stop_file):
                result = super().run(request, stop_file)
                if request.task_id == "review-w1":
                    return Result({"verdict": "PASS", "issues": [], "reviewed": "w2"},
                                  request.model_id, result.usage)
                return result
        result = Lab(self.root, WrongReview(), "fake", "none").run("compare tools", "wrong-review")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("review w1: invalid_output", result["errors"])
        self.assertEqual(result["final"]["answer"], "Synthesis of w2")
        rejected = read_result(self.root, "wrong-review")["rejected_results"]
        self.assertEqual(rejected[0]["result"]["content"]["reviewed"], "w2")

    def test_unresolved_blocker_is_disclosed_and_cannot_be_done(self):
        class BlockerReview(FakeBackend):
            def run(self, request, stop_file):
                result = super().run(request, stop_file)
                if request.task_id == "review-w1":
                    return Result({"verdict": "ISSUES", "issues": [{
                        "severity": "BLOCKER", "problem": "unsupported claim",
                        "evidence": "candidate.answer"}], "reviewed": "w1"},
                        request.model_id, result.usage)
                return result
        result = Lab(self.root, BlockerReview(), "fake", "none").run("compare tools", "blocker")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("unresolved blocker w1-B1", result["errors"])
        self.assertTrue(any("w1-B1" in note for note in result["final"]["limitations"]))

    def test_synthesis_cannot_omit_unresolved_blocker(self):
        class MissingBlocker(FakeBackend):
            def run(self, request, stop_file):
                result = super().run(request, stop_file)
                if request.task_id == "review-w1":
                    return Result({"verdict": "ISSUES", "issues": [{
                        "severity": "BLOCKER", "problem": "unsupported claim",
                        "evidence": "candidate.answer"}], "reviewed": "w1"},
                        request.model_id, result.usage)
                if request.phase == "synthesis":
                    return Result({"answer": "all clear", "limitations": []},
                                  request.model_id, result.usage)
                return result
        result = Lab(self.root, MissingBlocker(), "fake", "none").run("compare tools", "omit-blocker")
        self.assertEqual(result["status"], "ABORT")
        self.assertIsNone(read_result(self.root, "omit-blocker")["final"])

    def test_budget_protects_minimum_result(self):
        fake = FakeBackend()
        result = Lab(self.root, fake, "fake", "none", max_llm_calls=3).run("compare tools", "small")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["calls"], 3)
        self.assertEqual([phase for phase, _, _ in fake.calls], ["draft", "review", "synthesis"])
        self.assertEqual(len(read_result(self.root, "small")["artifacts"]), 3)

    def test_stop_prevents_following_calls(self):
        fake = FakeBackend(delay=0.25)
        result_box = {}
        thread = threading.Thread(target=lambda: result_box.update(
            Lab(self.root, fake, "fake", "none").run("compare tools", "stoptest")))
        thread.start()
        stop = self.root / "lab/runs/stoptest/STOP"
        for _ in range(100):
            if (self.root / "lab/runs/stoptest/state.sqlite").is_file():
                break
            time.sleep(0.01)
        stop.touch()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_box["status"], "STOPPED")
        self.assertLessEqual(len(fake.calls), 1)

    def test_watchdog_renews_lease_during_blocking_backend_call(self):
        class ObservingBackend(FakeBackend):
            def __init__(self):
                super().__init__(delay=0.08)
                self.lease_was_renewed = False

            def run(self, request, stop_file):
                db_path = request.workspace.parents[2] / "state.sqlite"
                with sqlite3.connect(db_path) as db:
                    before = db.execute(
                        "SELECT heartbeat_at FROM run_leases WHERE run_id=?",
                        ("watchdog-renew",),
                    ).fetchone()[0]
                result = super().run(request, stop_file)
                with sqlite3.connect(db_path) as db:
                    after = db.execute(
                        "SELECT heartbeat_at FROM run_leases WHERE run_id=?",
                        ("watchdog-renew",),
                    ).fetchone()[0]
                self.lease_was_renewed = after > before
                return result

        backend = ObservingBackend()
        lab = Lab(self.root, backend, "fake", "none", max_retries=0)
        lab._heartbeat_interval_seconds = 0.01
        result = lab.run("watchdog renewal", "watchdog-renew")
        self.assertEqual(result["status"], "DONE")
        self.assertTrue(backend.lease_was_renewed)

    def test_real_process_kill_recovers_without_replaying_unknown_turn(self):
        script = """
import sys
from pathlib import Path
from orchestrator.v101.backend import FakeBackend
from orchestrator.v101.lab import Lab
Lab(Path(sys.argv[1]), FakeBackend(delay=5), 'fake', 'none',
    max_retries=0).run('hard kill', 'hard-kill')
"""
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.root)],
            cwd=str(Path(__file__).resolve().parents[2]),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        db_path = self.root / "lab/runs/hard-kill/state.sqlite"
        saw_running = False
        try:
            for _ in range(300):
                if db_path.is_file():
                    try:
                        with sqlite3.connect(db_path) as db:
                            row = db.execute(
                                "SELECT COUNT(*) FROM attempts WHERE status='RUNNING'"
                            ).fetchone()
                        if row[0] == 1:
                            saw_running = True
                            break
                    except sqlite3.Error:
                        pass
                time.sleep(0.01)
            self.assertTrue(saw_running)
        finally:
            process.kill()
            process.wait(timeout=3)

        recovered = recover_stale_run(
            self.root, "hard-kill", now=time.time() + 1000)
        self.assertEqual(recovered["status"], "ABORT")
        result = read_result(self.root, "hard-kill")
        self.assertEqual(result["calls"], 1)
        self.assertEqual(result["attempts"][0]["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(result["attempts"][0]["attempt"], 1)

    def test_real_tcp_disconnect_after_submission_aborts_without_retry(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        received = threading.Event()

        def drop_connection():
            try:
                connection, _ = server.accept()
                with connection:
                    if connection.recv(4096):
                        received.set()
            finally:
                server.close()

        server_thread = threading.Thread(target=drop_connection, daemon=True)
        server_thread.start()

        class TcpDisconnectBackend(FakeBackend):
            def run(self, request, stop_file):
                self.calls.append((request.phase, request.task_id,
                                   request.attempt))
                with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
                    client.sendall(json.dumps(request.payload).encode("utf-8"))
                    try:
                        response = client.recv(4096)
                    except ConnectionError as exc:
                        raise BackendError(
                            "outcome_unknown",
                            "TCP connection dropped after submission") from exc
                    if not response:
                        raise BackendError(
                            "outcome_unknown",
                            "TCP connection dropped after submission")
                raise AssertionError("disconnect server unexpectedly replied")

        backend = TcpDisconnectBackend()
        result = Lab(
            self.root, backend, "fake", "none", max_retries=1).run(
                "network disconnect", "tcp-disconnect")
        server_thread.join(timeout=3)
        self.assertFalse(server_thread.is_alive())
        self.assertTrue(received.is_set())
        self.assertEqual(result["status"], "ABORT")
        self.assertIn("outcome_unknown", result["error"])
        self.assertEqual(len(backend.calls), 1)
        recorded = read_result(self.root, "tcp-disconnect")
        self.assertEqual(recorded["calls"], 1)
        self.assertEqual(recorded["attempts"][0]["error_kind"],
                         "outcome_unknown")

    def test_manual_model_requires_explicit_authorization(self):
        with self.assertRaises(BackendError):
            AppServerBackend("expensive-model", "high", {"expensive-model"})
        allowed = AppServerBackend("expensive-model", "high", {"expensive-model"}, allow_manual=True)
        self.assertEqual(allowed.model_id, "expensive-model")

    def test_model_usage_identity_requires_matching_billed_group(self):
        good = {"threadUsage": {"threadId": "thread-1", "groups": [
            {"model": "gpt-5.6-sol", "reasoningEffort": "high", "totalTokens": 100}]}}
        evidence = AppServerBackend._verified_usage_group(good, "thread-1", "gpt-5.6-sol", "high")
        self.assertEqual(evidence["source"], "account/usage/read")
        for bad in (
            {"threadUsage": None},
            {"threadUsage": {"threadId": "thread-1", "groups": []}},
            {"threadUsage": {"threadId": "thread-1", "groups": None}},
            {"threadUsage": {"threadId": "thread-1", "groups": [
                {"model": "gpt-6-astra", "reasoningEffort": "high", "totalTokens": 100}]}},
            {"threadUsage": {"threadId": "thread-1", "groups": [
                {"model": "gpt-5.6-sol", "reasoningEffort": "low", "totalTokens": 100}]}},
        ):
            with self.subTest(bad=bad), self.assertRaises(BackendError):
                AppServerBackend._verified_usage_group(bad, "thread-1", "gpt-5.6-sol", "high")

    def test_identity_preflight_requires_prior_thread_model_and_effort_group(self):
        good = {"threadUsage": {"threadId": "prior", "groups": [
            {"model": "gpt-5.6-sol", "reasoningEffort": "high", "totalTokens": 10}]}}
        self.assertEqual(AppServerBackend._probe_identity_source(good, "prior")["groupCount"], 1)
        for bad in ({"threadUsage": None},
                    {"threadUsage": {"threadId": "prior", "groups": None}},
                    {"threadUsage": {"threadId": "other", "groups": good["threadUsage"]["groups"]}},
                    {"threadUsage": {"threadId": "prior", "groups": [
                        {"model": "gpt-5.6-sol", "totalTokens": 10}]}}):
            with self.subTest(bad=bad), self.assertRaises(BackendError):
                AppServerBackend._probe_identity_source(bad, "prior")

    def test_model_policy_failure_stops_before_next_worker(self):
        class WrongModelBackend(FakeBackend):
            def run(self, request, stop_file):
                self.calls.append((request.phase, request.task_id, request.attempt))
                completed = Result({"answer": "paid response", "evidence": [],
                                    "uncertainties": []}, request.model_id,
                                   {"totalTokens": 42}, None)
                raise BackendError("model_policy", "identity evidence unavailable",
                                   {"totalTokens": 42}, completed)

        backend = WrongModelBackend()
        result = Lab(self.root, backend, "fake", "none").run("compare tools", "policy")
        self.assertEqual(result["status"], "ABORT")
        self.assertEqual(len(backend.calls), 1)
        evidence = read_result(self.root, "policy")
        self.assertEqual(evidence["calls"], 1)
        self.assertEqual(evidence["attempts"][0]["usage"]["totalTokens"], 42)
        self.assertEqual(evidence["rejected_results"][0]["result"]["content"]["answer"],
                         "paid response")

    def test_uncertain_or_internal_backend_failure_stops_before_second_worker(self):
        for kind in ("outcome_unknown", "internal_error"):
            with self.subTest(kind=kind):
                class UncertainBackend(FakeBackend):
                    def run(self, request, stop_file):
                        self.calls.append((request.phase, request.task_id, request.attempt))
                        if kind == "internal_error":
                            raise RuntimeError("unexpected transport failure")
                        raise BackendError(kind, "turn may have executed", {"totalTokens": 9})

                backend = UncertainBackend()
                run_id = kind.replace("_", "-")
                result = Lab(self.root, backend, "fake", "none").run("compare tools", run_id)
                self.assertEqual(result["status"], "ABORT")
                self.assertEqual(len(backend.calls), 1)
                attempt = read_result(self.root, run_id)["attempts"][0]
                self.assertEqual(attempt["status"], "FAILED")
                self.assertEqual(attempt["error_kind"], kind)

    def test_final_message_prefers_final_event_and_deduplicates_turn_items(self):
        final = {"id": "msg-1", "type": "agentMessage", "phase": "final_answer", "text": '{"answer":"ok"}'}
        commentary = {"id": "msg-0", "type": "agentMessage", "phase": "commentary", "text": "working"}
        self.assertEqual(AppServerBackend._final_message({"items": [final]}, [commentary, final]),
                         '{"answer":"ok"}')
        self.assertEqual(AppServerBackend._final_message({"items": [
            dict(final, text='{"answer":"stale"}')]}, [final]), '{"answer":"ok"}')
        with self.assertRaises(BackendError):
            AppServerBackend._final_message({"items": [commentary]}, [])
        self.assertEqual(AppServerBackend._final_message({"items": [
            {"id": "legacy", "type": "agentMessage", "text": "{}"}]}, []), "{}")
        anonymous = {"type": "agentMessage", "phase": "final_answer", "text": "{}"}
        self.assertEqual(AppServerBackend._final_message({"items": [anonymous]}, [anonymous]), "{}")
        with self.assertRaises(BackendError):
            AppServerBackend._final_message({"items": [anonymous, dict(anonymous, text='{"x":1}') ]}, [])

    def test_real_backend_identity_read_failure_is_not_retryable(self):
        backend = AppServerBackend("gpt-5.6-sol", "high")
        thread_id = "thread-1"
        turn_id = "turn-1"
        class Proc:
            _lab_notifications = [
                {"method": "thread/tokenUsage/updated", "params": {
                    "threadId": "other-thread", "turnId": turn_id,
                    "tokenUsage": {"last": {"totalTokens": 999}}}},
                {"method": "thread/tokenUsage/updated", "params": {
                    "threadId": thread_id, "turnId": turn_id,
                    "tokenUsage": {"last": {"totalTokens": 9}}}},
                {"method": "item/completed", "params": {
                    "threadId": thread_id, "turnId": turn_id,
                    "item": {"type": "agentMessage", "id": "final", "phase": "final_answer",
                             "text": '{"answer":"ok","evidence":[],"uncertainties":[]}'}}},
                {"method": "turn/completed", "params": {
                    "threadId": thread_id, "turn": {"id": turn_id, "status": "completed", "items": []}}},
            ]
        calls = []
        def fake_call(proc, request_id, method, params, timeout):
            calls.append(method)
            if method == "thread/start":
                return {"activePermissionProfile": {"id": "lab-worker"},
                        "model": "gpt-5.6-sol", "thread": {"id": thread_id}}
            if method == "turn/start":
                return {"turn": {"id": turn_id}}
            if method == "account/usage/read":
                raise BackendError("backend_failure", "usage service unavailable")
            raise AssertionError(method)
        backend.list_models = lambda: [{"model": "gpt-5.6-sol", "efforts": ["high"], "hidden": False}]
        backend._start = lambda cwd, restricted: Proc()
        backend._init = lambda proc: None
        backend._close = lambda proc: None
        backend._call = fake_call
        workspace = self.root / "w1"
        workspace.mkdir()
        request = Request("run", "draft-w1", 1, "draft", "w1", "gpt-5.6-sol",
                          "high", workspace, {"goal": "short"}, 5)
        with self.assertRaises(BackendError) as raised:
            backend.run(request, self.root / "STOP")
        self.assertEqual(raised.exception.kind, "model_policy")
        self.assertEqual(raised.exception.usage["totalTokens"], 9)
        self.assertEqual(raised.exception.result.content["answer"], "ok")
        self.assertIsNone(raised.exception.result.model_evidence)
        self.assertEqual(calls, ["thread/start", "turn/start", "account/usage/read"])

    def test_real_backend_lost_turn_start_response_is_outcome_unknown(self):
        backend = AppServerBackend("gpt-5.6-sol", "high")
        class Proc:
            _lab_notifications = []
        def fake_call(proc, request_id, method, params, timeout):
            if method == "thread/start":
                return {"activePermissionProfile": {"id": "lab-worker"},
                        "model": "gpt-5.6-sol", "thread": {"id": "thread-1"}}
            raise BackendError("timeout", "turn/start response lost")
        backend.list_models = lambda: [{"model": "gpt-5.6-sol", "efforts": ["high"], "hidden": False}]
        backend._start = lambda cwd, restricted: Proc()
        backend._init = lambda proc: None
        backend._close = lambda proc: None
        backend._call = fake_call
        workspace = self.root / "w1"
        workspace.mkdir()
        request = Request("run", "draft-w1", 1, "draft", "w1", "gpt-5.6-sol",
                          "high", workspace, {"goal": "short"}, 5)
        with self.assertRaises(BackendError) as raised:
            backend.run(request, self.root / "STOP")
        self.assertEqual(raised.exception.kind, "outcome_unknown")

    def test_real_pipe_disconnect_after_turn_submission_is_outcome_unknown(self):
        server = self.root / "disconnect_server.py"
        server.write_text("""
import json
import os
import sys
for line in sys.stdin:
    message = json.loads(line)
    if 'id' not in message:
        continue
    method = message.get('method')
    if method == 'initialize':
        result = {}
    elif method == 'model/list':
        result = {'data': [{'model': 'gpt-5.6-sol', 'hidden': False,
                            'supportedReasoningEfforts': [
                                {'reasoningEffort': 'high'}]}]}
    elif method == 'thread/start':
        result = {'activePermissionProfile': {'id': 'lab-worker'},
                  'model': 'gpt-5.6-sol', 'thread': {'id': 'thread-pipe'}}
    elif method == 'turn/start':
        os._exit(0)
    else:
        result = {}
    print(json.dumps({'id': message['id'], 'result': result}), flush=True)
""", encoding="utf-8")
        backend = AppServerBackend("gpt-5.6-sol", "high")

        def start(_cwd, _restricted):
            process = subprocess.Popen(
                [sys.executable, str(server)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1)
            process._lab_buffer = b""
            process._lab_notifications = []
            return process

        backend._start = start
        workspace = self.root / "pipe-workspace"
        workspace.mkdir()
        request = Request(
            "run", "draft-w1", 1, "draft", "w1", "gpt-5.6-sol",
            "high", workspace, {"goal": "short"}, 5)
        with self.assertRaises(BackendError) as raised:
            backend.run(request, self.root / "STOP")
        self.assertEqual(raised.exception.kind, "outcome_unknown")
        self.assertIn("turn/start", str(raised.exception))

    def test_profile_is_per_workspace_and_has_no_network(self):
        # The package itself may be extracted under a temporary directory for
        # validation. Use a stable non-temporary example here; real Worker
        # workspaces under runtime temp roots must still be rejected below.
        a = Path("/opt/sue-agent-lab-test-a")
        b = Path("/opt/sue-agent-lab-test-b")
        options = profile_overrides(a)
        joined = " ".join(options)
        self.assertIn(str(a), joined)
        self.assertNotIn(str(b), joined)
        self.assertIn("network.enabled=false", joined)
        self.assertIn('":root"="deny"', joined)
        with self.assertRaises(BackendError):
            profile_overrides(self.root / "temporary-worker")

        backend = AppServerBackend("fake", "none")
        starts = []
        backend._start = lambda cwd, restricted: starts.append((cwd, restricted)) or object()
        backend._init = lambda proc: None
        backend._call = lambda proc, request_id, method, params, timeout: {"data": []}
        backend._close = lambda proc: None
        self.assertEqual(backend.list_models(), [])
        self.assertEqual(starts, [(Path(tempfile.gettempdir()), False)])


if __name__ == "__main__":
    unittest.main()
