# Agent roles

- Leader: plan, merge revised proposals, run final critic. Default live model gpt-5.6-sol.
- Workers 1–5: independent drafts, two cross-reviews per candidate, and own revisions. Default live model gpt-5.6-luna.
- Deterministic verifier: no model; checks only whether a cited local file exists and its hash, marks URLs and unsupported claims unverified.
- Coordinator: only writer of state, artifacts and process-memory log. It never interprets worker agreement as truth.

## Operating rule for visible daily work

The Brain automatically routes work without waiting for Sue to name a Worker. Worker A is the default executor for a simple, bounded, reversible task such as an already-authorized GitHub upload or routine UI/file operation. The Brain defines the scope and stop condition, monitors progress, handles blockers, verifies the result, and reports the outcome. Parallel Workers are added only when their independent contributions justify the added time and model cost.

Delegation does not transfer authority: a Worker may act only inside Sue's existing authorization and may not broaden external publication, upload contents, recipients, deletion scope, or account changes.

These are logical roles backed by separate app-server threads, not named GUI chats. Every draft starts without other workers' drafts. Reviewers are assigned cyclically and cannot review themselves. Workers and Leader run read-only; no action-taking mode is exposed.
