# Sue Agent Lab portable deployment — v2.7.2

This package lets another Codex workspace reproduce the current collaboration pattern: the user speaks to one visible Main task; Main sends bounded work to visible A/B or A/B/C/D tasks in parallel; Main relays selected results for optional review and writes the final response. Worker identity and memory survive replacement of a chat window.

## Safety and authority

- Only the active Main task dispatches Workers, changes the authoritative roster, writes the ledger, or delivers the final result.
- Do not modify Codex login, VPN, proxy, DNS, or global network settings.
- Automatic retry is 0. Automatic model fallback is disabled. Do not consume a usage reset automatically.
- A real model call requires the user-selected model configuration. If unavailable, stop and report it.
- Worker shared-memory access is a workflow rule in visible tasks. Use the strict backend when operating-system-enforced sandboxing is required.
- Do not claim strict R2 model attestation. Client-visible output, timing, and token use are evidence, not proof of the provider's internal model build.

## Install and verify

Copy `orchestrator/` into the target Shared-Memory root. Use Python 3.9 or newer. From that root run:

```bash
python3 -m orchestrator --help
python3 -m unittest discover -s orchestrator -t . -p 'test_*.py'
```

The reference package passes **155 tests**. Acceptance must be rerun on the target computer; do not copy PASS status from this package.

Before creating Workers, run the offline package check:

```bash
python3 -m orchestrator.portable_doctor .
```

It verifies the manifest, every file hash and size, safe regular-file paths, configuration, and removal of owner-specific absolute paths. A clean template returns PASS with `placeholders_present=true`. After replacing all four Worker task IDs, run `--require-deployed-ids`. In that mode only the manifest-declared `orchestrator/visible_modes.json` may differ from the original package hash; the Doctor then fully validates its modes, ownership metadata, unique IDs and matching chat-archive paths. Every other file remains hash-bound.

## Create the visible tasks

Create four visible Codex tasks:

- `Sue Agent Lab · Worker A`
- `Sue Agent Lab · Worker B`
- `Sue Agent Lab · Worker C`
- `Sue Agent Lab · Worker D`

Default routing is editable. The current reference preference is A/B = GPT-5.6 Luna High, C = GPT-5.6 Terra Low, D = GPT-5.6 Sol Low. Speed is controlled by the user in the app.

Send each task a self-contained initialization prompt identifying its Worker letter and stating that it accepts only bounded Main tasks, does not write authoritative memory or ledgers, does not contact peers directly, and returns complete results. Wait for READY and record each real task ID.

Copy `templates/visible_modes.template.json` to `orchestrator/visible_modes.json`, replace all four `REPLACE_WITH_*_THREAD_ID` values, and update each `chat_archive_path` to contain the matching ID. Keep every Worker at generation 1 with empty history for a fresh install.

Validate both modes:

```bash
python3 -m orchestrator.visible_modes small
python3 -m orchestrator.visible_modes large
```

## Modes and cost limits

- `small`: A+B, one parallel draft wave, 2 Worker turns, capacity floor 1.
- `large`: A+B+C+D, one parallel draft wave, 4 Worker turns, capacity floor 2.
- `small_reviewed`: A+B with bounded peer review, 8 Worker turns, capacity floor 1.
- `large_reviewed`: A+B+C+D with bounded peer review, 12 Worker turns, capacity floor 2.
- External review defaults to 0 rounds and is capped at 1 when the user explicitly requests it.

For a lowest-cost request, run `visible_cost_guard` before dispatch. If usage metadata is unavailable, enforce the frozen call cap. Do not infer a model identity from token counts.

For runtime availability, prepare a snapshot covering every frozen Worker:

```json
{
  "workers": {
    "A": {"state": "AVAILABLE"},
    "B": {"state": "BUSY"}
  }
}
```

Use `AVAILABLE`, `BUSY`, or `UNAVAILABLE`, then run:

```bash
python3 -m orchestrator.visible_capacity capacity.json --mode small --backoff-step 0 > capacity-report.json
python3 -m orchestrator.programmatic_main RUN_LEDGER.jsonl --capacity-report capacity-report.json
```

Busy capacity waits at most twice, for 5 seconds and 15 seconds. After that, the system dispatches only if the mode's capacity floor is met and forces the eventual result to PARTIAL; below the floor it stops. Backoff itself performs no model call.

## Worker memory and window replacement

Create current memory projections and sanitized visible-chat archives:

