"""Read one exact completed visible Codex turn without generating a model turn."""

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import uuid

from .v101.backend import AppServerBackend


class VisibleReadError(RuntimeError):
    pass


def _uuid(value: str, label: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise VisibleReadError(f"invalid {label}") from exc
    if str(parsed) != value:
        raise VisibleReadError(f"invalid {label}")


def _extract(turn: dict, thread_id: str, turn_id: str) -> dict:
    if turn.get("id") != turn_id:
        raise VisibleReadError("turn ID mismatch")
    if turn.get("status") != "completed":
        raise VisibleReadError("target turn is not completed")
    items = turn.get("items")
    if not isinstance(items, list):
        raise VisibleReadError("turn items are unavailable")
    if any(not isinstance(item, dict) for item in items):
        raise VisibleReadError("invalid turn item")
    final = [item for item in items
             if item.get("type") == "agentMessage" and item.get("phase") == "final_answer"]
    if len(final) != 1:
        raise VisibleReadError("expected exactly one final answer item")
    item = final[0]
    message_id = item.get("id")
    value = item.get("text")
    if not isinstance(message_id, str) or not isinstance(value, str) or not value.strip():
        raise VisibleReadError("final answer is empty or lacks an item ID")
    return {"thread_id": thread_id, "turn_id": turn_id, "message_id": message_id,
            "status": "completed", "text": value,
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            "source": "app-server.thread/turns/list.itemsView=full"}


def fetch_turn(call, thread_id: str, turn_id: str, max_pages: int = 8) -> dict:
    """Page stored turns and accept only the requested turn's final answer."""
    _uuid(thread_id, "thread ID")
    _uuid(turn_id, "turn ID")
    cursor = None
    seen = set()
    for _ in range(max_pages):
        params = {"threadId": thread_id, "limit": 50, "sortDirection": "desc",
                  "itemsView": "full"}
        if cursor is not None:
            params["cursor"] = cursor
        data = call("thread/turns/list", params)
        if not isinstance(data, dict):
            raise VisibleReadError("invalid App Server turns response")
        turns = data.get("data")
        if not isinstance(turns, list):
            raise VisibleReadError("invalid App Server turns response")
        if any(not isinstance(turn, dict) for turn in turns):
            raise VisibleReadError("invalid App Server turn")
        matching = [turn for turn in turns if turn.get("id") == turn_id]
        if len(matching) > 1:
            raise VisibleReadError("duplicate target turn")
        if matching:
            return _extract(matching[0], thread_id, turn_id)
        cursor = data.get("nextCursor")
        if not cursor:
            break
        if cursor in seen:
            raise VisibleReadError("repeating turns cursor")
        seen.add(cursor)
    raise VisibleReadError("target turn was not found within bounded history")


def read_visible_turn(thread_id: str, turn_id: str, executable: str = "codex") -> dict:
    """Only initialize and read history; never call thread/start or turn/start."""
    _uuid(thread_id, "thread ID")
    _uuid(turn_id, "turn ID")
    client = AppServerBackend("read-only-client", "none", executable=executable)
    cwd = Path("/private/tmp") if Path("/private/tmp").is_dir() else Path(tempfile.gettempdir())
    proc = client._start(cwd, False)
    request_id = 10
    try:
        client._init(proc)

        def call(method: str, params: dict) -> dict:
            nonlocal request_id
            request_id += 1
            return client._call(proc, request_id, method, params, 20)

        return fetch_turn(call, thread_id, turn_id)
    finally:
        client._close(proc)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("thread_id")
    parser.add_argument("turn_id")
    args = parser.parse_args(argv)
    try:
        result = read_visible_turn(args.thread_id, args.turn_id)
    except (VisibleReadError, RuntimeError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "READ_FAILED", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
