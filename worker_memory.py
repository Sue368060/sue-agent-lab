"""Persistent memory and atomic visible-window replacement for Workers A-D."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .visible_ledger import doctor_ledger
from .visible_modes import DEFAULT_CONFIG, ModeConfigError, resolve_mode


DEFAULT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MEMORY_ROOT = (
    DEFAULT_ROOT / ".ai/projects/SUE-AGENT-LAB-2026/workers"
)
DEFAULT_RUNS = Path(__file__).with_name("runs")
DEFAULT_EXTRACTOR = Path(__file__).with_name("extract_local_codex_chat.zsh")
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{5,127}$")
WORKER_ID_RE = re.compile(r"^[A-Z][A-Z0-9_-]{0,15}$")


class WorkerMemoryError(RuntimeError):
    """Raised when Worker memory cannot be refreshed or transferred safely."""


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_write_text(
        path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerMemoryError(f"cannot read Worker roster: {exc}") from exc
    if not isinstance(config, dict) or config.get("schema_version") != 2:
        raise WorkerMemoryError("Worker takeover requires visible-mode schema 2")
    workers = config.get("workers")
    if not isinstance(workers, dict) or not workers:
        raise WorkerMemoryError("Worker roster is missing")
    try:
        for mode in config.get("modes", {}):
            resolve_mode(config, mode)
    except ModeConfigError as exc:
        raise WorkerMemoryError(str(exc)) from exc
    return config


def _verify_terminal_ledgers(runs_dir: Path) -> None:
    if not runs_dir.exists():
        return
    if not runs_dir.is_dir():
        raise WorkerMemoryError(f"visible runs path is not a directory: {runs_dir}")
    for ledger_path in sorted(runs_dir.glob("*.jsonl")):
        diagnosis = doctor_ledger(ledger_path)
        if diagnosis.get("healthy") is not True:
            raise WorkerMemoryError(
                f"cannot verify visible ledger {ledger_path.name}: "
                f"{diagnosis.get('status', 'UNKNOWN')}")
        if diagnosis.get("run_status") == "RUNNING":
            raise WorkerMemoryError(
                f"visible run {diagnosis.get('run_id', ledger_path.stem)} is active; "
                "finish or cancel it before replacing a Worker window")


def _memory_paths(memory_root: Path, worker_id: str) -> dict[str, Path]:
    root = Path(memory_root) / worker_id
    return {
        "root": root,
        "state": root / "WORKER_STATE.json",
        "memory": root / "WORKER_MEMORY.md",
        "bootstrap": root / "BOOTSTRAP.md",
        "chats": root / "chats",
    }


def _shared_read_paths(root: Path) -> list[str]:
    return [
        str(root / "AGENTS.md"),
        str(root / ".ai/CURRENT_TASK.md"),
        str(root / ".ai/projects/SUE-AGENT-LAB-2026/PROJECT.md"),
        str(root / ".ai/projects/SUE-AGENT-LAB-2026/BRAIN_HANDOFF.md"),
        str(root / ".ai/projects/SUE-AGENT-LAB-2026/BRAIN_STATE.json"),
        str(root / "orchestrator/visible_modes.json"),
    ]


def _worker_snapshot(config: dict[str, Any], worker_id: str,
                     memory_root: Path, shared_root: Path) -> dict[str, Any]:
    worker = config["workers"].get(worker_id)
    if not isinstance(worker, dict):
        raise WorkerMemoryError(f"unknown Worker: {worker_id}")
    archive = worker["chat_archive_path"]
    archive_path = Path(archive)
    if not archive_path.is_absolute():
        archive_path = shared_root / archive_path
    return {
        "schema_version": 1,
        "project_id": "SUE-AGENT-LAB-2026",
        "worker_id": worker_id,
        "active_thread_id": worker["thread_id"],
        "generation": worker["generation"],
        "exclusive_owner": True,
        "model": worker["model"],
        "thinking": worker["thinking"],
        "role": worker.get("role"),
        "shared_memory_access": "READ_ONLY",
        "shared_memory_read_paths": _shared_read_paths(shared_root),
        "active_chat_archive": str(archive_path),
        "previous_thread_ids": list(worker["previous_thread_ids"]),
        "takeover_history": list(worker["takeover_history"]),
        "memory_directory": str(Path(memory_root) / worker_id),
        "authority": "orchestrator/visible_modes.json",
    }


def archive_worker_chat(config_path: Path, worker_id: str, *,
                        extractor_path: Path = DEFAULT_EXTRACTOR) -> dict[str, str]:
    """Refresh one active Worker's visible-text archive without model calls."""
    config_path = Path(config_path).resolve()
    config = _load_config(config_path)
    worker = config["workers"].get(worker_id)
    if not isinstance(worker, dict):
        raise WorkerMemoryError(f"unknown Worker: {worker_id}")
    shared_root = config_path.parent.parent
    archive = Path(worker["chat_archive_path"])
    if not archive.is_absolute():
        archive = shared_root / archive
    extractor = Path(extractor_path).resolve()
    if not extractor.is_file():
        raise WorkerMemoryError(f"chat extractor is unavailable: {extractor}")
    archive.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["/bin/zsh", str(extractor), worker["thread_id"], str(archive)],
            check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise WorkerMemoryError(f"cannot archive Worker {worker_id}: {detail}") from exc
    if not archive.is_file():
        raise WorkerMemoryError(f"Worker {worker_id} archive was not created")
    status = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if not status.startswith("extract_status="):
        raise WorkerMemoryError(f"Worker {worker_id} archive status is invalid")
    return {
        "worker_id": worker_id,
        "thread_id": worker["thread_id"],
        "archive_path": str(archive),
        "extract_status": status,
    }


