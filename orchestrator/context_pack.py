"""Build byte-stable short context and acceptance evidence from one ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .short_context_contract import sha256_json
from .visible_ledger import LedgerError, read_context_state


class ContextPackError(ValueError):
    pass


MAX_ARTIFACT_EXCERPT_CHARS = 2400


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _artifact_excerpt(text: str) -> str:
    if len(text) <= MAX_ARTIFACT_EXCERPT_CHARS:
        return text
    head = MAX_ARTIFACT_EXCERPT_CHARS * 3 // 4
    tail = MAX_ARTIFACT_EXCERPT_CHARS - head
    return (text[:head] + "\n\n[... deterministic middle omitted ...]\n\n" +
            text[-tail:])


def _verify_artifacts(state: dict, artifact_root: Path,
                      manifest: dict[str, dict]) -> tuple[dict, list[dict]]:
    if not isinstance(manifest, dict):
        raise ContextPackError("artifact manifest must be an object")
    candidates = state["candidates"]
    extra = sorted(set(manifest) - set(candidates))
    if extra:
        raise ContextPackError("artifact manifest contains unbound candidates")
    root = Path(artifact_root).resolve()
    evidence = {}
    errors = []
    for candidate_id in sorted(candidates):
        candidate = candidates[candidate_id]
        entry = manifest.get(candidate_id)
        item = {
            "candidate_id": candidate_id,
            "worker_id": candidate["worker_id"],
            "version": candidate["version"],
            "expected_sha256": candidate["sha256"],
            "verified": False,
        }
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            errors.append({"code": "MISSING_ARTIFACT", "candidate_id": candidate_id})
            evidence[candidate_id] = item
            continue
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ContextPackError("artifact path must stay below artifact root")
        path = root / relative
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            errors.append({"code": "MISSING_ARTIFACT", "candidate_id": candidate_id})
            item["path"] = relative.as_posix()
            evidence[candidate_id] = item
            continue
        if path.is_symlink() or root not in resolved.parents or not resolved.is_file():
            raise ContextPackError("artifact path is unsafe")
        raw = resolved.read_bytes()
        actual_hash = _sha256_bytes(raw)
        item.update(path=relative.as_posix(), actual_sha256=actual_hash,
                    bytes=len(raw))
        if entry.get("sha256") != candidate["sha256"] or actual_hash != candidate["sha256"]:
            errors.append({"code": "ARTIFACT_HASH_MISMATCH",
                           "candidate_id": candidate_id})
            evidence[candidate_id] = item
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            errors.append({"code": "ARTIFACT_NOT_UTF8", "candidate_id": candidate_id})
            evidence[candidate_id] = item
            continue
        item.update(verified=True, chars=len(text), excerpt=_artifact_excerpt(text))
        evidence[candidate_id] = item
    return evidence, errors


def _latest_candidates(state: dict) -> dict[str, str]:
    latest = {}
    for candidate_id, candidate in state["candidates"].items():
        worker_id = candidate["worker_id"]
        current = latest.get(worker_id)
        if current is None or candidate["version"] > current[0]:
            latest[worker_id] = (candidate["version"], candidate_id)
    return {worker_id: details[1] for worker_id, details in sorted(latest.items())}


def _acceptance_snapshot(state: dict, artifacts: dict,
                         evidence_errors: list[dict]) -> dict:
    contract = state.get("contract")
    if state.get("ledger_schema_version") != 3 or not isinstance(contract, dict):
        raise ContextPackError("context packs require a frozen schema-v3 contract")
    constraints = contract.get("hard_constraints")
    constraints_frozen = (
        isinstance(constraints, dict) and constraints.get("status") == "FROZEN" and
        isinstance(constraints.get("items"), list) and
        constraints.get("source_sha256") == contract.get("source_request_sha256")
    )
    latest = _latest_candidates(state)
    latest_complete = set(latest) == set(state["workers"])
    latest_verified = latest_complete and all(
        artifacts.get(candidate_id, {}).get("verified") is True
        for candidate_id in latest.values()
    )
    ready = bool(
        constraints_frozen and state["done_prerequisites_satisfied"] and
        not state["active_attempts"] and not state["unresolved_blockers"] and
        not evidence_errors and latest_verified
    )
    terminal = state.get("terminal")
    delivery_receipt = terminal.get("delivery_receipt") if isinstance(terminal, dict) else None
    final_gate_receipt = terminal.get("final_gate_receipt") if isinstance(terminal, dict) else None
    final_gate_required = contract.get("final_gate_contract") is not None
    done_verified = bool(
        ready and terminal and terminal.get("status") == "DONE" and
        isinstance(delivery_receipt, dict) and delivery_receipt.get("status") == "PASS" and
        (not final_gate_required or
         (isinstance(final_gate_receipt, dict) and
          final_gate_receipt.get("status") == "PASS"))
    )
    if done_verified:
        eligibility = "DONE_VERIFIED"
    elif ready and terminal is None:
        eligibility = "READY_FOR_FINAL"
    elif terminal and terminal.get("status") == "DONE":
        eligibility = "LEGACY_DONE_UNVERIFIED"
    else:
        eligibility = "REFUSE_DONE"
    body = {
        "schema_version": 1,
        "run_id": state["run_id"],
        "contract_sha256": state["contract_sha256"],
        "ledger_head_sha256": state["ledger_head_sha256"],
        "ledger_revision": state["revision"],
        "mode": state["mode"],
        "run_status": terminal["status"] if terminal else "RUNNING",
        "eligibility": eligibility,
        "ready_for_final": ready,
        "done_verified": done_verified,
        "constraints_frozen": constraints_frozen,
        "active_attempts": state["active_attempts"],
        "unresolved_blocker_ids": sorted(state["unresolved_blockers"]),
        "latest_candidate_by_worker": latest,
        "artifact_evidence": {
            candidate_id: {
                key: value for key, value in item.items() if key != "excerpt"
            } for candidate_id, item in sorted(artifacts.items())
        },
        "evidence_errors": evidence_errors,
        "delivery_receipt": delivery_receipt,
        "final_gate_receipt": final_gate_receipt,
    }
    body["snapshot_sha256"] = sha256_json(body)
    return body


def _render_context_pack(state: dict, artifacts: dict, snapshot: dict) -> bytes:
    contract = state["contract"]
    lines = [
        f"# Sue Agent Lab Context Pack — {state['run_id']}",
        "",
        "This file is generated from the frozen contract, replayed ledger state, and hash-verified artifacts.",
        "It is read-only context. It does not grant dispatch or write authority.",
        "",
        "## Run identity",
        "",
        f"- Contract SHA-256: `{state['contract_sha256']}`",
        f"- Ledger head SHA-256: `{state['ledger_head_sha256']}`",
        f"- Revision: `{state['revision']}`",
        f"- Mode: `{state['mode']}`",
        f"- Acceptance: `{snapshot['eligibility']}`",
        "",
        "## User request",
        "",
        contract["source_request"].strip(),
        "",
        "## Frozen constraints",
        "",
        "```json",
        _json(contract["hard_constraints"]),
        "```",
        "",
        "## Success criteria",
        "",
        "```json",
        _json(contract["success_criteria"]),
        "```",
        "",
        "## Worker assignments",
        "",
        "```json",
        _json(contract["task_plan"]),
        "```",
        "",
        "## Replayed run state",
        "",
        "```json",
        _json({"attempts": state["attempts"],
               "active_attempts": state["active_attempts"],
               "unresolved_blockers": state["unresolved_blockers"],
               "terminal": state["terminal"]}),
        "```",
        "",
        "## Verified candidate artifacts",
        "",
    ]
    if not artifacts:
        lines.append("No candidate artifact has been registered yet.")
    for candidate_id, item in sorted(artifacts.items()):
        lines.extend([
            f"### {candidate_id}",
            "",
            f"- Worker: `{item['worker_id']}`",
            f"- Version: `{item['version']}`",
            f"- Verified: `{'yes' if item['verified'] else 'no'}`",
            f"- Expected SHA-256: `{item['expected_sha256']}`",
        ])
        if item.get("verified"):
            lines.extend(["", "```text", item["excerpt"], "```"])
        lines.append("")
    lines.extend([
        "## Acceptance snapshot",
        "",
        f"- Snapshot SHA-256: `{snapshot['snapshot_sha256']}`",
        f"- Ready for final: `{'yes' if snapshot['ready_for_final'] else 'no'}`",
        f"- DONE verified: `{'yes' if snapshot['done_verified'] else 'no'}`",
        f"- Evidence errors: `{len(snapshot['evidence_errors'])}`",
        "",
    ])
    return "\n".join(lines).encode("utf-8")


def build_context_bundle(*, ledger_path: Path, artifact_root: Path,
                         artifact_manifest: dict[str, dict]) -> dict:
    try:
        state = read_context_state(Path(ledger_path))
    except LedgerError as exc:
        raise ContextPackError(f"ledger cannot be replayed: {exc}") from exc
    artifacts, errors = _verify_artifacts(state, Path(artifact_root), artifact_manifest)
    snapshot = _acceptance_snapshot(state, artifacts, errors)
    snapshot_bytes = (json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                 indent=2) + "\n").encode("utf-8")
    context_bytes = _render_context_pack(state, artifacts, snapshot)
    return {
        "context_pack_bytes": context_bytes,
        "acceptance_snapshot_bytes": snapshot_bytes,
        "snapshot": snapshot,
        "context_pack_sha256": _sha256_bytes(context_bytes),
        "acceptance_snapshot_file_sha256": _sha256_bytes(snapshot_bytes),
    }


def assert_ready_for_final(snapshot: dict) -> None:
    if not isinstance(snapshot, dict) or snapshot.get("eligibility") != "READY_FOR_FINAL":
        raise ContextPackError("Final Gate refuses finalization")


def assert_done_verified(snapshot: dict) -> None:
    if not isinstance(snapshot, dict) or snapshot.get("eligibility") != "DONE_VERIFIED":
        raise ContextPackError("Final Gate refuses DONE")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def write_context_bundle(*, ledger_path: Path, artifact_root: Path,
                         artifact_manifest: dict[str, dict], output_dir: Path) -> dict:
    bundle = build_context_bundle(
        ledger_path=ledger_path, artifact_root=artifact_root,
        artifact_manifest=artifact_manifest)
    output = Path(output_dir)
    _atomic_write(output / "context_pack.md", bundle["context_pack_bytes"])
    _atomic_write(output / "acceptance_snapshot.json",
                  bundle["acceptance_snapshot_bytes"])
    return {key: value for key, value in bundle.items()
            if key not in ("context_pack_bytes", "acceptance_snapshot_bytes")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sue-context-pack")
    parser.add_argument("ledger", type=Path)
    parser.add_argument("artifact_root", type=Path)
    parser.add_argument("artifact_manifest", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.artifact_manifest.read_text(encoding="utf-8"))
        result = write_context_bundle(
            ledger_path=args.ledger, artifact_root=args.artifact_root,
            artifact_manifest=manifest, output_dir=args.output_dir)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
