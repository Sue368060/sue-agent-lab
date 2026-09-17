"""One contract for deterministic and Codex App Server backends."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import select
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Protocol


class BackendError(RuntimeError):
    def __init__(self, kind: str, message: str, usage: dict | None = None,
                 result=None):
        super().__init__(message)
        self.kind = kind
        self.usage = usage
        self.result = result


@dataclass(frozen=True)
class Request:
    run_id: str
    task_id: str
    attempt: int
    phase: str
    actor: str
    model_id: str
    effort: str
    workspace: Path
    payload: dict
    timeout: int = 120


@dataclass(frozen=True)
class Result:
    content: dict
    actual_model: str
    usage: dict | None
    model_evidence: dict | None = None


class Backend(Protocol):
    def list_models(self) -> list[dict]: ...
    def run(self, request: Request, stop_file: Path) -> Result: ...
    def close(self) -> None: ...


class FakeBackend:
    """Zero-model deterministic responses, with optional injected failures."""

    def __init__(self, fail_once: set[str] | None = None, delay: float = 0,
                 error_once: dict[str, str] | None = None, invalid_once: set[str] | None = None):
        self.fail_once = set(fail_once or ())
        self.error_once = dict(error_once or {})
        self.invalid_once = set(invalid_once or ())
        self.delay = delay
        self.calls: list[tuple[str, str, int]] = []

    def list_models(self) -> list[dict]:
        return [{"model": "fake", "efforts": ["none"], "manual_only": False}]

    def run(self, request: Request, stop_file: Path) -> Result:
        self.calls.append((request.phase, request.task_id, request.attempt))
        if request.task_id in self.fail_once:
            self.fail_once.remove(request.task_id)
            raise BackendError("backend_failure", "injected transient failure")
        if request.task_id in self.error_once:
            kind = self.error_once.pop(request.task_id)
            raise BackendError(kind, "injected " + kind)
        if request.task_id in self.invalid_once:
            self.invalid_once.remove(request.task_id)
            return Result({}, request.model_id, {"totalTokens": 0})
        until = time.monotonic() + self.delay
        while time.monotonic() < until:
            if stop_file.exists():
                raise BackendError("cancelled", "stop requested")
            time.sleep(min(0.02, until - time.monotonic()))
        if stop_file.exists():
            raise BackendError("cancelled", "stop requested")
        if request.phase == "draft":
            content = {"answer": f"{request.actor} independent answer to {request.payload['goal']}",
                       "evidence": [], "uncertainties": ["fake evidence"]}
        elif request.phase == "review":
            content = {"verdict": "PASS", "issues": [], "reviewed": request.payload["candidate_id"]}
        elif request.phase == "synthesis":
            content = {"answer": "Synthesis of " + ", ".join(request.payload["candidate_ids"]),
                       "limitations": ["Fake output is not research evidence"] +
                                      [f"Unresolved {issue['id']}: {issue['problem']}"
                                       for issue in request.payload.get("unresolved_blockers", [])]}
        else:
            raise BackendError("invalid_request", "unsupported phase")
        return Result(content, request.model_id, {"totalTokens": 0})

    def close(self) -> None:
        pass


def profile_overrides(workspace: Path) -> list[str]:
    """Per-process, nonpersistent file policy: own workspace only, no network."""
    resolved = workspace.resolve()
    for temp_root in (Path("/tmp").resolve(), Path("/private/tmp").resolve(), Path(tempfile.gettempdir()).resolve()):
        if resolved == temp_root or temp_root in resolved.parents:
            raise BackendError("permission_denied", "real Worker workspace cannot be inside a runtime temporary directory")
    path = str(resolved)
    if '"' in path or "\\" in path:
        raise BackendError("invalid_request", "unsafe workspace path")
    fs = '{":root"="deny",":minimal"="read","' + path + '"="read"}'
    return ["-c", 'default_permissions="lab-worker"',
            "-c", "permissions.lab-worker.filesystem=" + fs,
            "-c", "permissions.lab-worker.network.enabled=false"]


class AppServerBackend:
    """One ephemeral App Server per request; each has its own restricted profile."""

    def __init__(self, model_id: str, effort: str, manual_only: set[str] | None = None,
                 allow_manual: bool = False, executable: str = "codex"):
        self.model_id = model_id
        self.effort = effort
        self.manual_only = set(manual_only or ())
        self.allow_manual = allow_manual
        self.executable = executable
        if not model_id or not effort:
            raise BackendError("model_policy", "actual model ID and effort are required")
        if model_id in self.manual_only and not allow_manual:
            raise BackendError("model_policy", "manual_only model is not authorized")

    def _start(self, cwd: Path, restricted: bool) -> subprocess.Popen:
        cmd = [self.executable, "app-server"] + (profile_overrides(cwd) if restricted else [])
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, bufsize=1)
        proc._lab_buffer = b""
        proc._lab_notifications = []
        return proc

    @staticmethod
    def _read(proc: subprocess.Popen, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if b"\n" in proc._lab_buffer:
                line, proc._lab_buffer = proc._lab_buffer.split(b"\n", 1)
                if line:
                    return json.loads(line)
            ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            data = os.read(proc.stdout.fileno(), 65536)
            if not data:
                break
            proc._lab_buffer += data
        raise BackendError("timeout", "app-server did not send a message")

    @staticmethod
    def _deny_server_request(proc: subprocess.Popen, msg: dict) -> bool:
        if "id" not in msg or "method" not in msg:
            return False
        proc.stdin.write(json.dumps({"id": msg["id"], "error": {
            "code": -32601, "message": "Sue Agent Lab grants no interactive Worker permissions"}}) + "\n")
        proc.stdin.flush()
        return True

    @staticmethod
    def _call(proc: subprocess.Popen, request_id: int, method: str, params: dict, timeout: float) -> dict:
        proc.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = AppServerBackend._read(proc, max(0.01, deadline - time.monotonic()))
            if AppServerBackend._deny_server_request(proc, msg):
                continue
            if msg.get("id") == request_id:
                if "error" in msg:
                    raise BackendError("backend_failure", str(msg["error"]))
                return msg["result"]
            proc._lab_notifications.append(msg)
        raise BackendError("timeout", f"{method} did not respond")

    @staticmethod
    def _init(proc: subprocess.Popen) -> None:
        AppServerBackend._call(proc, 1, "initialize", {
            "clientInfo": {"name": "sue-agent-lab-v101", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True}}, 20)
        proc.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        proc.stdin.flush()

    @staticmethod
    def _close(proc: subprocess.Popen) -> None:
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

    def list_models(self) -> list[dict]:
        proc = self._start(Path(tempfile.gettempdir()), False)
        try:
            self._init(proc)
            data = self._call(proc, 2, "model/list", {"limit": 100}, 20)["data"]
            return [{"model": m.get("model"), "efforts": [e.get("reasoningEffort") for e in m.get("supportedReasoningEfforts", [])],
                     "hidden": m.get("hidden", False)} for m in data]
        finally:
            self._close(proc)

    @staticmethod
    def _verified_usage_group(data: dict, thread_id: str, model_id: str, effort: str) -> dict:
        thread_usage = data.get("threadUsage") or {}
        if not isinstance(thread_usage, dict):
            raise BackendError("model_policy", "invalid per-thread usage response")
        if thread_usage.get("threadId") != thread_id:
            raise BackendError("model_policy", "per-thread model usage was not available")
        groups = [g for g in (thread_usage.get("groups") or [])
                  if isinstance(g, dict) and (g.get("totalTokens") or 0) > 0]
        if not groups:
            raise BackendError("model_policy", "per-thread model usage had no token-bearing group")
        if any(g.get("model") != model_id or g.get("reasoningEffort") != effort for g in groups):
            raise BackendError("model_policy", "billed model or effort differs from run policy")
        return {"source": "account/usage/read", "threadId": thread_id, "groups": groups}

    @staticmethod
    def _probe_identity_source(data: dict, thread_id: str) -> dict:
        """Check a completed prior thread before spending another real model turn."""
        thread_usage = data.get("threadUsage") or {}
        if not isinstance(thread_usage, dict):
            raise BackendError("model_policy", "invalid identity preflight response")
        if thread_usage.get("threadId") != thread_id:
            raise BackendError("model_policy", "identity preflight has no per-thread usage")
        groups = [g for g in (thread_usage.get("groups") or [])
                  if isinstance(g, dict) and (g.get("totalTokens") or 0) > 0 and
                  isinstance(g.get("model"), str) and
                  isinstance(g.get("reasoningEffort"), str)]
        if not groups:
            raise BackendError("model_policy", "identity preflight has no model/effort usage group")
        return {"source": "account/usage/read", "threadId": thread_id, "groupCount": len(groups)}

    def check_identity_source(self, completed_thread_id: str) -> dict:
        """Read-only preflight; it does not start a thread or a model turn."""
        proc = self._start(Path(tempfile.gettempdir()), False)
        try:
            self._init(proc)
            data = self._call(proc, 2, "account/usage/read",
                              {"threadId": completed_thread_id}, 20)
            return self._probe_identity_source(data, completed_thread_id)
        finally:
            self._close(proc)

    @staticmethod
    def _final_message(turn: dict, completed_items: list[dict]) -> str:
        """Accept one final answer, deduplicating item events also present on the turn."""
        by_id = {}
        for item in (turn.get("items") or []) + completed_items:
            if item.get("type") == "agentMessage":
                item_id = item.get("id")
                key = (("id", item_id) if isinstance(item_id, str) and item_id
                       else ("anonymous", json.dumps(item, ensure_ascii=False,
                                                     sort_keys=True, separators=(",", ":"))))
                by_id[key] = item
        final = [item for item in by_id.values() if item.get("phase") == "final_answer"]
        if not final:
            final = [item for item in by_id.values() if item.get("phase") is None]
        if len(final) != 1:
            raise BackendError("invalid_output", "expected exactly one final agent message")
        text = final[0].get("text")
        if not isinstance(text, str) or not text.strip():
            raise BackendError("invalid_output", "final agent message was empty")
        return text

    def run(self, request: Request, stop_file: Path) -> Result:
        if request.model_id != self.model_id or request.effort != self.effort:
            raise BackendError("model_policy", "request model or effort differs from run policy")
        catalog = self.list_models()
        selected = next((m for m in catalog if m["model"] == self.model_id and not m["hidden"]), None)
        if not selected or self.effort not in selected["efforts"]:
            raise BackendError("model_policy", "configured model/effort unavailable; no fallback")
        if stop_file.exists():
            raise BackendError("cancelled", "stop requested")
        proc = self._start(request.workspace, True)
        try:
            self._init(proc)
            started = self._call(proc, 2, "thread/start", {
                "cwd": str(request.workspace), "permissions": "lab-worker", "approvalPolicy": "never",
                "ephemeral": True, "model": self.model_id, "allowProviderModelFallback": False}, 20)
            if started.get("activePermissionProfile", {}).get("id") != "lab-worker" or started.get("model") != self.model_id:
                raise BackendError("permission_denied", "thread policy not applied")
            thread_id = started["thread"]["id"]
            expected = {
                "draft": {"answer": "string", "evidence": "array", "uncertainties": "array"},
                "review": {"verdict": "PASS or ISSUES",
                           "issues": "array of {severity: BLOCKER or NON_BLOCKER, problem: string, evidence: string}",
                           "reviewed": "exact candidate ID"},
                "synthesis": {"answer": "string",
                              "limitations": "array naming every unresolved blocker ID from payload"},
            }
            prompt = json.dumps({"phase": request.phase, "actor": request.actor, "payload": request.payload,
                                 "expected_fields": expected[request.phase],
                                 "instruction": "Return one JSON object only. Apply the rubric, state unsupported claims as uncertain, and do not use tools or external network."}, ensure_ascii=False)
            turn_submitted = True  # A lost turn/start response may still mean the server ran it.
            try:
                started_turn = self._call(proc, 3, "turn/start", {
                    "threadId": thread_id, "input": [{"type": "text", "text": prompt}],
                    "model": self.model_id, "effort": self.effort}, 20)
            except BackendError as exc:
                raise BackendError("outcome_unknown", f"turn/start outcome unknown: {exc}") from exc
            turn_id = (started_turn.get("turn") or {}).get("id")
            if not turn_id:
                raise BackendError("outcome_unknown", "turn/start returned no turn ID")
            deadline = time.monotonic() + request.timeout
            usage = None
            completed_items: list[dict] = []
            try:
                while time.monotonic() < deadline:
                    if stop_file.exists():
                        raise BackendError("cancelled", "stop requested; process terminated", usage)
                    if proc._lab_notifications:
                        msg = proc._lab_notifications.pop(0)
                    else:
                        try:
                            msg = self._read(proc, 0.2)
                        except BackendError as exc:
                            if exc.kind == "timeout":
                                continue
                            raise
                    if self._deny_server_request(proc, msg):
                        continue
                    method = msg.get("method")
                    params = msg.get("params", {})
                    if method == "model/rerouted" and params.get("threadId") == thread_id:
                        raise BackendError("model_policy", "model was rerouted during turn", usage)
                    if method == "thread/tokenUsage/updated" and (
                            params.get("threadId") == thread_id and params.get("turnId") == turn_id):
                        usage = (params.get("tokenUsage") or {}).get("last")
                    if method == "item/completed" and (
                            params.get("threadId") == thread_id and params.get("turnId") == turn_id):
                        completed_items.append(params.get("item") or {})
                    if method == "turn/completed" and (
                            params.get("threadId") == thread_id and
                            (params.get("turn") or {}).get("id") == turn_id):
                        turn = params["turn"]
                        if turn.get("status") != "completed":
                            raise BackendError("outcome_unknown", str(turn.get("error") or turn.get("status")), usage)
                        try:
                            content = json.loads(self._final_message(turn, completed_items))
                        except json.JSONDecodeError as exc:
                            raise BackendError("invalid_output", "response was not JSON", usage) from exc
                        if not isinstance(content, dict):
                            raise BackendError("invalid_output", "response must be an object", usage)
                        try:
                            billed = self._call(proc, 4, "account/usage/read", {"threadId": thread_id}, 20)
                            evidence = self._verified_usage_group(billed, thread_id, self.model_id, self.effort)
                        except BackendError as exc:
                            preserved = Result(content, self.model_id, usage, None)
                            raise BackendError("model_policy", f"identity evidence unavailable: {exc}",
                                               usage, preserved) from exc
                        return Result(content, evidence["groups"][0]["model"], usage, evidence)
                raise BackendError("outcome_unknown", "turn exceeded timeout after submission", usage)
            except BackendError as exc:
                if turn_submitted and exc.kind in ("timeout", "rate_limited", "backend_failure"):
                    raise BackendError("outcome_unknown", f"turn outcome unknown: {exc}", usage) from exc
                raise
        finally:
            self._close(proc)

    def close(self) -> None:
        pass
