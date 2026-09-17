# Architecture

Goal -> plan -> sealed parallel drafts -> two peer reviews per draft (ordinary and critical, never self) -> parallel revisions -> Leader synthesis -> deterministic source-path check -> Leader final critic -> process-only memory checkpoint.

One Lab writer holds an OS file lock. SQLite records jobs, attempts, steps, events, memory snapshots, and memory proposals. Event JSONL is exported at job completion or failure. Artifacts are atomically replaced and listed in manifest.json with SHA-256 hashes. Duplicate normalized goal and worker-count submissions return the same job. Completed steps are reused on resume; incomplete calls may be reissued after a crash, so remote model execution is at-least-once rather than exactly-once.

The local queue service is optional. start launches it, enqueue adds a job, status inspects it, stop asks it to finish its current job, and restart waits briefly for shutdown. On startup the service resumes queued or previously running jobs; failed jobs require explicit resume. It does not create login items or schedules.

All model turns use read-only Codex sandbox and never receive coordinator write rights. New thread per call makes independent drafts genuinely isolated. The process uses one app-server connection and routes responses by request ID and turn completion by thread ID. Incoming approval requests are denied. No Astra model is selected.

Known gaps: no hard mid-turn token interruption, no external URL retrieval, no proof that a source file supports a claim beyond existence and hash, no true exactly-once remote calls, and no full real multi-round acceptance run yet.
