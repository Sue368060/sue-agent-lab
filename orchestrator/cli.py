"""Minimal run / stop / result / recover CLI."""

import argparse
import json
from pathlib import Path
import sqlite3

from .backend import AppServerBackend, BackendError, FakeBackend
from .lab import Lab, read_result, read_run_status, recover_stale_run


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="sue-agent")
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("goal")
    run.add_argument("--backend", choices=("fake", "real"), default="fake")
    run.add_argument("--model", help="actual model ID; required for real")
    run.add_argument("--effort", default="high")
    run.add_argument("--max-llm-calls", type=int, default=6)
    run.add_argument("--max-minutes", type=int, default=20)
    run.add_argument("--policy-file", type=Path, default=Path(__file__).with_name("model_registry.json"))
    run.add_argument("--allow-manual-model", action="store_true")
    run.add_argument("--real-approved", action="store_true", help="explicit CP2 release for this run")
    run.add_argument("--identity-probe-thread",
                     help="prior completed thread with model/effort usage evidence; required for real")
    stop = sub.add_parser("stop")
    stop.add_argument("run_id")
    result = sub.add_parser("result")
    result.add_argument("run_id")
    recover = sub.add_parser("recover")
    recover.add_argument("run_id")
    recover.add_argument("--stale-after-seconds", type=int, default=300)
    args = p.parse_args(argv)
    if args.command == "stop":
        if not args.run_id or not args.run_id.replace("-", "").isalnum():
            p.error("invalid run_id")
        run_dir = args.root.resolve() / "lab" / "runs" / args.run_id
        if not run_dir.is_dir():
            p.error("unknown run_id")
        status = read_run_status(args.root, args.run_id)
        if status != "RUNNING":
            print(json.dumps({"run_id": args.run_id, "status": status, "cancel_requested": False}))
            return 0
        (run_dir / "STOP").touch(exist_ok=True)
        print(json.dumps({"run_id": args.run_id, "cancel_requested": True}))
        return 0
    if args.command == "result":
        try:
            print(json.dumps(read_result(args.root, args.run_id), ensure_ascii=False, indent=2))
            return 0
        except BackendError as exc:
            if exc.kind != "artifact_integrity":
                raise
            print(json.dumps({"run_id": args.run_id, "status": "INTEGRITY_ERROR",
                              "error": f"{exc.kind}: {exc}"}, ensure_ascii=False))
            return 2
    if args.command == "recover":
        try:
            print(json.dumps(recover_stale_run(
                args.root, args.run_id, args.stale_after_seconds
            ), ensure_ascii=False, indent=2))
            return 0
        except (BackendError, FileNotFoundError, ValueError, sqlite3.Error) as exc:
            try:
                status = read_run_status(args.root, args.run_id)
            except (BackendError, FileNotFoundError, ValueError, sqlite3.Error):
                status = "UNKNOWN"
            kind = exc.kind if isinstance(exc, BackendError) else type(exc).__name__
            print(json.dumps({"run_id": args.run_id,
                              "status": status,
                              "recovered": False, "error": f"{kind}: {exc}"},
                             ensure_ascii=False))
            return 2
    if args.backend == "real":
        if not args.real_approved or not args.model or not args.identity_probe_thread:
            p.error("real backend requires CP2 --real-approved, --model, and --identity-probe-thread")
        registry = json.loads(args.policy_file.read_text(encoding="utf-8"))["models"]
        if args.model not in registry:
            p.error("model has no reviewed registry entry")
        manual_only = {model_id for model_id, config in registry.items() if config.get("manual_only") is True}
        backend = AppServerBackend(args.model, args.effort, manual_only, args.allow_manual_model)
        try:
            backend.check_identity_source(args.identity_probe_thread)
        except BackendError as exc:
            p.error(f"real identity preflight failed before any model turn: {exc}")
        model = args.model
    else:
        backend = FakeBackend()
        model = "fake"
    try:
        lab = Lab(args.root, backend, model, "none" if args.backend == "fake" else args.effort,
                  max_llm_calls=args.max_llm_calls, max_minutes=args.max_minutes)
        print(json.dumps(lab.run(args.goal), ensure_ascii=False, indent=2))
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    raise SystemExit(main())
