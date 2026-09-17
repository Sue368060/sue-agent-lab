"""Offline portability checks for a Sue Agent Lab deployment bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any

from .visible_modes import resolve_mode


class PortableDoctorError(ValueError):
    pass


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


_DEPLOYMENT_MUTABLE_FILES = {"orchestrator/visible_modes.json"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _safe_manifest_path(root: Path, value: object) -> tuple[str, Path]:
    if not isinstance(value, str) or not value or "\\" in value:
        raise PortableDoctorError("manifest path is invalid")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise PortableDoctorError(f"unsafe manifest path: {value}")
    path = root.joinpath(*relative.parts)
    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise PortableDoctorError(f"manifest path escapes package root: {value}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise PortableDoctorError(f"manifest path uses a symlink: {value}")
    return value, path


def inspect_portable_root(root: Path, *, require_deployed_ids: bool = False) -> dict[str, Any]:
    root = Path(root).resolve()
    manifest_path = root / "MANIFEST.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PortableDoctorError(f"manifest unavailable: {exc}") from exc
    entries = manifest.get("files")
    source_files = manifest.get("source_files")
    if (not isinstance(entries, list) or not isinstance(source_files, int) or
            isinstance(source_files, bool) or source_files != len(entries)):
        raise PortableDoctorError("manifest file count is invalid")

    declared_mutable = manifest.get("deployment_mutable_files", [])
    if (not isinstance(declared_mutable, list) or
            not all(isinstance(item, str) for item in declared_mutable) or
            len(declared_mutable) != len(set(declared_mutable))):
        raise PortableDoctorError("deployment mutable-file list is invalid")
    mutable_files = set(declared_mutable)
    if mutable_files and mutable_files != _DEPLOYMENT_MUTABLE_FILES:
        raise PortableDoctorError("deployment mutable-file list is not allowed")
    if require_deployed_ids and mutable_files != _DEPLOYMENT_MUTABLE_FILES:
        raise PortableDoctorError(
            "manifest does not declare the deployed Worker roster as mutable")

    problems: list[str] = []
    checked = []
    changed_mutable_files = []
    seen_paths: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            problems.append("invalid manifest entry")
            continue
        try:
            relative, path = _safe_manifest_path(root, item.get("path"))
        except PortableDoctorError as exc:
            problems.append(str(exc))
            continue
        if relative in seen_paths:
            problems.append(f"duplicate manifest path: {relative}")
            continue
        seen_paths.add(relative)
        expected_bytes = item.get("bytes")
        expected_sha = item.get("sha256")
        if (not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool) or
                expected_bytes < 0 or not isinstance(expected_sha, str) or
                not _SHA256_RE.fullmatch(expected_sha)):
            problems.append(f"{relative}: invalid size or SHA-256 metadata")
            continue
        try:
            if not path.is_file():
                raise OSError("not a regular file")
            raw = path.read_bytes()
        except OSError as exc:
            problems.append(f"{relative}: {exc}")
            continue
        mismatch = len(raw) != expected_bytes or _sha256(raw) != expected_sha
        if mismatch and require_deployed_ids and relative in mutable_files:
            changed_mutable_files.append(relative)
        elif mismatch:
            problems.append(f"{relative}: size or hash mismatch")
        checked.append(relative)

    if len(seen_paths) != len(entries):
        problems.append("manifest paths are not unique")
    if mutable_files - seen_paths:
        problems.append("deployment mutable file is missing from manifest")

    config_path = root / "orchestrator/visible_modes.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        for mode in config.get("modes", {}):
            resolve_mode(config, mode)
        if not isinstance(config.get("workers"), dict):
            raise ValueError("workers must be an object")
    except (OSError, TypeError, ValueError) as exc:
        problems.append(f"visible mode config: {exc}")
        config = {"workers": {}}

    anonymous = {
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
        "44444444-4444-4444-8444-444444444444",
    }
    thread_ids = [worker.get("thread_id") for worker in
                  config.get("workers", {}).values() if isinstance(worker, dict)]
    placeholders_present = any(thread_id in anonymous or (
        isinstance(thread_id, str) and thread_id.startswith("REPLACE_WITH_"))
        for thread_id in thread_ids)
    if require_deployed_ids and placeholders_present:
        problems.append("Worker task IDs are still portable placeholders")
    if require_deployed_ids and not placeholders_present:
        for worker_id, worker in config.get("workers", {}).items():
            if not isinstance(worker, dict):
                continue
            thread_id = worker.get("thread_id")
            archive = worker.get("chat_archive_path")
            if (not isinstance(thread_id, str) or not isinstance(archive, str) or
                    thread_id not in archive):
                problems.append(
                    f"Worker {worker_id} archive path does not match its task ID")

    forbidden_paths = []
    for relative in checked:
        path = root / relative
        if path.suffix.lower() not in {
                ".md", ".txt", ".json", ".py", ".zsh", ""}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            continue
        mac_home_pattern = "/" + r"Users/[^/\s]+/"
        if re.search(mac_home_pattern, text):
            forbidden_paths.append(relative)
    if forbidden_paths:
        problems.append("owner-specific absolute paths: " + ", ".join(forbidden_paths))

    body = {
        "schema_version": 1,
        "status": "PASS" if not problems else "FAIL",
        "package": manifest.get("package"),
        "version": manifest.get("version"),
        "root": str(root),
        "files_checked": len(checked),
        "manifest_sha256": _sha256(manifest_path.read_bytes()),
        "python": platform.python_version(),
        "platform": platform.system(),
        "placeholders_present": placeholders_present,
        "deployment_ready": not placeholders_present and not problems,
        "deployment_mutable_files": sorted(mutable_files),
        "changed_mutable_files": sorted(changed_mutable_files),
        "problems": problems,
    }
    body["report_sha256"] = _sha256(json.dumps(
        body, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8"))
    return body


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sue-portable-doctor")
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument("--require-deployed-ids", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = inspect_portable_root(
            args.root, require_deployed_ids=args.require_deployed_ids)
    except PortableDoctorError as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
