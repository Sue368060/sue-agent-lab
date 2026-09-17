from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from .portable_doctor import inspect_portable_root


class PortableDoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "orchestrator").mkdir()
        source = Path(__file__).with_name("visible_modes.json")
        config = json.loads(source.read_text(encoding="utf-8"))
        ids = {
            "A": "11111111-1111-4111-8111-111111111111",
            "B": "22222222-2222-4222-8222-222222222222",
            "C": "33333333-3333-4333-8333-333333333333",
            "D": "44444444-4444-4444-8444-444444444444",
        }
        for worker_id, worker in config["workers"].items():
            worker["thread_id"] = ids[worker_id]
            worker["generation"] = 1
            worker["previous_thread_ids"] = []
            worker["takeover_history"] = []
            worker["chat_archive_path"] = f"workers/{worker_id}/{ids[worker_id]}.md"
        config_path = self.root / "orchestrator/visible_modes.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        raw = config_path.read_bytes()
        manifest = {
            "package": "test", "version": "1", "source_files": 1,
            "deployment_mutable_files": ["orchestrator/visible_modes.json"],
            "files": [{"path": "orchestrator/visible_modes.json",
                       "bytes": len(raw),
                       "sha256": hashlib.sha256(raw).hexdigest()}],
        }
        (self.root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_clean_portable_bundle_passes_but_is_not_deployed(self):
        report = inspect_portable_root(self.root)
        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["placeholders_present"])
        self.assertFalse(report["deployment_ready"])
        self.assertEqual(len(report["report_sha256"]), 64)

    def test_deployment_mode_rejects_placeholders(self):
        report = inspect_portable_root(self.root, require_deployed_ids=True)
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("placeholders", report["problems"][0])

    def test_tamper_and_owner_specific_path_are_reported(self):
        config = self.root / "orchestrator/visible_modes.json"
        owner_path = "/Users/" + "example-owner/private"
        config.write_text(config.read_text() + f"\n{owner_path}\n", encoding="utf-8")
        report = inspect_portable_root(self.root)
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("hash mismatch" in item for item in report["problems"]))
        self.assertTrue(any("owner-specific" in item for item in report["problems"]))

    def test_deployed_roster_allows_only_declared_config_change(self):
        config_path = self.root / "orchestrator/visible_modes.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        for index, (worker_id, worker) in enumerate(config["workers"].items(), 10):
            thread_id = f"aaaaaaaa-aaaa-4aaa-8aaa-{index:012d}"
            worker["thread_id"] = thread_id
            worker["chat_archive_path"] = f"workers/{worker_id}/chats/codex-{thread_id}.md"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        normal = inspect_portable_root(self.root)
        deployed = inspect_portable_root(self.root, require_deployed_ids=True)
        self.assertEqual(normal["status"], "FAIL")
        self.assertEqual(deployed["status"], "PASS")
        self.assertTrue(deployed["deployment_ready"])
        self.assertEqual(deployed["changed_mutable_files"],
                         ["orchestrator/visible_modes.json"])

    def test_manifest_rejects_traversal_and_duplicate_paths(self):
        manifest_path = self.root / "MANIFEST.json"
        manifest = json.loads(manifest_path.read_text())
        entry = dict(manifest["files"][0])
        entry["path"] = "../outside.txt"
        manifest["files"].append(entry)
        manifest["source_files"] = 2
        manifest_path.write_text(json.dumps(manifest))
        report = inspect_portable_root(self.root)
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("unsafe manifest path" in item
                            for item in report["problems"]))

        manifest["files"][1]["path"] = manifest["files"][0]["path"]
        manifest_path.write_text(json.dumps(manifest))
        report = inspect_portable_root(self.root)
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("duplicate manifest path" in item
                            for item in report["problems"]))


if __name__ == "__main__":
    unittest.main()
