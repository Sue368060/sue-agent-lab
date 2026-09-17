# User guide

Run all commands from the Shared-Memory root, with global options before the command:

    python3 orchestrator/cli.py doctor
    python3 orchestrator/cli.py run "A small research question" --workers 3
    python3 orchestrator/cli.py status
    python3 orchestrator/cli.py result LAB-xxxxxxxxxxxx
    python3 orchestrator/cli.py logs LAB-xxxxxxxxxxxx

These commands use the free fake backend unless --backend app-server is specified. For a real run, first use:

    python3 orchestrator/cli.py --backend app-server doctor
    python3 orchestrator/cli.py --backend app-server --max-calls 15 --max-tokens 250000 run "Your narrow question" --workers 3

The real run can consume substantial subscription usage. A tiny Luna turn measured about 13,000 total tokens including cached input on this host. No direct price can be inferred from that count. Choose a small question and inspect the limits before raising caps. 3 workers require 15 model calls; 5 require 23. The token cap is checked between turns, not mid-turn; a parallel batch may overshoot before all usage reports arrive.

For a background local queue, use start, enqueue, status, stop, or restart with the same global backend/state options. resume JOB_ID retries a failed or interrupted job within remaining per-step and total-call limits; --max-retries may be raised explicitly after diagnosing a failure. status and result do not invoke a model.

The result directory holds a synthesis JSON, verification JSON, manifest, and all intermediate artifacts. Check verification status before relying on factual claims. A listed local source is only confirmed to exist; substantive support still needs human/source review. Any external action needs a separate user request and approval.

For local source-file checking, put --source /absolute/path/to/file before run (repeat for multiple files). Only those exact user-supplied paths may be read and hashed by the deterministic verifier. URLs and model-invented file paths stay unverified.
