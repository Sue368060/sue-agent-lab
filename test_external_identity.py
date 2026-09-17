from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from .external_identity import (ExternalIdentityError, mint_external_identity,
                                resolve_external_identity)


class ExternalIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = Path(self.temp.name) / "external-identities.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_mint_is_stable_and_does_not_store_raw_locator(self):
        generated = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        first = mint_external_identity(
            self.registry, "workbuddy", "window title plus local fingerprint",
            label="Kimi review", now=datetime(2026, 9, 17, tzinfo=timezone.utc),
            generated_uuid=generated)
        second = mint_external_identity(
            self.registry, "workbuddy", "window title plus local fingerprint")
        self.assertEqual(first, second)
        self.assertEqual(first["external_id"],
                         "workbuddy-aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa")
        self.assertNotIn("window title", self.registry.read_text())
        self.assertEqual(resolve_external_identity(
            self.registry, "workbuddy", "window title plus local fingerprint"),
            first)

    def test_distinct_provider_or_locator_gets_distinct_identity(self):
        a = mint_external_identity(self.registry, "claude", "session-1")
        b = mint_external_identity(self.registry, "workbuddy", "session-1")
        c = mint_external_identity(self.registry, "claude", "session-2")
        self.assertEqual(len({a["external_id"], b["external_id"],
                              c["external_id"]}), 3)
        registry = json.loads(self.registry.read_text())
        self.assertEqual(len(registry["identities"]), 3)

    def test_invalid_provider_and_corrupt_registry_fail_closed(self):
        with self.assertRaises(ExternalIdentityError):
            mint_external_identity(self.registry, "Bad Provider!", "x")
        self.registry.write_text("not-json")
        with self.assertRaises(ExternalIdentityError):
            mint_external_identity(self.registry, "claude", "x")

    def test_resolve_validates_lookup_and_registry_schema(self):
        with self.assertRaises(ExternalIdentityError):
            resolve_external_identity(self.registry, "Bad Provider!", "x")
        self.registry.write_text(json.dumps({"schema_version": 1,
                                             "identities": []}))
        with self.assertRaises(ExternalIdentityError):
            resolve_external_identity(self.registry, "claude", "x")

    def test_corrupt_identity_entry_fails_closed(self):
        item = mint_external_identity(self.registry, "claude", "session")
        registry = json.loads(self.registry.read_text())
        key = next(iter(registry["identities"]))
        registry["identities"][key]["external_id"] = "forged"
        self.registry.write_text(json.dumps(registry))
        with self.assertRaises(ExternalIdentityError):
            resolve_external_identity(self.registry, "claude", "session")

        self.registry.write_text("[]")
        with self.assertRaises(ExternalIdentityError):
            resolve_external_identity(self.registry, "claude", "session")


if __name__ == "__main__":
    unittest.main()
