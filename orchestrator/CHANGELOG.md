# Changelog

## v2.7.2 — 2026-09-17

- Fixed deployed-package verification: replacing anonymous Worker IDs no longer causes a false tamper failure. Only the manifest-declared `orchestrator/visible_modes.json` may change in deployed mode, and its full schema, modes, unique IDs and archive bindings are revalidated.
- Hardened manifest parsing against path traversal, parent symlinks, duplicate paths and malformed size/hash metadata.
- Hardened non-Codex identity lookup so invalid providers, malformed top-level JSON and forged identity entries fail closed with a controlled error.
- Removed a test-only dependency on the package extraction directory, so a fresh copy can run its suite from a temporary validation folder without weakening the real-Worker temporary-directory prohibition.
- Corrected the deployment guide's Main takeover wording: compare-and-swap transfers workflow authority but does not provide operating-system enforcement against an obsolete chat window.
- Full standard discovery now passes 155 tests.

## v2.7.1 — 2026-09-17

- Added a real loopback TCP test in which the server reads a submitted request and then closes the connection without replying. The run records `outcome_unknown`, aborts and makes exactly one backend call.
- This verifies OS network-disconnect behavior without changing the user's Wi-Fi, VPN, proxy or DNS. Full standard discovery now passes 151 tests.

## v2.7.0 — 2026-09-17

- Added an offline portable Doctor that verifies every manifested file, rejects symlinks, hash/size drift and owner-specific absolute paths, and distinguishes anonymous package placeholders from a deployment-ready Worker roster.
- Added a locked, atomic local identity registry for Claude, WorkBuddy or other applications that do not expose a stable native conversation ID. Raw locators are hashed rather than stored.
- Added a real subprocess/pipe disconnect test after turn submission; the backend returns `OUTCOME_UNKNOWN` and never converts the transport failure into an automatic retry.
- Closed subprocess streams deterministically after App Server shutdown. Full standard discovery now passes 150 tests.

## v2.6.0 — 2026-09-17

- Replaced the real A and B visible tasks with generation-2 windows and atomically transferred their persistent Worker identities; old IDs remain only in takeover history and old tasks are no longer dispatch targets.
- Added one-step terminal `worker_memory checkpoint`, plus a portable extractor that resolves Codex data from `CODEX_HOME` or the current user's home directory.
- Added frozen per-mode capacity floors and hash-bound runtime availability reports. Busy Workers use two no-model waits (5/15 seconds); exhausted capacity degrades to mandatory PARTIAL only above the floor and otherwise stops.
- Added an in-call strict-backend lease watchdog and a real subprocess-kill recovery test proving unknown calls are not replayed.
- Refreshed the portable deployment package to schema 2 and the current 143-test baseline.

## v2.5.0 — 2026-09-17

- Added persistent, separate A–D Worker memory directories with sanitized visible-chat archives, current state and replacement-window bootstrap prompts.
- Upgraded `visible_modes.json` to schema 2 with exclusive active-thread ownership, generation counters, prior-thread lists and takeover history.
- Added `worker_memory.py` for no-model archive/refresh and atomic Worker-window takeover. Takeover requires a fresh old-chat archive, expected-owner comparison and terminal healthy ledgers.
- Added deterministic tests for memory generation, archive routing, stale/duplicate ownership, missing archive, active/corrupt ledger refusal, terminal takeover and idempotence.

# 2026-09-16 — v2.4.3 review hardening and package entry

- Reproduced Claude's realistic-section-reuse and zero-synthesis bypasses, then added pairwise section similarity and original-synthesis floors. New contracts emit delivery receipt v3; older frozen contracts and receipt v2 ledgers remain readable.
- Main-Agent takeover now scans every visible ledger and refuses transfer when any ledger is running or cannot be verified, even when `BRAIN_STATE.json` says no run is active.
- Added package entry files and relative-import compatibility. `python3 -m orchestrator`, `python3 -m orchestrator.cli`, direct script use, and one standard test-discovery command now work.
- Marked the 2026-09-14 takeover note as historical and refreshed current documentation. Full standard discovery: 127 tests pass. No model calls were used.

## 2026-09-16 — v2.4.2 low-cost preflight after real A/B micro-run