```bash
python3 -m orchestrator.worker_memory checkpoint
```

Run this checkpoint after a terminal collaboration run and before a Main handoff. It archives user-visible messages only and rebuilds each Worker's `WORKER_STATE.json`, `WORKER_MEMORY.md`, and `BOOTSTRAP.md`. This is a deterministic workflow checkpoint; the Codex UI does not provide an event hook for true message-by-message live archiving.

When a Worker chat becomes too long:

1. Ask the user to create/authorize a replacement visible task if that has not already been requested.
2. Create the new task with the same Worker identity and desired model.
3. Wait for READY and record the new task ID.
4. Run the atomic takeover:

```bash
python3 -m orchestrator.worker_memory takeover \
  --worker A \
  --thread-id NEW_THREAD_ID \
  --expected-thread-id OLD_THREAD_ID
```

The command archives the old chat, refuses active or unreadable ledgers, rejects duplicate ownership, records history, increments the generation, and rebuilds the Worker memory. Archive/unpin the old visible task after the takeover succeeds.

## Main memory and replacement

Install the three files from `templates/` into the target memory structure and fill in the target project's details. A new Main task must read `BRAIN_HANDOFF.md`, `BRAIN_STATE.json`, the project record, `orchestrator/visible_modes.json`, and the relevant Worker memory indexes before acting.

Transfer Main ownership atomically:

```bash
python3 -m orchestrator.brain_takeover \
  --thread-id NEW_MAIN_THREAD_ID \
  --expected-owner OLD_MAIN_THREAD_ID
```

The command refuses takeover while any visible ledger is running or unreadable. It changes the authoritative Main task with a locked expected-owner comparison and records the prior owner. The old chat loses workflow authority, but ordinary visible tasks are not protected by an operating-system write barrier; all Main tasks must still follow the ownership record.

## One visible collaboration run

1. Freeze the exact user request, hard constraints, machine checks, semantic goals, mode, roster, model requests, budgets, capacity policy, and task plan before dispatch.
2. Check ownership, cost, and capacity.
3. Reserve the ledger attempts before sending one self-contained task to every selected Worker.
4. Read each response from the exact task/turn, store the exact content as a result artifact, and bind its hash to the ledger.
5. In quick mode, Main synthesizes after the draft wave. In reviewed mode, route a candidate to a non-author, allow one bounded BLOCKER revision/recheck path, and stop on budget.
6. Run delivery and final gates. A complete answer must cover every accepted Worker contribution, contain substantive required sections, and appear inline in Main's response.
7. Finalize as DONE only when all frozen requirements pass. Missing roster results or unresolved material issues produce PARTIAL; unsafe or unverifiable state produces ABORT.
8. Run `python3 -m orchestrator.worker_memory checkpoint` and update Main handoff memory.

Workers never need to talk directly. Main reads the exact result from one task and sends a short, bounded candidate to another when review is useful. This keeps authority and cost visible.

## Recovery evidence

The strict backend maintains a lease while a model call is blocking. If the process dies, stale recovery marks an in-flight attempt `OUTCOME_UNKNOWN`, preserves completed artifacts, aborts the run, and does not replay that unknown call. The package includes a real subprocess-kill test. It does not disturb the computer's network configuration; response-loss behavior is tested through a controlled backend failure.

## Known platform boundaries

The following need platform or external-service support and remain explicit boundaries:

- strict R2 provider-side model/build attestation;
- operating-system-enforced read-only Shared-Memory for ordinary visible Codex tasks;
- a third-party immutable ledger anchor;
- automatic cross-device synchronization when the Shared-Memory folder itself is not copied or synced;
- a Codex UI event hook for real-time chat archiving.

The local system still detects hash tampering, maintains single-writer authority, keeps portable Worker/Main memory, and can export a complete handoff folder. Do not label the five boundaries above as solved by local code.

## Stable IDs for non-Codex conversations

When Claude, WorkBuddy or another application does not expose a stable native conversation ID, create a stable local ID in Shared-Memory:

```bash
python3 -m orchestrator.external_identity \
  --registry .ai/projects/SUE-AGENT-LAB-2026/EXTERNAL_IDENTITIES.json \
  mint --provider workbuddy --locator 'LOCAL_UNIQUE_LOCATOR' --label 'Kimi review'
```

The raw locator is never stored; only its SHA-256 and an opaque generated ID are written. Copying Shared-Memory to another computer preserves the mapping. This provides local continuity, not proof of the external application's internal identity.
