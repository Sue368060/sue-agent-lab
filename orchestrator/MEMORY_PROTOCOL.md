# Memory protocol

Canonical memory remains the existing Shared-Memory/.ai tree. The coordinator records hashes of the seven core files at submission and at each run/resume start. A changed-file list and bounded current-text excerpt are logged/included in pending phase context; completed steps remain immutable. This is version awareness, not a continuous file watcher or full semantic diff.

Workers can read memory but cannot write it. Their outputs and proposed claims go into local artifacts and SQLite proposals. After a successful real run, only process metadata (goal, artifact path, needs-review label) is appended idempotently to .ai/projects/SUE-AGENT-LAB-2026/RUN_LOG.md. Fake runs simulate this step and never touch canonical memory. The orchestrator does not promote research facts without source review.

Each visible Worker has a persistent directory at `.ai/projects/SUE-AGENT-LAB-2026/workers/WORKER_ID/`. `WORKER_STATE.json` and `WORKER_MEMORY.md` are short derived views; `BOOTSTRAP.md` initializes a replacement window; `chats/` stores deterministic sanitized visible-message extracts. `orchestrator/visible_modes.json` schema 2 is the authority for the active thread, generation and takeover history.

After every terminal visible run and before Main handoff, run `python3 -m orchestrator.worker_memory checkpoint`. It archives all active visible chats and rebuilds the derived Worker memory without model calls. This is a deterministic workflow checkpoint; the Codex UI does not expose a message-by-message event hook.

Before replacing a Worker window, refresh the old chat archive. The atomic takeover compares the expected old thread ID, scans every visible ledger, rejects a running or unverifiable ledger and prevents one thread from owning two Worker identities. It then increments the generation and updates only the authoritative roster under a file lock. Derived Worker memory can be regenerated after interruption. Old windows remain historical and are no longer dispatch targets.

Do not change CURRENT_TASK.md or global AGENT_ROUTING.md for a lab run. The Lab project records are indexed in .ai/projects/INDEX.md. iCloud and USB sync occur only on Sue's explicit backup request.
