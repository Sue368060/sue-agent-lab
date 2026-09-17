# Sue Agent Lab V1.0.1 walking skeleton

For ordinary Sue Agent Lab work, Sue now prefers a main Codex task coordinating two visible, independent Worker tasks in parallel, cross-feeding their results, and performing the final check. The current user-facing workflow is in `outputs/Sue_Agent_Lab_可见并行工作模式.md` in the 2026-09-14 task. This V1.0.1 CLI remains an opt-in strict-permission/test harness; its R2 real loop is not accepted.

This is a separate, opt-in implementation. It does not replace the earlier `orchestrator/` prototype or the shared-memory control plane.

From the installed Shared-Memory root:

```bash
/opt/homebrew/bin/python3.11 -m orchestrator.v101.cli run "A low-risk goal" --backend fake
/opt/homebrew/bin/python3.11 -m orchestrator.v101.cli result <run-id>
/opt/homebrew/bin/python3.11 -m orchestrator.v101.cli stop <run-id>
/opt/homebrew/bin/python3.11 -m orchestrator.v101.cli recover <run-id>
```

Fake is the default and makes no model calls. A run produces two independent drafts, cross-reviews by the other Worker, and a Leader synthesis in `orchestrator/lab/runs/<run-id>/`. The Orchestrator alone writes SQLite and hashed artifacts; Workers receive distinct read-only input workspaces. A single run uses five backend calls on the happy path. The default six-call limit allows one retry. The admission rule reserves enough calls for at least one draft, its independent review, and synthesis; otherwise it returns an abort report instead of an invented final.

`result` verifies every recorded artifact's fixed path, SHA-256, and completed-attempt link before showing the answer. A mismatch returns `INTEGRITY_ERROR` with exit code 2. Replaying an already completed attempt performs the same check and never calls a Worker on a mismatched artifact. `stop` reads run status independently, so a damaged result cannot prevent cancelling an active run.

Each active run owns a renewable SQLite lease. The Orchestrator refreshes it around every backend call and removes it when the run reaches a terminal state. `recover` is an explicit offline operation for a process that died while its run remained `RUNNING`: it refuses an unexpired lease and also refuses a recently updated legacy run with no lease. Lease expiry means that the Orchestrator can no longer prove ownership; it does not prove the old process has exited. Once the lease is expired, recovery therefore fails closed: it records a recovery event, changes only still-running attempts to `OUTCOME_UNKNOWN`, marks the run `ABORT`, preserves every existing artifact, and performs no backend call or retry. Even a synthesis artifact committed just before a crash remains audit evidence only and is not exposed as a final answer for an aborted run. Recovery never runs automatically.

Real execution is held at CP2. It requires `--backend real --real-approved --model <actual-id> --effort <effort> --identity-probe-thread <completed-thread-id>` and a reviewed entry in `model_registry.json`. The identity probe only reads account usage for an already completed thread and blocks the run before a new model turn if no per-thread model/effort group is available. Passing the probe is not real-run acceptance; the newly created one-turn thread must still pass its own identity check. The configured model/effort stays fixed for the run. `manual_only` entries require `--allow-manual-model` and should only be used after Sue's explicit authorization for that run. The App Server backend builds an ephemeral, per-Worker permission profile with root deny, minimal runtime read, own workspace read, and network disabled. It does not edit Codex login or global config. `thread/start` must report the profile and exact requested model or the turn is refused.

Runtime temporary directories are rejected for real Worker workspaces because the platform minimal-read policy can expose other files there. Use the project `lab/runs/` tree.

If a model response completes but later fails schema validation or independent model/effort evidence, the Orchestrator stores it under `rejected-results/` with a SQLite record and SHA-256. It remains audit evidence only: it cannot satisfy an attempt, enter synthesis, or become final, and it never triggers an automatic replay. Neutral App Server operations use the operating system's temporary directory; no macOS-only path is required.

Validation:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/sue-lab-v101-pycache /opt/homebrew/bin/python3.11 -m unittest orchestrator.v101.test_v101 -v
```

The current local tests do not establish real model quality or runtime token-usage completeness. R1 completed one real Sol High turn. R2 attempted a five-turn real run but stopped after the first Worker because the optional per-thread billed model/effort usage group was unavailable. A later read-only probe against an already completed visible thread again returned no per-thread usage, so the new preflight blocks further real runs on this account before a model turn. The backend also treats a lost turn-start response or post-submission uncertainty as fatal, never retries a completed turn because identity lookup failed, and accepts only matching thread/turn usage notifications. Do not treat the requested model echoed in a result as independent execution evidence.
