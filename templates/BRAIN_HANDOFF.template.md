# Sue Agent Lab — Main Agent Handoff

## Startup

Read this file, `BRAIN_STATE.json`, the project records and `orchestrator/visible_modes.json`. If the user asks this chat to continue or take over, claim Main Agent ownership atomically before dispatch or authoritative writes.

## Current Workers

Fill A–D thread IDs, model, thinking level and optional roles from `visible_modes.json`.

## Completed acceptance

Record only tests and real runs that actually passed in this deployment. Never copy PASS status from the reference system without rerunning the corresponding check.

## Current run

Record the active run ID or `none`, reserved/completed attempts, exact answer hashes, unresolved BLOCKERs and stop state.

## Next action

Write one exact next stage that a successor can execute without replaying completed work.

## Prohibitions

No automatic retry, model fallback, usage reset, speed management, network/login modification, Worker authoritative write, or false strict-R2 acceptance.