def checkpoint_worker_memory(config_path: Path = DEFAULT_CONFIG,
                             memory_root: Path = DEFAULT_MEMORY_ROOT,
                             worker_ids: list[str] | None = None, *,
                             extractor_path: Path = DEFAULT_EXTRACTOR
                             ) -> list[dict[str, Any]]:
    """Archive active chats and rebuild their derived memory in one step."""
    config_path = Path(config_path).resolve()
    config = _load_config(config_path)
    selected = worker_ids or sorted(config["workers"])
    archived = [archive_worker_chat(
        config_path, worker_id, extractor_path=extractor_path)
        for worker_id in selected]
    snapshots = refresh_worker_memory(config_path, memory_root, selected)
    by_worker = {item["worker_id"]: item for item in snapshots}
    return [{"archive": item, "memory": by_worker[item["worker_id"]]}
            for item in archived]


def _render_memory(snapshot: dict[str, Any]) -> str:
    role = snapshot["role"] or "尚未固定，可由 Sue 后续指定"
    history = snapshot["takeover_history"]
    history_lines = [
        f"- 第 {item['generation']} 代：`{item['from_thread_id']}` → "
        f"`{item['to_thread_id']}`；旧聊天：`{item['archived_chat_path']}`"
        for item in history
    ] or ["- 暂无窗口接替记录。"]
    shared = "\n".join(f"- `{path}`" for path in snapshot["shared_memory_read_paths"])
    return f"""# Worker {snapshot['worker_id']} 长期记忆

这是 Sue Agent Lab Worker {snapshot['worker_id']} 的长期身份入口。聊天窗口只是载体；Worker 身份、角色、模型偏好和历史记录保存在本目录及 `orchestrator/visible_modes.json` 中。

## 当前身份

- 当前窗口：`{snapshot['active_thread_id']}`
- 代次：{snapshot['generation']}
- 模型配置：`{snapshot['model']}` / `{snapshot['thinking']}`
- 角色：{role}
- 当前聊天归档：`{snapshot['active_chat_archive']}`

## 工作规则

- 读取本文件、`WORKER_STATE.json`、当前聊天归档，以及 Main Agent 指定的最小 Shared-Memory 文件。
- Shared-Memory 对 Worker 是只读的；可以在回答中提出记忆修改建议，但只有 Main Agent 可以写权威项目记忆、派单和最终答案。
- 不依赖旧窗口的隐含上下文。每个任务以 Main Agent 的自包含任务包为准。
- 不直接读取其他 Worker 的完整历史。需要互审时，只读取 Main Agent 转来的候选结果或短 Context Pack。
- 当前线程编号不匹配 `WORKER_STATE.json` 时，停止自称现任 Worker，并提醒 Main Agent检查接替状态。

## 可读取的共享记忆入口

{shared}

## 窗口接替历史

{chr(10).join(history_lines)}
"""


