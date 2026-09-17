"""Stable local IDs for non-Codex conversations that lack a usable native ID."""

from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid


PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
EXTERNAL_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}-[0-9a-f]{32}$")


class ExternalIdentityError(ValueError):
    pass


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _key(provider: str, locator: str) -> str:
    return hashlib.sha256(f"{provider}\0{locator}".encode("utf-8")).hexdigest()


def _normalize_lookup(provider: str, locator: str) -> tuple[str, str]:
    if not isinstance(provider, str) or not isinstance(locator, str):
        raise ExternalIdentityError("provider or locator is invalid")
    provider = provider.strip().lower()
    locator = locator.strip()
    if not PROVIDER_RE.fullmatch(provider) or not locator:
        raise ExternalIdentityError("provider or locator is invalid")
    return provider, locator


def _load_registry(path: Path) -> dict:
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExternalIdentityError(f"registry cannot be read: {exc}") from exc
    identities = registry.get("identities") if isinstance(registry, dict) else None
    if (not isinstance(registry, dict) or registry.get("schema_version") != 1 or
            not isinstance(identities, dict)):
        raise ExternalIdentityError("registry schema is invalid")
    for key, value in identities.items():
        if (not isinstance(key, str) or not HASH_RE.fullmatch(key) or
                not isinstance(value, dict) or value.get("locator_sha256") != key or
                not isinstance(value.get("provider"), str) or
                not PROVIDER_RE.fullmatch(value["provider"]) or
                not isinstance(value.get("external_id"), str) or
                not EXTERNAL_ID_RE.fullmatch(value["external_id"]) or
                not value["external_id"].startswith(value["provider"] + "-") or
                value.get("label") is not None and
                not isinstance(value.get("label"), str) or
                not isinstance(value.get("created_at"), str)):
            raise ExternalIdentityError("registry identity entry is invalid")
    return registry


def mint_external_identity(registry_path: Path, provider: str, locator: str,
                           *, label: str | None = None,
                           now: datetime | None = None,
                           generated_uuid: uuid.UUID | None = None) -> dict:
    provider, locator = _normalize_lookup(provider, locator)
    if label is not None and not label.strip():
        raise ExternalIdentityError("label cannot be empty")
    path = Path(registry_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if path.exists():
            registry = _load_registry(path)
        else:
            registry = {"schema_version": 1, "identities": {}}
        locator_hash = _key(provider, locator)
        existing = registry["identities"].get(locator_hash)
        if existing is not None:
            return existing
        value = {
            "external_id": f"{provider}-{(generated_uuid or uuid.uuid4()).hex}",
            "provider": provider,
            "locator_sha256": locator_hash,
            "label": label.strip() if label else None,
            "created_at": (now or datetime.now().astimezone()).isoformat(
                timespec="seconds"),
        }
        registry["identities"][locator_hash] = value
        _atomic_json(path, registry)
        return value


def resolve_external_identity(registry_path: Path, provider: str,
                              locator: str) -> dict | None:
    provider, locator = _normalize_lookup(provider, locator)
    path = Path(registry_path).resolve()
    if not path.is_file():
        return None
    registry = _load_registry(path)
    return registry["identities"].get(_key(provider, locator))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sue-external-identity")
    parser.add_argument("--registry", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("mint", "resolve"):
        command = sub.add_parser(name)
        command.add_argument("--provider", required=True)
        command.add_argument("--locator", required=True)
        if name == "mint":
            command.add_argument("--label")
    args = parser.parse_args(argv)
    try:
        if args.command == "mint":
            result = mint_external_identity(
                args.registry, args.provider, args.locator, label=args.label)
        else:
            result = resolve_external_identity(
                args.registry, args.provider, args.locator)
    except ExternalIdentityError as exc:
        parser.error(str(exc))
    print(json.dumps({"status": "FOUND" if result else "NOT_FOUND",
                      "identity": result}, ensure_ascii=False, indent=2))
    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
