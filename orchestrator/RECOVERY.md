# Recovery

Use status and logs to locate the failed stage, then inspect the artifact directory and manifest. A duplicate run goal returns the existing job. resume JOB_ID reuses every completed step. A step that exhausted its retry allowance will not run again unless the operator explicitly raises --max-retries. Total call count is never reset by resume. Failed background jobs are not retried in a loop.

The writer lock prevents simultaneous coordinators from modifying the same state directory. A killed process releases the OS lock; a restarted queue service selects jobs left running. Pending remote turns are not reattached automatically, so a crash may result in one extra model turn on resume. Do not delete the SQLite database or artifacts to recover; make a copy and inspect first.

stop is cooperative and takes effect after the current job. For immediate interruption, stop the process through normal OS controls, then inspect state before resume. No destructive reset command is provided.
