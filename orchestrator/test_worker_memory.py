import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from .worker_memory import (
    WorkerMemoryError,
    archive_worker_chat,
    checkpoint_worker_memory,
    claim_worker,
    refresh_worker_memory,
)


class WorkerMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = self.root / "orchestrator" / "visible_modes.json"
        self.config.parent.mkdir()
        self.memory = self.root / ".ai/projects/SUE-AGENT-LAB-2026/workers"
        base = json.loads(
            (Path(__file__).with_name("visible_modes.json")).read_text(
                encoding="utf-8"))
        for worker_id, worker in base["workers"].items():
            worker["thread_id"] = f"thread-{worker_id.lower()}-old"
            worker["generation"] = 1
            worker["previous_thread_ids"] = []
            worker["takeover_history"] = []
            worker["chat_archive_path"] = str(
                self.memory / worker_id / "chats" /
                f"codex-thread-{worker_id.lower()}-old.md")
        self.config.write_text(json.dumps(base), encoding="utf-8")
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.now = datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.tempdir.cleanup()

    def _archive(self, worker_id="A"):
        config = json.loads(self.config.read_text())
        path = Path(config["workers"][worker_id]["chat_archive_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("archived visible chat\n", encoding="utf-8")
        return path

    def test_refresh_writes_independent_memory_and_bootstrap(self):
        snapshots = refresh_worker_memory(self.config, self.memory, ["A", "B"])
        self.assertEqual([item["worker_id"] for item in snapshots], ["A", "B"])
        state = json.loads((self.memory / "A/WORKER_STATE.json").read_text())
        self.assertEqual(state["active_thread_id"], "thread-a-old")
        self.assertEqual(state["shared_memory_access"], "READ_ONLY")
        self.assertIn("Worker A", (self.memory / "A/BOOTSTRAP.md").read_text())

    def test_archive_refresh_uses_current_thread_and_owned_path(self):
        extractor = self.root / "extract.zsh"
        extractor.write_text("placeholder", encoding="utf-8")

        def fake_run(command, **kwargs):
            Path(command[3]).parent.mkdir(parents=True, exist_ok=True)
            Path(command[3]).write_text("visible archive\n", encoding="utf-8")
            class Result:
                stdout = "extract_status=updated turns=1 messages=2\n"
            return Result()

        with patch("orchestrator.worker_memory.subprocess.run",
                   side_effect=fake_run) as run:
            result = archive_worker_chat(
                self.config, "A", extractor_path=extractor)
        self.assertEqual(result["thread_id"], "thread-a-old")
        self.assertTrue(Path(result["archive_path"]).is_file())
        self.assertEqual(run.call_args.args[0][2], "thread-a-old")

    def test_checkpoint_archives_then_refreshes_memory(self):
        extractor = self.root / "extract.zsh"
        extractor.write_text("placeholder", encoding="utf-8")

        def fake_run(command, **kwargs):
            Path(command[3]).parent.mkdir(parents=True, exist_ok=True)
            Path(command[3]).write_text("visible archive\n", encoding="utf-8")
            class Result:
                stdout = "extract_status=updated turns=1 messages=2\n"
            return Result()

        with patch("orchestrator.worker_memory.subprocess.run",
                   side_effect=fake_run):
            result = checkpoint_worker_memory(
                self.config, self.memory, ["A"], extractor_path=extractor)
        self.assertEqual(result[0]["archive"]["worker_id"], "A")
        self.assertEqual(result[0]["memory"]["active_thread_id"],
                         "thread-a-old")
        self.assertTrue((self.memory / "A/WORKER_STATE.json").is_file())

    def test_takeover_archives_owner_and_updates_generation(self):
        archive = self._archive("A")
        snapshot = claim_worker(
            self.config, "A", "thread-a-new",
            expected_thread_id="thread-a-old", memory_root=self.memory,
            runs_dir=self.runs, now=self.now)
        self.assertEqual(snapshot["active_thread_id"], "thread-a-new")
        self.assertEqual(snapshot["generation"], 2)
        self.assertEqual(snapshot["previous_thread_ids"], ["thread-a-old"])
        self.assertEqual(snapshot["takeover_history"][0]["archived_chat_path"],
                         str(archive))
        config = json.loads(self.config.read_text())
        self.assertEqual(config["workers"]["A"]["thread_id"], "thread-a-new")

    def test_takeover_requires_existing_old_archive(self):
        with self.assertRaisesRegex(WorkerMemoryError, "must be archived"):
            claim_worker(
                self.config, "A", "thread-a-new",
                expected_thread_id="thread-a-old", memory_root=self.memory,
                runs_dir=self.runs, now=self.now)

    def test_stale_owner_and_duplicate_thread_are_rejected(self):
        self._archive("A")
        with self.assertRaisesRegex(WorkerMemoryError, "owner changed"):
            claim_worker(
                self.config, "A", "thread-a-new",
                expected_thread_id="wrong-owner", memory_root=self.memory,
                runs_dir=self.runs, now=self.now)
        with self.assertRaisesRegex(WorkerMemoryError, "already belongs"):
            claim_worker(
                self.config, "A", "thread-b-old",
                expected_thread_id="thread-a-old", memory_root=self.memory,
                runs_dir=self.runs, now=self.now)

    def test_running_or_unverifiable_ledger_blocks_takeover(self):
        self._archive("A")
        (self.runs / "run.jsonl").write_text("placeholder\n")
        with patch("orchestrator.worker_memory.doctor_ledger", return_value={
            "healthy": True, "status": "HEALTHY", "run_status": "RUNNING",
            "run_id": "run-1",
        }):
            with self.assertRaisesRegex(WorkerMemoryError, "run-1 is active"):
                claim_worker(
                    self.config, "A", "thread-a-new",
                    expected_thread_id="thread-a-old", memory_root=self.memory,
                    runs_dir=self.runs, now=self.now)
        with patch("orchestrator.worker_memory.doctor_ledger", return_value={
            "healthy": False, "status": "CORRUPT",
        }):
            with self.assertRaisesRegex(WorkerMemoryError, "cannot verify"):
                claim_worker(
                    self.config, "A", "thread-a-new",
                    expected_thread_id="thread-a-old", memory_root=self.memory,
                    runs_dir=self.runs, now=self.now)

    def test_terminal_ledger_allows_takeover_and_same_owner_is_idempotent(self):
        self._archive("A")
        (self.runs / "run.jsonl").write_text("placeholder\n")
        with patch("orchestrator.worker_memory.doctor_ledger", return_value={
            "healthy": True, "status": "HEALTHY", "run_status": "DONE",
            "run_id": "done",
        }):
            first = claim_worker(
                self.config, "A", "thread-a-new",
                expected_thread_id="thread-a-old", memory_root=self.memory,
                runs_dir=self.runs, now=self.now)
            second = claim_worker(
                self.config, "A", "thread-a-new",
                expected_thread_id="thread-a-new", memory_root=self.memory,
                runs_dir=self.runs, now=self.now)
        self.assertEqual(first["generation"], 2)
        self.assertEqual(second["generation"], 2)
        self.assertEqual(len(second["takeover_history"]), 1)


if __name__ == "__main__":
    unittest.main()
