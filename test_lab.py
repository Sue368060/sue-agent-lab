import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from . import lab as lab_module
from .backends import FakeBackend, BackendError
from .lab import Lab, select_models


class RecordingFake(FakeBackend):
    def __init__(self, fail_once=None):
        super().__init__(fail_once)
        self.prompts = []
        self.invalid_once = set()

    def generate(self, phase, actor, prompt, model, timeout=120):
        self.prompts.append((phase, actor, prompt))
        if (phase, actor) in self.invalid_once:
            self.invalid_once.remove((phase, actor))
            return {"content": {"oops": True}, "usage": {"totalTokens": 0}, "model": model}
        return super().generate(phase, actor, prompt, model, timeout)


class LabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backend = RecordingFake()
        self.lab = Lab(self.backend, self.temp.name)
        self.addCleanup(self.lab.close)

    def test_full_loop_sealed_drafts_and_cross_review(self):
        job, created = self.lab.submit("Compare evidence quality", 3)
        self.assertTrue(created)
        result = self.lab.run(job)
        self.assertEqual(result["status"], "completed")
        rows = list(self.lab.conn.execute("SELECT stage,actor,target FROM steps WHERE job_id=?", (job,)))
        self.assertEqual(len(rows), 16)  # 15 model turns + deterministic verification
        reviews = [(r["actor"], r["target"]) for r in rows if r["stage"] == "review"]
        self.assertEqual(len(reviews), 6)
        for target in ("worker-1", "worker-2", "worker-3"):
            mine = [actor for actor, reviewed in reviews if reviewed == target]
            self.assertEqual(len(mine), 2)
            self.assertTrue(any("ordinary" in actor for actor in mine))
            self.assertTrue(any("critical" in actor for actor in mine))
            self.assertTrue(all(not actor.startswith(target + "/") for actor in mine))
        for phase, actor, prompt in self.backend.prompts:
            if phase == "draft":
                self.assertNotIn("independent proposal", prompt)
        before = len(self.backend.calls)
        same, created = self.lab.submit("Compare evidence quality", 3)
        self.assertEqual(same, job)
        self.assertFalse(created)
        self.lab.run(job)
        self.assertEqual(len(self.backend.calls), before)
        self.assertTrue(Path(self.temp.name, job + ".events.jsonl").is_file())
        manifest = json.loads(Path(result["artifact_dir"], "manifest.json").read_text())
        self.assertTrue(manifest["all_match"])
        self.assertTrue(self.lab.result(job)["artifact_integrity"])
        draft_path = next(Path(r["artifact_path"]) for r in self.lab.conn.execute(
            "SELECT artifact_path FROM steps WHERE job_id=? AND stage='draft' LIMIT 1", (job,)
        ))
        draft_path.write_text("tampered", encoding="utf-8")
        self.assertFalse(self.lab.result(job)["artifact_integrity"])

    def test_recovery_after_worker_failure_preserves_siblings(self):
        self.backend.fail_once.add(("draft", "worker-1"))
        self.lab.max_retries = 0
        job, _ = self.lab.submit("Recovery case", 3)
        with self.assertRaises(BackendError):
            self.lab.run(job)
        self.assertEqual(self.lab.result(job)["status"], "failed")
        saved = self.lab.conn.execute(
            "SELECT count(*) FROM steps WHERE job_id=? AND stage='draft'", (job,)
        ).fetchone()[0]
        self.assertEqual(saved, 2)
        before = len([c for c in self.backend.calls if c == ("plan", "leader")])
        self.lab.max_retries = 1  # explicit operator decision to allow one more attempt
        result = self.lab.run(job)
        self.assertEqual(result["status"], "completed")
        after = len([c for c in self.backend.calls if c == ("plan", "leader")])
        self.assertEqual(before, after)

    def test_incremental_memory_version_notice(self):
        original = lab_module.MEMORY
        memory = Path(self.temp.name) / "memory"
        memory.mkdir()
        (memory / "AGENTS.md").write_text("first", encoding="utf-8")
        lab_module.MEMORY = memory
        self.addCleanup(setattr, lab_module, "MEMORY", original)
        job, _ = self.lab.submit("Memory sync case", 3)
        (memory / "AGENTS.md").write_text("second", encoding="utf-8")
        self.lab.run(job)
        changed = [json.loads(r[0]) for r in self.lab.conn.execute(
            "SELECT payload_json FROM events WHERE job_id=? AND kind='memory_changed'", (job,)
        )]
        self.assertEqual(changed[0]["changed_files"], ["AGENTS.md"])
        plan_prompt = next(prompt for phase, _, prompt in self.backend.prompts if phase == "plan")
        self.assertIn("AGENTS.md", plan_prompt)
        self.assertIn("second", plan_prompt)

    def test_model_and_evidence_guards(self):
        catalog = [{"model": "gpt-6-astra"}, {"model": "gpt-5.6-luna"}, {"model": "gpt-5.6-sol"}]
        self.assertEqual(select_models(catalog), {"worker": "gpt-5.6-luna", "leader": "gpt-5.6-sol"})
        with self.assertRaises(BackendError):
            select_models(catalog, leader="gpt-6-astra")
        source = Path(self.temp.name) / "source.txt"
        source.write_text("evidence", encoding="utf-8")
        checks = Lab.verify_claims({"claims": [
            {"text": "local", "source": str(source)},
            {"text": "remote", "source": "https://example.org"},
            {"text": "no source"},
        ]}, [str(source)])
        self.assertEqual([c["status"] for c in checks],
                         ["source-exists", "unverified", "unverified"])

    def test_call_budget_admission(self):
        self.lab.max_calls = 14
        with self.assertRaises(ValueError):
            self.lab.submit("Too expensive", 3)

    def test_five_worker_review_matrix(self):
        job, _ = self.lab.submit("Five worker variant", 5)
        self.assertEqual(self.lab.run(job)["status"], "completed")
        self.assertEqual(self.lab.conn.execute(
            "SELECT count(*) FROM steps WHERE job_id=?", (job,)
        ).fetchone()[0], 24)
        for i in range(1, 6):
            target = "worker-%d" % i
            actors = [r[0] for r in self.lab.conn.execute(
                "SELECT actor FROM steps WHERE job_id=? AND stage='review' AND target=?",
                (job, target),
            )]
            self.assertEqual(len(actors), 2)
            self.assertTrue(all(not actor.startswith(target + "/") for actor in actors))

    def test_invalid_json_shape_retries_boundedly(self):
        self.backend.invalid_once.add(("draft", "worker-2"))
        job, _ = self.lab.submit("Schema retry case", 3)
        self.assertEqual(self.lab.run(job)["status"], "completed")
        attempts = self.lab.conn.execute(
            "SELECT count FROM attempts WHERE job_id=? AND stage='draft' AND actor='worker-2'",
            (job,),
        ).fetchone()[0]
        self.assertEqual(attempts, 2)


if __name__ == "__main__":
    unittest.main()
