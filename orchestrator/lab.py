"""Single-writer, two-Worker walking skeleton."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .backend import Backend, BackendError, Request, Result


def _json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _verified_artifact(run_dir: Path, artifact_id: str, path: str, sha256: str) -> dict:
    expected = run_dir.resolve() / "artifacts" / f"{artifact_id}.json"
    if Path(path) != expected:
        raise BackendError("artifact_integrity", f"artifact path changed: {artifact_id}")
    try:
        if expected.resolve(strict=True) != expected or not expected.is_file():
            raise BackendError("artifact_integrity", f"artifact path is unsafe: {artifact_id}")
        raw = expected.read_bytes()
    except OSError as exc:
        raise BackendError("artifact_integrity", f"artifact missing or unreadable: {artifact_id}") from exc
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise BackendError("artifact_integrity", f"artifact hash mismatch: {artifact_id}")
    try:
        content = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise BackendError("artifact_integrity", f"artifact JSON invalid: {artifact_id}") from exc
    if not isinstance(content, dict):
        raise BackendError("artifact_integrity", f"artifact content invalid: {artifact_id}")
    return content


def _verified_rejected_result(run_dir: Path, artifact_id: str,
                              path: str, sha256: str) -> dict:
    expected = run_dir.resolve() / "rejected-results" / f"{artifact_id}.json"
    if Path(path) != expected:
        raise BackendError("artifact_integrity", f"rejected-result path changed: {artifact_id}")
    try:
        if expected.resolve(strict=True) != expected or not expected.is_file():
            raise BackendError("artifact_integrity", f"rejected-result path is unsafe: {artifact_id}")
        raw = expected.read_bytes()
    except OSError as exc:
        raise BackendError("artifact_integrity", f"rejected result missing: {artifact_id}") from exc
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise BackendError("artifact_integrity", f"rejected-result hash mismatch: {artifact_id}")
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise BackendError("artifact_integrity", f"rejected-result JSON invalid: {artifact_id}") from exc
    if not isinstance(value, dict) or "content" not in value:
        raise BackendError("artifact_integrity", f"rejected-result content invalid: {artifact_id}")
    return value


class Lab:
    def __init__(self, root: Path, backend: Backend, model_id: str, effort: str,
                 max_llm_calls: int = 6, max_retries: int = 1, max_minutes: int = 20,
                 turn_timeout: int = 90):
        self.root = root.resolve()
        self.backend = backend
        self.model_id = model_id
        self.effort = effort
        self.max_llm_calls = max_llm_calls
        self.max_retries = min(max(0, max_retries), 1)
        self.max_minutes = max_minutes
        self.turn_timeout = turn_timeout
        self._deadline: float | None = None
        if max_llm_calls < 3:
            raise ValueError("minimum result set needs at least three calls")

    def _connect(self, run_dir: Path) -> sqlite3.Connection:
        db = sqlite3.connect(run_dir / "state.sqlite", timeout=15)
        db.row_factory = sqlite3.Row
        db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=15000;
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL,
                model_id TEXT NOT NULL, effort TEXT NOT NULL, calls INTEGER NOT NULL,
                error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts (
                run_id TEXT NOT NULL, task_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                phase TEXT NOT NULL, actor TEXT NOT NULL, status TEXT NOT NULL,
                error_kind TEXT, artifact_path TEXT, usage_json TEXT, model_evidence_json TEXT,
                PRIMARY KEY(run_id, task_id, attempt)
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
                phase TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rejected_results (
                artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
                phase TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
                error_kind TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_leases (
                run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                heartbeat_at REAL NOT NULL, lease_expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recovery_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                prior_run_status TEXT NOT NULL, prior_updated_at REAL NOT NULL,
                owner_id TEXT, lease_expires_at REAL, recovered_at REAL NOT NULL,
                attempts_affected INTEGER NOT NULL, reason TEXT NOT NULL
            );
        """)
        db.commit()
        return db

    def _touch_lease(self, db: sqlite3.Connection, run_id: str) -> None:
        """Refresh the current Orchestrator lease without granting Workers DB access."""
        owner_id = getattr(self, "_owner_id", None)
        if owner_id is None:
            return
        now = time.time()
        changed = db.execute(
            "UPDATE run_leases SET heartbeat_at=?,lease_expires_at=? "
            "WHERE run_id=? AND owner_id=?",
            (now, now + self._lease_seconds, run_id, owner_id),
        ).rowcount
        if changed != 1:
            raise BackendError("lease_lost", "run lease is missing or owned by another process")
        db.commit()

    @contextmanager
    def _lease_watchdog(self, run_dir: Path, run_id: str):
        """Renew the Orchestrator lease while one backend call is blocking."""
        owner_id = getattr(self, "_owner_id", None)
        if owner_id is None:
            yield
            return
        stop = threading.Event()
        lost: list[str] = []
        interval = getattr(
            self, "_heartbeat_interval_seconds",
            max(1.0, min(30.0, self._lease_seconds / 3)),
        )

        def heartbeat() -> None:
            db_path = run_dir / "state.sqlite"
            while not stop.wait(interval):
                connection = None
                try:
                    connection = sqlite3.connect(db_path, timeout=15)
                    now = time.time()
                    changed = connection.execute(
                        "UPDATE run_leases SET heartbeat_at=?,lease_expires_at=? "
                        "WHERE run_id=? AND owner_id=?",
                        (now, now + self._lease_seconds, run_id, owner_id),
                    ).rowcount
                    connection.commit()
                    if changed != 1:
                        lost.append("run lease is missing or owned by another process")
                        (run_dir / "STOP").touch(exist_ok=True)
                        return
                except Exception as exc:  # Fail closed; main thread reports lease_lost.
                    lost.append(f"watchdog heartbeat failed: {type(exc).__name__}")
                    (run_dir / "STOP").touch(exist_ok=True)
                    return
                finally:
                    if connection is not None:
                        connection.close()

        thread = threading.Thread(
            target=heartbeat, name=f"lease-watchdog-{run_id}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=max(1.0, interval * 2))
        if lost:
            raise BackendError("lease_lost", lost[0])

    @staticmethod
    def _update(db: sqlite3.Connection, run_id: str, status: str, error: str | None = None) -> None:
        db.execute("UPDATE runs SET status=?, error=?, updated_at=? WHERE run_id=?",
                   (status, error, time.time(), run_id))
        db.commit()

    @staticmethod
    def _validate(phase: str, content: dict) -> None:
        if not isinstance(content, dict):
            raise BackendError("invalid_output", "backend content must be an object")
        if phase == "draft" and (not isinstance(content.get("answer"), str) or
                                 not content.get("answer", "").strip() or
                                 not isinstance(content.get("evidence"), list) or
                                 not all(isinstance(x, str) for x in content.get("evidence", [])) or
                                 not isinstance(content.get("uncertainties"), list) or
                                 not all(isinstance(x, str) for x in content.get("uncertainties", []))):
            raise BackendError("invalid_output", "draft fields invalid")
        if phase == "review" and (content.get("verdict") not in ("PASS", "ISSUES") or
                                   not isinstance(content.get("issues"), list) or
                                   not isinstance(content.get("reviewed"), str)):
            raise BackendError("invalid_output", "review verdict/issues invalid")
        if phase == "review":
            issues = content["issues"]
            if (content["verdict"] == "PASS" and issues) or (content["verdict"] == "ISSUES" and not issues):
                raise BackendError("invalid_output", "review verdict does not match issues")
            for issue in issues:
                if (not isinstance(issue, dict) or
                        issue.get("severity") not in ("BLOCKER", "NON_BLOCKER") or
                        not isinstance(issue.get("problem"), str) or not issue["problem"].strip() or
                        not isinstance(issue.get("evidence"), str) or not issue["evidence"].strip()):
                    raise BackendError("invalid_output", "review issue fields invalid")
        if phase == "synthesis" and (not isinstance(content.get("answer"), str) or
                                     not content.get("answer", "").strip() or
                                     not isinstance(content.get("limitations"), list) or
                                     not all(isinstance(x, str) for x in content.get("limitations", []))):
            raise BackendError("invalid_output", "synthesis fields invalid")

    @staticmethod
    def _preserve_rejected_result(db: sqlite3.Connection, run_dir: Path, run_id: str,
                                  task_id: str, attempt: int, phase: str,
                                  error_kind: str, result: Result) -> str:
        """Keep a completed paid response as audit evidence, never as an accepted artifact."""
        artifact_id = f"{task_id}-a{attempt}-rejected"
        envelope = {
            "content": result.content,
            "actual_model": result.actual_model,
            "usage": result.usage,
            "model_evidence": result.model_evidence,
            "accepted": False,
            "error_kind": error_kind,
        }
        raw = _json(envelope).encode("utf-8")
        path = run_dir.resolve() / "rejected-results" / f"{artifact_id}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        db.execute("INSERT INTO rejected_results VALUES(?,?,?,?,?,?,?)",
                   (artifact_id, run_id, task_id, phase, str(path),
                    hashlib.sha256(raw).hexdigest(), error_kind))
        return str(path)

    def _execute(self, db: sqlite3.Connection, run_dir: Path, run_id: str, phase: str,
                 task_id: str, actor: str, payload: dict, reserve_after: int = 0) -> dict:
        stop_file = run_dir / "STOP"
        for attempt in range(1, self.max_retries + 2):
            self._touch_lease(db, run_id)
            if stop_file.exists():
                raise BackendError("cancelled", "stop requested")
            if self._deadline is not None and time.monotonic() >= self._deadline:
                raise BackendError("budget", "time budget exhausted")
            old = db.execute("SELECT status,artifact_path FROM attempts WHERE run_id=? AND task_id=? AND attempt=?",
                             (run_id, task_id, attempt)).fetchone()
            if old:
                if old["status"] == "DONE":
                    artifact_id = f"{task_id}-a{attempt}"
                    artifact = db.execute(
                        "SELECT path,sha256 FROM artifacts WHERE run_id=? AND task_id=? AND phase=? AND artifact_id=?",
                        (run_id, task_id, phase, artifact_id)).fetchone()
                    if not artifact or old["artifact_path"] != artifact["path"]:
                        raise BackendError("artifact_integrity", f"artifact record missing or changed: {artifact_id}")
                    return _verified_artifact(run_dir, artifact_id, artifact["path"], artifact["sha256"])
                if old["status"] == "RUNNING":
                    raise BackendError("duplicate", "attempt already running")
                continue
            calls = db.execute("SELECT calls FROM runs WHERE run_id=?", (run_id,)).fetchone()["calls"]
            if calls + 1 + reserve_after > self.max_llm_calls:
                raise BackendError("budget", "max_llm_calls reserve protects final result")
            workspace = run_dir / "workspaces" / task_id / str(attempt)
            workspace.mkdir(parents=True, exist_ok=False)
            (workspace / "input.json").write_text(_json(payload), encoding="utf-8")
            db.execute("INSERT INTO attempts(run_id,task_id,attempt,phase,actor,status,error_kind,artifact_path) VALUES(?,?,?,?,?,?,?,?)",
                       (run_id, task_id, attempt, phase, actor, "RUNNING", None, None))
            db.execute("UPDATE runs SET calls=calls+1,updated_at=? WHERE run_id=?", (time.time(), run_id))
            db.commit()
            request = Request(run_id, task_id, attempt, phase, actor, self.model_id,
                              self.effort, workspace, payload, self.turn_timeout)
            result: Result | None = None
            try:
                self._touch_lease(db, run_id)
                with self._lease_watchdog(run_dir, run_id):
                    result: Result = self.backend.run(request, stop_file)
                self._touch_lease(db, run_id)
                if result.actual_model != self.model_id:
                    raise BackendError("model_policy", "backend changed model")
                self._validate(phase, result.content)
                if phase == "review" and result.content["reviewed"] != payload["candidate_id"]:
                    raise BackendError("invalid_output", "review refers to a different candidate")
                if phase == "synthesis":
                    limitations = result.content["limitations"]
                    missing = [issue["id"] for issue in payload.get("unresolved_blockers", [])
                               if not any(issue["id"] in note for note in limitations)]
                    if missing:
                        raise BackendError("invalid_output", "synthesis omitted unresolved blockers")
                raw = _json(result.content).encode("utf-8")
                artifact_id = f"{task_id}-a{attempt}"
                path = run_dir / "artifacts" / f"{artifact_id}.json"
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(raw)
                db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?)",
                           (artifact_id, run_id, task_id, phase, str(path), hashlib.sha256(raw).hexdigest()))
                db.execute("UPDATE attempts SET status='DONE',artifact_path=?,usage_json=?,model_evidence_json=? WHERE run_id=? AND task_id=? AND attempt=?",
                           (str(path), _json(result.usage) if result.usage is not None else None,
                            _json(result.model_evidence) if result.model_evidence is not None else None,
                            run_id, task_id, attempt))
                db.commit()
                return result.content
            except BackendError as exc:
                self._touch_lease(db, run_id)
                rejected = exc.result if exc.result is not None else result
                rejected_path = None
                if rejected is not None:
                    rejected_path = self._preserve_rejected_result(
                        db, run_dir, run_id, task_id, attempt, phase, exc.kind, rejected)
                usage = rejected.usage if rejected is not None else exc.usage
                evidence = rejected.model_evidence if rejected is not None else None
                db.execute("UPDATE attempts SET status='FAILED',error_kind=?,artifact_path=?,usage_json=?,model_evidence_json=? WHERE run_id=? AND task_id=? AND attempt=?",
                           (exc.kind, rejected_path,
                            _json(usage) if usage is not None else None,
                            _json(evidence) if evidence is not None else None,
                            run_id, task_id, attempt))
                db.commit()
                if exc.kind not in ("timeout", "rate_limited", "backend_failure") or attempt > self.max_retries:
                    raise
                if exc.kind == "rate_limited":
                    time.sleep(min(2.0, 0.5 * attempt))
            except Exception as exc:
                self._touch_lease(db, run_id)
                db.execute("UPDATE attempts SET status='FAILED',error_kind='internal_error' WHERE run_id=? AND task_id=? AND attempt=?",
                           (run_id, task_id, attempt))
                db.commit()
                raise BackendError("internal_error", f"unexpected backend error: {type(exc).__name__}") from exc
        raise BackendError("backend_failure", "retry exhausted")

    def run(self, goal: str, run_id: str | None = None) -> dict:
        if not goal.strip():
            raise ValueError("goal is empty")
        run_id = run_id or uuid.uuid4().hex[:12]
        self._deadline = time.monotonic() + self.max_minutes * 60
        if not run_id.replace("-", "").isalnum():
            raise ValueError("invalid run id")
        run_dir = self.root / "lab" / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        db = self._connect(run_dir)
        now = time.time()
        self._owner_id = uuid.uuid4().hex
        self._lease_seconds = max(300, self.turn_timeout * 2 + 60)
        db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)",
                   (run_id, goal, "RUNNING", self.model_id, self.effort, 0, None, now, now))
        db.execute("INSERT INTO run_leases VALUES(?,?,?,?)",
                   (run_id, self._owner_id, now, now + self._lease_seconds))
        db.commit()
        criteria = [
            {"id": "C1", "type": "machine", "required": True, "description": "draft/review/final JSON fields present"},
            {"id": "C2", "type": "llm", "required": True, "description": "answer addresses the goal with reasons", "evaluator": "leader"},
            {"id": "C3", "type": "llm", "required": True, "description": "identify a plausible counterexample or failure", "evaluator": "reviewer", "reason": "prevent self-confirmation"},
        ]
        (run_dir / "rubric.json").write_text(_json(criteria), encoding="utf-8")
        drafts: dict[str, dict] = {}
        reviews: dict[str, dict] = {}
        errors: list[str] = []
        try:
            for actor in ("w1", "w2"):
                try:
                    drafts[actor] = self._execute(db, run_dir, run_id, "draft", f"draft-{actor}", actor,
                                                  {"goal": goal, "criteria": criteria}, reserve_after=2)
                except BackendError as exc:
                    if exc.kind in ("cancelled", "model_policy", "permission_denied",
                                    "outcome_unknown", "internal_error", "artifact_integrity",
                                    "lease_lost"):
                        raise
                    errors.append(f"draft {actor}: {exc.kind}")
            for author, candidate in drafts.items():
                reviewer = "w2" if author == "w1" else "w1"
                try:
                    reviews[author] = self._execute(db, run_dir, run_id, "review", f"review-{author}", reviewer,
                                                    {"goal": goal, "candidate_id": author,
                                                     "candidate": candidate, "criteria": criteria}, reserve_after=1)
                except BackendError as exc:
                    if exc.kind in ("cancelled", "model_policy", "permission_denied",
                                    "outcome_unknown", "internal_error", "artifact_integrity",
                                    "lease_lost"):
                        raise
                    errors.append(f"review {author}: {exc.kind}")
            reviewed = [a for a in drafts if a in reviews]
            if not reviewed:
                raise BackendError("minimum_result", "no candidate with independent review")
            blockers = [
                {"id": f"{author}-B{index}", "problem": issue["problem"], "evidence": issue["evidence"]}
                for author in reviewed
                for index, issue in enumerate(reviews[author]["issues"], start=1)
                if issue["severity"] == "BLOCKER"
            ]
            errors.extend(f"unresolved blocker {issue['id']}" for issue in blockers)
            final = self._execute(db, run_dir, run_id, "synthesis", "synthesis", "leader",
                                  {"goal": goal, "candidate_ids": reviewed,
                                   "candidates": {a: drafts[a] for a in reviewed},
                                   "reviews": {a: reviews[a] for a in reviewed},
                                   "criteria": criteria, "errors": errors,
                                   "unresolved_blockers": blockers})
            status = "DONE" if len(reviewed) == 2 and not errors else "PARTIAL"
            self._update(db, run_id, status)
            return {"run_id": run_id, "status": status, "final": final,
                    "errors": errors, "calls": db.execute("SELECT calls FROM runs WHERE run_id=?", (run_id,)).fetchone()["calls"]}
        except BackendError as exc:
            self._update(db, run_id, "STOPPED" if exc.kind == "cancelled" else "ABORT", f"{exc.kind}: {exc}")
            return {"run_id": run_id, "status": "STOPPED" if exc.kind == "cancelled" else "ABORT",
                    "error": f"{exc.kind}: {exc}", "errors": errors}
        finally:
            db.execute("DELETE FROM run_leases WHERE run_id=? AND owner_id=?",
                       (run_id, self._owner_id))
            db.commit()
            self._owner_id = None
            db.close()


