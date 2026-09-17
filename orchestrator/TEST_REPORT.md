# Test report — 2026-09-14

## 2026-09-17 v2.7.2

Final audit reproduced and fixed a deployment acceptance contradiction: installing real Worker IDs necessarily changes `visible_modes.json`, so the old hash-only Doctor could never pass `--require-deployed-ids`. The manifest now declares exactly one deployment-mutable file. Package mode still requires its original hash; deployed mode permits only that roster to differ and then validates all mode, ownership, ID uniqueness, placeholder and archive-path rules. Manifest traversal, parent symlinks, duplicate paths and malformed hash metadata fail closed. External conversation identity resolution now validates lookup input and every stored registry entry instead of leaking raw exceptions on malformed JSON structures. A test that incorrectly depended on the package source living outside a temporary extraction directory now uses a fixed non-temporary example while preserving the actual temporary-Worker rejection check. Standard discovery passes **155 tests** with `ResourceWarning` treated as an error.

## 2026-09-17 v2.7.1

A real `127.0.0.1` TCP server now accepts the submitted payload and disconnects without a response. The strict Lab records `outcome_unknown`, returns ABORT, preserves exactly one attempted call and performs no retry. This test uses the operating-system network stack but makes no external request and changes no network configuration. Standard discovery passes **151 tests**.

## 2026-09-17 v2.7.0

Standard discovery passes **150 tests**. The new portable Doctor detects tampering, deployment placeholders and owner-specific paths; the external identity registry is stable, atomic and stores only locator hashes; and a real subprocess closes its transport pipe immediately after `turn/start`, producing `OUTCOME_UNKNOWN` with no retry. Resource-warning enforcement also passes after deterministic stream cleanup.

## 2026-09-17 v2.6.0

Standard discovery passes **143 tests**. New coverage verifies blocking-call lease renewal, real subprocess-kill recovery without unknown-call replay, Worker archive-plus-memory checkpoints, runtime capacity ALLOW/BACKOFF/DEGRADED/STOP decisions, tamper rejection, and schema-2 generation fixtures independent of the live roster. A/B generation-2 visible tasks were created, initialized from their persistent memory and atomically claimed. No model answer was requested for these checks.

# 2026-09-16 v2.4.3 external-review closure

Claude's two delivery bypasses were reproduced offline: one realistic paragraph could fill all six required sections, and raw Worker fragments could be arranged as a final answer without Main-Agent synthesis. Validator v3 now rejects both using symmetric 8-gram section similarity plus a synthesis-novelty floor. Earlier frozen contracts keep validator v2 replay semantics so historical ledgers remain healthy.

The Luna High snapshot observed a live ledger while the authorized A/B micro-run was still in progress; that ledger later finalized `HEALTHY/DONE`. Its structural concern was valid: takeover formerly trusted only `BRAIN_STATE.json`. The takeover command now also scans all visible ledgers and fails closed on a running or unreadable ledger. Package imports and one-command discovery were repaired. Standard discovery passes 127 tests; all three stored ledgers replay `HEALTHY/DONE`. This stage used no model calls.

## 2026-09-16 v2.4.2 real A/B micro-run and low-cost guard

Run `VISIBLE-AB-MICRO-20260916-01` used exactly two concurrent Luna Medium draft turns, no review/retry/C/D/external model, and reached `DONE_VERIFIED` with Ledger Doctor `HEALTHY`. B completed in 8.7 seconds and A in 21.6 seconds, so model wait was about 22 seconds. The useful short outputs still carried 73,138 and 176,854 input tokens because the persistent Worker chats were long; exact UI quota percentage is unavailable. Added a metadata-only low-cost preflight: contexts above 50,000 recent input tokens return `STOP_FOR_WORKER_ROTATION`; missing usage falls back to `max_llm_calls`. Five new tests prove allow/stop/fallback, no message-body capture, and hash-bound Programmatic Main stopping. 115 current plus seven legacy tests pass, 122 total. No model call was used after the two authorized drafts.

## 2026-09-16 v2.4 complete offline end-to-end acceptance

Added one deterministic integration rehearsal covering the entire quick A/B path in a single run: frozen contract, two draft reservations/completions, programmatic synthesis routing, deterministic Context Pack, six-section inline result, claim/quote and machine/semantic evidence, Final Gate receipt, `DONE`, `STOP`, `DONE_VERIFIED`, and final Ledger Doctor `HEALTHY/DONE`. This proves the previously separate v2.4 modules interoperate. 110 current tests and seven legacy tests pass, 117 total. No Worker, external reviewer, or real Backend was called.

