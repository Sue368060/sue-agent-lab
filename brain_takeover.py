"""Atomic Main Agent ownership transfer for Sue Agent Lab."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .visible_ledger import doctor_ledger
except ImportError:  # Direct script execution remains supported.
    from visible_ledger import doctor_ledger


DEFAULT_STATE = (
    Path(__file__).resolve().parents[1]
    / ".ai/projects/SUE-AGENT-LAB-2026/BRAIN_STATE.json"
)
DEFAULT_RUNS = Path(__file__).with_name("runs")
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{5,127}$")


class TakeoverError(RuntimeError):
    """Raised when ownership cannot be transferred safely."""


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TakeoverError(f"cannot read brain state: {exc}") from exc
    if not isinstance(value, dict):
        raise TakeoverError("brain state must be a JSON object")
    if value.get("schema_version") != 1:
        raise TakeoverError("unsupported brain state schema")
    if value.get("project_id") != "SUE-AGENT-LAB-2026":
        raise TakeoverError("brain state belongs to a different project")
    if not isinstance(value.get("active_brain_thread_id"), str):
        raise TakeoverError("brain state has no active owner")
    if value.get("exclusive_main_owner") is not True:
        raise TakeoverError("brain state does not require an exclusive owner")
    runtime = value.get("current_runtime_status")
    if not isinstance(runtime, dict) or "active_visible_run_id" not in runtime:
        raise TakeoverError("brain state has no visible-run status")
    return value


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _verify_no_unfinished_ledgers(runs_dir: Path) -> None:
    """Fail closed if takeover cannot prove every visible run is terminal."""
    if not runs_dir.exists():
        return
    if not runs_dir.is_dir():
        raise TakeoverError(f"visible runs path is not a directory: {runs_dir}")
    for ledger_path in sorted(runs_dir.glob("*.jsonl")):
        diagnosis = doctor_ledger(ledger_path)
        if diagnosis.get("healthy") is not True:
            raise TakeoverError(
                f"cannot verify visible ledger {ledger_path.name}: "
                f"{diagnosis.get('status', 'UNKNOWN')}"
            )
        if diagnosis.get("run_status") == "RUNNING":
            raise TakeoverError(
                f"visible run {diagnosis.get('run_id', ledger_path.stem)} is active "
                f"in ledger {ledger_path.name}; finish or cancel it first"
            )


def claim_brain(
    state_path: Path,
    new_thread_id: str,
    *,
    expected_owner: str | None = None,
    label: str | None = None,
    now: datetime | None = None,
    runs_dir: Path | None = None,
) -> dict[str, Any]:
    """Claim exclusive Main Agent ownership with compare-and-swap semantics."""
    if not THREAD_ID_RE.fullmatch(new_thread_id):
        raise TakeoverError("invalid new thread id")

    state_path = state_path.resolve()
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = _load_state(state_path)
        old_owner = state["active_brain_thread_id"]

        if old_owner != new_thread_id and expected_owner is None:
            raise TakeoverError("expected owner is required for ownership transfer")
        if expected_owner is not None and old_owner != expected_owner:
            raise TakeoverError(
                f"owner changed: expected {expected_owner}, found {old_owner}"
            )
        if old_owner != new_thread_id:
            resolved_runs = (
                Path(runs_dir).resolve() if runs_dir is not None else
                (DEFAULT_RUNS.resolve() if state_path == DEFAULT_STATE.resolve()
                 else state_path.parent / "runs")
            )
            _verify_no_unfinished_ledgers(resolved_runs)
        active_run = state.get("current_runtime_status", {}).get(
            "active_visible_run_id"
        )
        if active_run is not None and old_owner != new_thread_id:
            raise TakeoverError(
                f"visible run {active_run} is active; finish or hand it off first"
            )

        timestamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
        if old_owner != new_thread_id:
            history = state.setdefault("ownership_history", [])
            if not isinstance(history, list):
                raise TakeoverError("ownership_history must be a list")
            history.append(
                {
                    "from_thread_id": old_owner,
                    "to_thread_id": new_thread_id,
                    "claimed_at": timestamp,
                    "basis": "Sue requested continue/takeover after memory read",
                }
            )
            state["supersedes_thread_id"] = old_owner
            state["active_brain_thread_id"] = new_thread_id

        state["active_brain_label"] = label or "Sue Agent Lab current Main Agent"
        state["exclusive_main_owner"] = True
        state["updated_at"] = timestamp
        state["current_phase"] = "takeover-ready; active Main Agent claimed"
        _atomic_write(state_path, state)
        return state


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Atomically claim Sue Agent Lab Main Agent ownership."
    )
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--expected-owner", required=True)
    parser.add_argument("--label")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS)
    args = parser.parse_args()
    try:
        state = claim_brain(
            args.state,
            args.thread_id,
            expected_owner=args.expected_owner,
            label=args.label,
            runs_dir=args.runs_dir,
        )
    except TakeoverError as exc:
        parser.exit(2, f"takeover refused: {exc}\n")
    print(
        json.dumps(
            {
                "status": "CLAIMED",
                "active_brain_thread_id": state["active_brain_thread_id"],
                "supersedes_thread_id": state.get("supersedes_thread_id"),
                "updated_at": state["updated_at"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
