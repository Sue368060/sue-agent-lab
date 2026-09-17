"""Deterministic test backend and a guarded Codex app-server transport."""

import json
import queue
import re
import subprocess
import tempfile
import threading
import time


class BackendError(RuntimeError):
    pass


class FakeBackend:
    """No model calls; predictable, distinct agent output for contract tests."""

    def __init__(self, fail_once=None):
        self.fail_once = set(fail_once or [])
        self.calls = []

    def models(self):
        return [
            {"model": "fake-cheap", "hidden": False, "isDefault": False},
            {"model": "fake-strong", "hidden": False, "isDefault": True},
        ]

    def generate(self, phase, actor, prompt, model, timeout=120):
        key = (phase, actor)
        self.calls.append(key)
        if key in self.fail_once:
            self.fail_once.remove(key)
            raise BackendError("injected transient worker failure")
        if phase == "plan":
            content = {"plan": "Define the question, compare independent answers, check evidence.", "criteria": ["addresses goal", "states uncertainty"]}
        elif phase == "draft":
            content = {"answer": "%s independent proposal: %s" % (actor, prompt.split("GOAL:", 1)[-1][:160]), "evidence": [], "uncertainties": ["No external sources supplied"]}
        elif phase == "review":
            content = {"verdict": "revise", "strengths": ["addresses goal"], "issues": ["state source limitations"], "critical": "critical" in actor}
        elif phase == "revision":
            content = {"answer": "%s revised answer with explicit source limitations" % actor, "evidence": [], "uncertainties": ["Requires external verification"]}
        elif phase == "synthesis":
            content = {"answer": "Synthesized result: " + prompt.split("GOAL:", 1)[-1][:180], "limitations": ["No external evidence verified"], "claims": []}
        elif phase == "final_critic":
            content = {"pass": True, "issues": [], "note": "Do not present unsupported claims as verified."}
        else:
            raise BackendError("unknown phase " + phase)
        return {"content": content, "usage": {"totalTokens": 0}, "model": model}

    def close(self):
        pass


def parse_model_json(raw):
    """Accept a plain JSON object or a single fenced JSON object, never eval."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^\`\`\`(?:json)?\s*|\s*\`\`\`$", "", raw, flags=re.I)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BackendError("model did not return valid JSON: %s" % exc) from exc
    if not isinstance(parsed, dict):
        raise BackendError("model response must be a JSON object")
    return parsed


class AppServerBackend:
    """One stdio app-server process; JSON-RPC requests and notifications are demultiplexed."""

    def __init__(self, cwd, codex="codex", request_timeout=30, thread_cwd=None):
        self.cwd = str(cwd)
        # A neutral turn cwd avoids re-injecting the entire Shared-Memory AGENTS tree
        # into every low-cost worker. Memory is explicitly versioned by the coordinator.
        self.thread_cwd = str(thread_cwd or tempfile.gettempdir())
        self.request_timeout = request_timeout
        self.proc = subprocess.Popen(
            [codex, "app-server"],
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self.lock = threading.Lock()
        self.pending = {}
        self.turns = {}
        self.usage = {}
        self.model_efforts = {}
        self.next_id = 1
        self.dead = False
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.request("initialize", {"clientInfo": {"name": "sue-agent-lab", "version": "0.1.0"}})
        self._send({"method": "initialized", "params": {}})

    def _send(self, msg):
        with self.lock:
            if self.proc.poll() is not None:
                raise BackendError("app-server exited")
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()

    def _read(self):
        try:
            for line in self.proc.stdout:
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "id" in msg and ("result" in msg or "error" in msg):
                    waiter = self.pending.get(msg["id"])
                    if waiter:
                        waiter.put(msg)
                elif msg.get("method") == "turn/completed":
                    params = msg.get("params") or {}
                    waiter = self.turns.get(params.get("threadId"))
                    if waiter:
                        waiter.put(msg)
                elif msg.get("method") == "thread/tokenUsage/updated":
                    params = msg.get("params") or {}
                    self.usage[params.get("threadId")] = (params.get("tokenUsage") or {}).get("last", {})
                elif "id" in msg and "method" in msg:
                    # A worker is read-only. Never silently approve a tool or permission request.
                    try:
                        self._send({"id": msg["id"], "error": {"code": -32601, "message": "Sue Agent Lab does not grant interactive approvals"}})
                    except Exception:
                        pass
        finally:
            self.dead = True
            for waiter in list(self.pending.values()) + list(self.turns.values()):
                waiter.put({"error": {"message": "app-server connection closed"}})

    def request(self, method, params=None, timeout=None):
        with self.lock:
            req_id = self.next_id
            self.next_id += 1
            waiter = queue.Queue(maxsize=1)
            self.pending[req_id] = waiter
        try:
            self._send({"id": req_id, "method": method, "params": params or {}})
            try:
                response = waiter.get(timeout=timeout or self.request_timeout)
            except queue.Empty as exc:
                raise BackendError("timeout on " + method) from exc
            if "error" in response:
                raise BackendError("%s: %s" % (method, response["error"]))
            return response["result"]
        finally:
            with self.lock:
                self.pending.pop(req_id, None)

    def models(self):
        data = []
        cursor = None
        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            response = self.request("model/list", params)
            data.extend(response.get("data", []))
            cursor = response.get("nextCursor")
            if not cursor:
                self.model_efforts = {
                    m.get("model"): [e.get("reasoningEffort") for e in m.get("supportedReasoningEfforts", [])
                                     if isinstance(e, dict)]
                    for m in data
                }
                return data

    def generate(self, phase, actor, prompt, model, timeout=120):
        preferred_effort = "high" if actor == "leader" else "low"
        effort = preferred_effort if preferred_effort in self.model_efforts.get(model, []) else None
        response = self.request(
            "thread/start",
            {"cwd": self.thread_cwd, "model": model, "sandbox": "read-only", "approvalPolicy": "never", "ephemeral": True},
        )
        thread_id = response["thread"]["id"]
        waiter = queue.Queue(maxsize=1)
        self.turns[thread_id] = waiter
        try:
            turn_params = {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt}],
                    "clientUserMessageId": "%s-%s-%d" % (phase, actor, time.time_ns()),
                }
            if effort:
                turn_params["effort"] = effort
            self.request("turn/start", turn_params)
            try:
                notification = waiter.get(timeout=timeout)
            except queue.Empty as exc:
                raise BackendError("turn timed out: %s/%s" % (phase, actor)) from exc
            if "error" in notification:
                raise BackendError(str(notification["error"]))
            turn = notification["params"]["turn"]
            if turn.get("status") != "completed":
                raise BackendError("turn status %s: %s" % (turn.get("status"), turn.get("error")))
            messages = [item.get("text", "") for item in turn.get("items", []) if item.get("type") == "agentMessage"]
            if not messages:
                raise BackendError("turn completed without an agent message")
            return {"content": parse_model_json(messages[-1]), "usage": self.usage.pop(thread_id, {}),
                    "model": model, "effort": effort, "thread_id": thread_id}
        finally:
            self.turns.pop(thread_id, None)

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