## 2026-09-16 v2.4 brain and whole-system closure

SC-2 deterministic Context Pack, SC-3 programmatic next action and SC-4 Final Gate 2.0 are connected. Tests cover byte-stable packs, artifact hash/path failures, stale ledger heads, elapsed-time stopping, deterministic review/revision routing, invented claim values, false quotes, machine/semantic evidence, final revision limits, external-review authorization/cap and resolved-mode fingerprints. The original stage baseline was 109 current tests and seven legacy tests, 116 total. No real model call was used.

## 2026-09-16 v2.3 Claude-review closure

The delivery validator is now part of ledger finalization. New tests prove that completion rejects missing final-delivery evidence, Worker text whose SHA-256 does not match a final-basis candidate, deadlines above the frozen 300/480-second limit, repeated filler, thin pseudo-sections, and plain labels presented without Markdown structure. The frozen contract now includes the delivery rules and maximum Worker deadline. Fifty focused tests and the 97-test combined current/compatibility suite pass. The historical travel ledger remains HEALTHY/DONE at revision 13.

## 2026-09-16 practical-run optimization

The first real travel run took 1424.3 seconds and used six Worker turns plus two serial Claude review rounds. The default modes were therefore changed from mandatory review loops to one parallel draft wave: small=2 turns and large=4 turns. Full 8/12 review loops remain explicit reviewed modes. New tests prove quick two-draft DONE, reject review dispatch in quick mode, preserve full-review coverage rules in reviewed modes, validate time/external-review budgets, and reject link-only, contribution-dropping or overcompressed Main-chat delivery. The combined suite passes 92 tests on system Python 3.9. The historical travel ledger remains HEALTHY/DONE at revision 13.

Environment: macOS, Python 3.9.6, codex-cli 0.153.4. Local v2 app-server schema generated before adapter implementation.

Deterministic tests: seven passed. Covered 3- and 5-worker full state machines, sealed drafts, no-self ordinary/critical cross-review, 16/24 persisted steps, duplicate dedupe, artifact manifest and tamper detection, injected draft failure and explicit retry/recovery without repeating plan or saved sibling drafts, changed memory hash notification, invalid-output retry, model exclusion, local-source verification boundary, and call-budget admission.

Queue service smoke: foreground serve, separate enqueue, status, and cooperative stop passed in a temporary state directory. A detached start was observed launching, but long-lived detached survival was not independently verified under the tool sandbox.

Real read-only checks: model/list returned Sol, Astra, Terra, Luna, 5.5; routing picked Luna/Sol and excluded Astra. One tiny Luna JSON turn completed. Three concurrent tiny Luna turns completed with distinct thread IDs and matching worker identities. One Sol High planning turn returned the required JSON contract. No Astra call was made. Approximate measured total tokens: 13,893, 13,214, and 13,077 for each of the three concurrent Luna turns; Sol High plan: 15,736. Reported totals include cached input.

Not yet verified: a 15-turn full real research loop, daemon process kill/restart under load, provider-enforced token cutoff, real content accuracy, and automated external URL evidence checks. The deterministic verifier reports local-file existence/hash, not semantic proof. No iCloud or USB backup was performed.

## 2026-09-15 ledger doctor and fencing

Schema-v3 visible-ledger start/reserve/complete/fail/cancel/finalize writes now perform owner/epoch/revision compare-and-swap under the append lock and embed the validated frozen contract. Two-process create and same-revision reserve races each admitted exactly one writer. Legacy schemas remain readable and reject writes. The read-only Doctor classifies corruption and permits explicit repair only for an incomplete physical tail after a fully valid schema-v3 prefix; repair rechecks file hash and fencing state and durably backs up the damaged bytes before truncation. Tests cover middle and semantic corruption, complete JSON tails, malformed UUIDs, contract tampering, bool revision/epoch and float epoch bypasses. An independent final audit passed. Baseline: 77 current plus seven legacy tests on Python 3.12 and macOS Python 3.9. Historical schema-v1 ledger replay remains HEALTHY/DONE/read-only. Zero visible Worker or real Backend calls were used.