def _render_bootstrap(snapshot: dict[str, Any]) -> str:
    return f"""# Worker {snapshot['worker_id']} 新窗口接入提示

你是 Sue Agent Lab 的 Worker {snapshot['worker_id']}，当前代次为 {snapshot['generation']}。先读取：

1. `{snapshot['memory_directory']}/WORKER_MEMORY.md`
2. `{snapshot['memory_directory']}/WORKER_STATE.json`
3. `{snapshot['active_chat_archive']}`（若已生成）
4. Main Agent 本次提供的自包含任务包

你可以只读访问 Shared-Memory，但不得写权威项目记忆、不得自行派单、不得替 Main Agent 生成最终交付。先核对当前聊天线程与 `active_thread_id` 一致；不一致时停止并报告。完成读取后等待 Main Agent 的具体任务。
"""


def refresh_worker_memory(config_path: Path = DEFAULT_CONFIG,
                          memory_root: Path = DEFAULT_MEMORY_ROOT,
                          worker_ids: list[str] | None = None) -> list[dict[str, Any]]:
    config_path = Path(config_path).resolve()
    config = _load_config(config_path)
    shared_root = config_path.parent.parent
    selected = worker_ids or sorted(config["workers"])
    snapshots = []
    for worker_id in selected:
        if not WORKER_ID_RE.fullmatch(worker_id):
            raise WorkerMemoryError(f"invalid Worker ID: {worker_id}")
        snapshot = _worker_snapshot(
            config, worker_id, Path(memory_root).resolve(), shared_root)
        paths = _memory_paths(Path(memory_root).resolve(), worker_id)
        paths["chats"].mkdir(parents=True, exist_ok=True)
        _atomic_write_json(paths["state"], snapshot)
        _atomic_write_text(paths["memory"], _render_memory(snapshot))
        _atomic_write_text(paths["bootstrap"], _render_bootstrap(snapshot))
        snapshots.append(snapshot)
    return snapshots


