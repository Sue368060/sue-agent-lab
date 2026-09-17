# Read first

Read `DEPLOYMENT.md` before creating Worker tasks or changing configuration. This package reproduces Sue Agent Lab's visible Main + A/B/C/D workflow without copying Sue's chats, real task IDs, ledgers, credentials, caches, photos, or PDFs.

The UUIDs in `orchestrator/visible_modes.json` are anonymous test placeholders. Replace them with the four newly created task IDs before a real run. Then run the full test command and establish Worker memory checkpoints.

Implementation baseline: **v2.7.2**, 155 local tests. It includes replaceable Main and Worker windows, persistent per-Worker memory, schema-2 ownership history, deterministic ledgers and context packs, runtime capacity floor/backoff, low-cost guard, final delivery checks, lease watchdog, process-kill/pipe-loss/TCP-loss recovery without replay, an offline portable Doctor, and stable local IDs for non-Codex conversations.

It cannot independently prove the provider's internal model identity (strict R2), make a visible Codex task physically read-only, or create an external immutable audit anchor without an external service. These boundaries are stated in `DEPLOYMENT.md`; do not report them as passed.