def recover_stale_run(root: Path, run_id: str, stale_after_seconds: int = 300,
                      now: float | None = None) -> dict:
    """Explicitly seal an abandoned RUNNING run without retrying any backend call."""
    if not run_id or not run_id.replace("-", "").isalnum():
        raise ValueError("invalid run id")
    if stale_after_seconds < 0:
        raise ValueError("stale_after_seconds must be non-negative")
    run_dir = root.resolve() / "lab" / "runs" / run_id
    db_path = run_dir / "state.sqlite"
    if not db_path.is_file():
        raise FileNotFoundError(run_id)
    recovered_at = time.time() if now is None else now
    db = sqlite3.connect(db_path, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.executescript("""
            PRAGMA busy_timeout=15000;
            CREATE TABLE IF NOT EXISTS run_leases (
                run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                heartbeat_at REAL NOT NULL, lease_expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recovery_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                prior_run_status TEXT NOT NULL, prior_updated_at REAL NOT NULL,
                owner_id TEXT, lease_expires_at REAL, recovered_at REAL NOT NULL,
                attempts_affected INTEGER NOT NULL, reason TEXT NOT NULL
            );
        """)
        db.execute("BEGIN IMMEDIATE")
        run = db.execute("SELECT status,updated_at FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise FileNotFoundError(run_id)
        if run["status"] != "RUNNING":
            raise BackendError("not_running", f"run is already {run['status']}")
        lease = db.execute(
            "SELECT owner_id,heartbeat_at,lease_expires_at FROM run_leases WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if lease is not None and lease["lease_expires_at"] > recovered_at:
            raise BackendError("active_lease", "run lease is still active")
        if lease is None and recovered_at - run["updated_at"] < stale_after_seconds:
            raise BackendError("recent_run", "run has no lease but is not stale yet")
        affected = db.execute(
            "UPDATE attempts SET status='OUTCOME_UNKNOWN',error_kind='outcome_unknown' "
            "WHERE run_id=? AND status='RUNNING'",
            (run_id,),
        ).rowcount
        reason = "outcome_unknown: stale RUNNING run recovered without retry"
        db.execute(
            "INSERT INTO recovery_events(run_id,prior_run_status,prior_updated_at,owner_id,"
            "lease_expires_at,recovered_at,attempts_affected,reason) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, run["status"], run["updated_at"],
             lease["owner_id"] if lease is not None else None,
             lease["lease_expires_at"] if lease is not None else None,
             recovered_at, affected, reason),
        )
        db.execute("UPDATE runs SET status='ABORT',error=?,updated_at=? WHERE run_id=?",
                   (reason, recovered_at, run_id))
        db.execute("DELETE FROM run_leases WHERE run_id=?", (run_id,))
        db.commit()
        return {"run_id": run_id, "previous_status": "RUNNING", "status": "ABORT",
                "recovered": True, "attempts_marked_outcome_unknown": affected,
                "reason": reason}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def read_run_status(root: Path, run_id: str) -> str:
    if not run_id or not run_id.replace("-", "").isalnum():
        raise ValueError("invalid run id")
    db_path = root.resolve() / "lab" / "runs" / run_id / "state.sqlite"
    if not db_path.is_file():
        raise FileNotFoundError(run_id)
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = db.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise FileNotFoundError(run_id)
        return row[0]
    finally:
        db.close()


def read_result(root: Path, run_id: str) -> dict:
    if not run_id or not run_id.replace("-", "").isalnum():
        raise ValueError("invalid run id")
    run_dir = root.resolve() / "lab" / "runs" / run_id
    db_path = run_dir / "state.sqlite"
    if not db_path.is_file():
        raise FileNotFoundError(run_id)
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        arts = [dict(x) for x in db.execute("SELECT artifact_id,phase,path,sha256 FROM artifacts WHERE run_id=? ORDER BY artifact_id", (run_id,))]
        art_by_id = {art["artifact_id"]: art for art in arts}
        verified = {art["artifact_id"]: _verified_artifact(run_dir, art["artifact_id"],
                                                          art["path"], art["sha256"])
                    for art in arts}
        has_rejected_results = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rejected_results'"
        ).fetchone()
        rejected = ([dict(x) for x in db.execute(
            "SELECT artifact_id,task_id,phase,path,sha256,error_kind FROM rejected_results "
            "WHERE run_id=? ORDER BY artifact_id", (run_id,)
        )] if has_rejected_results else [])
        rejected_by_id = {item["artifact_id"]: item for item in rejected}
        for item in rejected:
            item["result"] = _verified_rejected_result(
                run_dir, item["artifact_id"], item["path"], item["sha256"])
        attempts = [dict(x) for x in db.execute("SELECT task_id,attempt,phase,status,error_kind,artifact_path,usage_json,model_evidence_json FROM attempts WHERE run_id=? ORDER BY rowid", (run_id,))]
        for attempt in attempts:
            artifact_path = attempt.pop("artifact_path")
            if attempt["status"] == "DONE":
                artifact_id = f"{attempt['task_id']}-a{attempt['attempt']}"
                art = art_by_id.get(artifact_id)
                if art is None or art["phase"] != attempt["phase"] or art["path"] != artifact_path:
                    raise BackendError("artifact_integrity", f"completed attempt lost artifact: {artifact_id}")
            elif attempt["status"] == "FAILED" and artifact_path:
                artifact_id = f"{attempt['task_id']}-a{attempt['attempt']}-rejected"
                item = rejected_by_id.get(artifact_id)
                if item is None or item["phase"] != attempt["phase"] or item["path"] != artifact_path:
                    raise BackendError("artifact_integrity", f"failed attempt lost rejected result: {artifact_id}")
            usage_json = attempt.pop("usage_json")
            evidence_json = attempt.pop("model_evidence_json")
            attempt["usage"] = json.loads(usage_json) if usage_json else None
            attempt["model_evidence"] = json.loads(evidence_json) if evidence_json else None
        done_synthesis_ids = {
            f"{attempt['task_id']}-a{attempt['attempt']}"
            for attempt in attempts
            if attempt["phase"] == "synthesis" and attempt["status"] == "DONE"
        }
        final_artifact = next((x for x in arts if x["artifact_id"] in done_synthesis_ids), None)
        if row["status"] in ("DONE", "PARTIAL") and final_artifact is None:
            raise BackendError("artifact_integrity", "completed run lost final artifact")
        final = (verified[final_artifact["artifact_id"]]
                 if final_artifact and row["status"] in ("DONE", "PARTIAL") else None)
        has_recovery_events = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_events'"
        ).fetchone()
        recoveries = ([dict(x) for x in db.execute(
            "SELECT prior_run_status,prior_updated_at,owner_id,lease_expires_at,"
            "recovered_at,attempts_affected,reason FROM recovery_events "
            "WHERE run_id=? ORDER BY event_id", (run_id,)
        )] if has_recovery_events else [])
        return {"run_id": run_id, "status": row["status"], "calls": row["calls"],
                "model_id": row["model_id"], "effort": row["effort"], "error": row["error"],
                "final": final, "artifacts": arts, "attempts": attempts,
                "rejected_results": rejected, "recoveries": recoveries}
    finally:
        db.close()