- Ran one explicitly authorized A/B quick task with two concurrent Luna Medium drafts and no further model calls; the run reached `DONE_VERIFIED` and Doctor `HEALTHY`.
- Added `visible_cost_guard.py`, which reads only local token-usage metadata and stops explicit low-cost dispatch when a persistent Worker context exceeds 50,000 recent input tokens.
- When usage metadata is unavailable, the guard falls back to the mode's `max_llm_calls`; a hash-bound report can convert Programmatic Main dispatch into `STOP_FOR_WORKER_ROTATION`.
- Full suite: 115 current plus 7 legacy tests, 122 total.

## 2026-09-16 — v2.4 integrated offline rehearsal

- Added `test_visible_v24_e2e.py`, which connects contract freeze, A/B draft completion, one programmatic next action, deterministic Context Pack, inline synthesis, Final Gate evidence, `DONE`, `STOP`, `DONE_VERIFIED`, and Ledger Doctor health in one no-model run.
- Full suite: 110 current plus 7 legacy tests, 117 total.

## 2026-09-16 — v2.4 short-context brain and Final Gate 2.0

- Added deterministic `context_pack.py` and `programmatic_main.py`, so a replacement Main can resume from a verified short pack and one hash-bound next action.
- Added `final_gate.py`; final claims now bind to located Worker quotes, key values, required machine checks, semantic checks and a bounded final revision.
- Frozen the total elapsed target, external-review budget and resolved mode fingerprint into new contracts.
- Added ledger accounting for one explicitly authorized external review; unauthorized or second rounds fail closed.
- New-contract `DONE_VERIFIED` requires both delivery and Final Gate receipts.
- Full suite: 109 current plus 7 legacy tests, 116 total.

## 2026-09-16 — v2.3 delivery and deadline enforcement

- Wired deterministic delivery validation into `visible_ledger.finalize_run`; new frozen-contract runs cannot become `DONE` or `PARTIAL` without a validated inline result and immutable delivery receipt.
- Bound every supplied Worker body to a final-basis candidate SHA-256 and added a 200-character minimum, closing quick-mode hash-only completion.
- Replaced caller-supplied section/contribution markers with Markdown structure checks, direct Worker-output overlap, minimum section content and repeated-filler rejection.
- Frozen delivery policy and per-Worker deadline limit now enter `contract_sha256`; reservation and replay reject deadlines above the frozen cap.
- Full current and compatibility suite: 97 tests passed. Historical schema-v3 ledgers without the new optional fields remain readable.

## 2026-09-16 — practical-run performance and delivery repair

- Default small/large visible modes now use one parallel draft wave with 2/4 Worker turns; the prior 8/12 full review loops remain available as `small_reviewed` and `large_reviewed`.
- Added elapsed-time, external-review, short-prompt and context-rotation policies. Small mode temporarily requests Luna Medium; speed remains user-managed.
- Added the first deterministic delivery validator; v2.3 subsequently connected and hardened it.
- Historical schema-v3 contracts without `review_policy` continue to replay as full-review contracts.

## 2026-09-14

- Verified Astra handoff against empty local orchestrator and wrote TAKEOVER_STATUS.md before implementation.
- Added SQLite workflow, fake and app-server backends, read-only worker sessions, cross review, revisions, verification, hashed artifacts, duplicate detection, bounded retry and resume.
- Added CLI doctor/run/resume/status/result/logs/start/stop/restart/enqueue and local queue service.
- Added seven deterministic tests and real model-list, Sol High plan, Luna one-turn, and three-concurrent-worker smoke checks.
- Registered this as an important project in canonical memory without changing global routing or CURRENT_TASK.md.
# 2026-09-17 — Brain-led automatic delegation

- Added a default rule that the Brain classifies actionable requests and proactively assigns simple bounded execution to Worker A without waiting for Sue to name A.
- Kept planning, permission boundaries, blocker recovery, acceptance, and final delivery with the Brain.
- Added proportional staffing: one Worker for simple work, more Workers only when parallelism materially improves the result.
- Recorded GitHub upload as the motivating example: the Brain should dispatch it automatically once the upload scope is authorized.
