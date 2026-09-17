# Message protocol

Job and step identity: job ID, stage, actor, target. SQLite primary keys make a completed step idempotent. A call_intent event is committed before each model invocation; step_completed follows successful artifact persistence. On a crash between these events, the step can be retried, which may duplicate a remote model turn but not a completed local artifact.

The transport sends initialize/initialized, model/list, thread/start, turn/start, then waits for turn/completed. Requests are correlated by JSON-RPC ID, notifications by thread ID. A successful answer must be a JSON object; malformed output fails the step and consumes a bounded retry. Permission/approval requests are denied.

SQLite is canonical. The exported JOB.events.jsonl is for inspection, not replay authority. Artifacts are UTF-8 JSON and the final manifest lists expected SHA-256 hashes.
