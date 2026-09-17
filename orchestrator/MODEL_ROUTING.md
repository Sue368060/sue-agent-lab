# Model routing and budget

On submit, read the live model catalog. Prefer visible Luna for worker and visible Sol for Leader. Explicit names are accepted only when actually available. Astra is excluded even if listed. If no safe pair exists, fail closed rather than silently upgrading.

Defaults: 3 workers, at most 24 model calls, one retry per step, 20 minutes per run, and 50,000 observed tokens. A full real 3-worker loop usually needs a larger explicit token cap because app-server model turns include substantial cached input. Per-step and total call caps persist across resume. Token usage is recorded when app-server sends a usage notification and checked before later turns; it is not a hard provider-side budget.

No service tier is assumed from the user's informal 1.5fast preference. The catalog and selected model names are reported by doctor.
