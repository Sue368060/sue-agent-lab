# Agent roles

- Leader: plan, merge revised proposals, run final critic. Default live model gpt-5.6-sol.
- Workers 1–5: independent drafts, two cross-reviews per candidate, and own revisions. Default live model gpt-5.6-luna.
- Deterministic verifier: no model; checks only whether a cited local file exists and its hash, marks URLs and unsupported claims unverified.
- Coordinator: only writer of state, artifacts and process-memory log. It never interprets worker agreement as truth.

These are logical roles backed by separate app-server threads, not named GUI chats. Every draft starts without other workers' drafts. Reviewers are assigned cyclically and cannot review themselves. Workers and Leader run read-only; no action-taking mode is exposed.