def claim_worker(config_path: Path, worker_id: str, new_thread_id: str, *,
                 expected_thread_id: str | None,
                 memory_root: Path = DEFAULT_MEMORY_ROOT,
                 runs_dir: Path = DEFAULT_RUNS,
                 now: datetime | None = None,
                 require_archive: bool = True) -> dict[str, Any]:
    if not WORKER_ID_RE.fullmatch(worker_id):
        raise WorkerMemoryError("invalid Worker ID")
    if not THREAD_ID_RE.fullmatch(new_thread_id):
        raise WorkerMemoryError("invalid new thread ID")
    config_path = Path(config_path).resolve()
    shared_root = config_path.parent.parent
    lock_path = config_path.with_suffix(config_path.suffix + ".worker.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        config = _load_config(config_path)
        worker = config["workers"].get(worker_id)
        if not isinstance(worker, dict):
            raise WorkerMemoryError(f"unknown Worker: {worker_id}")
        old_thread_id = worker["thread_id"]
        if old_thread_id != new_thread_id and expected_thread_id is None:
            raise WorkerMemoryError("expected thread ID is required for takeover")
        if expected_thread_id is not None and old_thread_id != expected_thread_id:
            raise WorkerMemoryError(
                f"Worker owner changed: expected {expected_thread_id}, "
                f"found {old_thread_id}")
        for other_id, other in config["workers"].items():
            if other_id != worker_id and other.get("thread_id") == new_thread_id:
                raise WorkerMemoryError(
                    f"thread already belongs to Worker {other_id}")
        if old_thread_id != new_thread_id:
            _verify_terminal_ledgers(Path(runs_dir).resolve())
            old_archive = Path(worker["chat_archive_path"])
            if not old_archive.is_absolute():
                old_archive = shared_root / old_archive
            if require_archive and not old_archive.is_file():
                raise WorkerMemoryError(
                    "current Worker chat must be archived before takeover")
            generation = worker["generation"] + 1
            new_archive = (
                Path(memory_root).resolve() / worker_id / "chats" /
                f"codex-{new_thread_id}.md"
            )
            try:
                new_archive_value = str(new_archive.relative_to(shared_root))
            except ValueError:
                new_archive_value = str(new_archive)
            timestamp = (now or datetime.now().astimezone()).isoformat(
                timespec="seconds")
            worker["takeover_history"].append({
                "generation": generation,
                "from_thread_id": old_thread_id,
                "to_thread_id": new_thread_id,
                "archived_chat_path": str(old_archive),
                "claimed_at": timestamp,
                "basis": "Sue requested Worker window replacement",
            })
            if old_thread_id not in worker["previous_thread_ids"]:
                worker["previous_thread_ids"].append(old_thread_id)
            worker["thread_id"] = new_thread_id
            worker["generation"] = generation
            worker["chat_archive_path"] = new_archive_value
            _atomic_write_json(config_path, config)
    try:
        snapshot = refresh_worker_memory(
            config_path, memory_root, [worker_id])[0]
    except (OSError, ValueError, WorkerMemoryError) as exc:
        raise WorkerMemoryError(
            "Worker takeover committed, but derived memory refresh failed; "
            "run the refresh command before dispatch") from exc
    return snapshot


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="sue-worker-memory",
        description="Refresh Worker memory or atomically replace one visible window.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--memory-root", type=Path, default=DEFAULT_MEMORY_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    refresh = sub.add_parser("refresh")
    refresh.add_argument("--worker", action="append", default=[])
    archive = sub.add_parser("archive")
    archive.add_argument("--worker", action="append", default=[])
    archive.add_argument("--extractor", type=Path, default=DEFAULT_EXTRACTOR)
    checkpoint = sub.add_parser("checkpoint")
    checkpoint.add_argument("--worker", action="append", default=[])
    checkpoint.add_argument("--extractor", type=Path, default=DEFAULT_EXTRACTOR)
    takeover = sub.add_parser("takeover")
    takeover.add_argument("--worker", required=True)
    takeover.add_argument("--thread-id", required=True)
    takeover.add_argument("--expected-thread-id", required=True)
    takeover.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS)
    takeover.add_argument("--extractor", type=Path, default=DEFAULT_EXTRACTOR)
    args = parser.parse_args(argv)
    try:
        if args.command == "refresh":
            result = refresh_worker_memory(
                args.config, args.memory_root, args.worker or None)
            payload = {"status": "REFRESHED", "workers": result}
        elif args.command == "archive":
            config = _load_config(Path(args.config).resolve())
            selected = args.worker or sorted(config["workers"])
            result = [archive_worker_chat(
                args.config, worker_id, extractor_path=args.extractor)
                for worker_id in selected]
            payload = {"status": "ARCHIVED", "workers": result}
        elif args.command == "checkpoint":
            result = checkpoint_worker_memory(
                args.config, args.memory_root, args.worker or None,
                extractor_path=args.extractor)
            payload = {"status": "CHECKPOINTED", "workers": result}
        else:
            archive_worker_chat(
                args.config, args.worker, extractor_path=args.extractor)
            result = claim_worker(
                args.config, args.worker, args.thread_id,
                expected_thread_id=args.expected_thread_id,
                memory_root=args.memory_root, runs_dir=args.runs_dir)
            payload = {"status": "CLAIMED", "worker": result}
    except (OSError, ValueError, WorkerMemoryError) as exc:
        parser.exit(2, f"worker memory refused: {exc}\n")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
