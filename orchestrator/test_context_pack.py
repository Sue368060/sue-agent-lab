from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from .context_pack import (ContextPackError, assert_done_verified,
                           assert_ready_for_final, build_context_bundle,
                           write_context_bundle)
from .short_context_contract import freeze_contract
from .visible_ledger import (complete_turn, finalize_run, read_run,
                             reserve_turn, start_run)
from .visible_modes import DEFAULT_CONFIG, resolve_mode


OWNER = "brain-context-test"
EPOCH = 4
TURN_A = "00000000-0000-4000-8000-0000000000a1"
TURN_B = "00000000-0000-4000-8000-0000000000b1"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _candidate(worker_id: str) -> str:
    return "".join(
        f"Worker {worker_id} 的第 {index} 条独立结果包含日期、预算、风险、执行步骤和可核对来源。"
        for index in range(1, 12)
    )


def _final_artifact(outputs: dict[str, str]) -> str:
    sections = {
        "推荐结论": "选择满足硬约束且证据完整的方案，并说明为什么不采用其他分支。" +
                  "每个关键选择都要能回到用户目标和冻结条件。",
        "详细方案": "按顺序列出时间、负责人、输入、输出和失败处理。\n" +
                "\n".join(outputs.values()),
        "预算与资源": "统一计算模型回合、时间、文件和人工检查成本，并预留失败后的安全余量。",
        "备选方案": "主要条件不成立时缩小范围，保留核心成果，不自动增加模型或额外审查。每个替代分支都要写清触发条件和停止条件。",
        "风险与未知项": "未确认事实逐条列出来源和验证动作，不把推断写成已经验证的事实。高影响未知项必须在最终交付前明确处理状态。",
        "下一步行动": "先运行机械检查，再保存账本证据，只有通过最终门禁后才对用户宣布完成。失败时记录原因并从最近阶段重新验证。",
    }
    sections["预算与资源"] += "".join(
        f"资源项{index}记录上限、实际值和负责人。" for index in range(1, 8))
    return "# 最终方案\n\n" + "\n\n".join(
        f"## {heading}\n\n{body}" for heading, body in sections.items())


class ContextPackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = self.root / "run.jsonl"
        config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        self.plan = resolve_mode(config, "small")
        self.contract = freeze_contract(
            run_id="SC2-TEST", source_request="Build a bounded, auditable answer.",
            plan=self.plan, task_plan={"A": "analyze route", "B": "analyze budget"},
            task_plan_source="leader",
            hard_constraints=[{"id": "HC1", "value": "no external action",
                               "source": "explicit_user"}],
            success_criteria={
                "machine_checks": [{"id": "MC1", "description": "hashes match",
                                    "checker": "sha256", "required": True}],
                "semantic_goals": [],
            }, master_owner=OWNER, master_epoch=EPOCH, max_leader_turns=3)
        start_run(self.ledger, self.contract, at=1, owner_id=OWNER,
                  master_epoch=EPOCH, expected_revision=-1)
        self.outputs = {"A": _candidate("A"), "B": _candidate("B")}
        self.manifest = {}
        for worker_id, turn_id in (("A", TURN_A), ("B", TURN_B)):
            state = read_run(self.ledger)
            reserved = reserve_turn(
                self.ledger, "SC2-TEST", worker_id, "draft", 100, at=2,
                owner_id=OWNER, master_epoch=EPOCH,
                expected_revision=state["revision"])
            text = self.outputs[worker_id]
            candidate_id = f"{worker_id}1"
            artifact = self.root / f"{candidate_id}.md"
            artifact.write_text(text, encoding="utf-8")
            completed = complete_turn(
                self.ledger, "SC2-TEST", reserved["attempt_id"],
                next(item["thread_id"] for item in self.plan["workers"]
                     if item["worker_id"] == worker_id),
                turn_id, _sha(text), candidate_id=candidate_id,
                candidate_version=1, at=3, owner_id=OWNER,
                master_epoch=EPOCH, expected_revision=reserved["revision"])
            self.assertEqual(completed["type"], "turn_completed")
            self.manifest[candidate_id] = {"path": artifact.name, "sha256": _sha(text)}

    def tearDown(self):
        self.temp.cleanup()

    def build(self, manifest=None):
        return build_context_bundle(
            ledger_path=self.ledger, artifact_root=self.root,
            artifact_manifest=self.manifest if manifest is None else manifest)

    def test_identical_state_produces_identical_bytes(self):
        first = self.build()
        second = self.build()
        self.assertEqual(first["context_pack_bytes"], second["context_pack_bytes"])
        self.assertEqual(first["acceptance_snapshot_bytes"],
                         second["acceptance_snapshot_bytes"])
        self.assertEqual(first["snapshot"]["eligibility"], "READY_FOR_FINAL")
        assert_ready_for_final(first["snapshot"])

    def test_written_files_are_byte_stable(self):
        one = self.root / "one"
        two = self.root / "two"
        first = write_context_bundle(
            ledger_path=self.ledger, artifact_root=self.root,
            artifact_manifest=self.manifest, output_dir=one)
        second = write_context_bundle(
            ledger_path=self.ledger, artifact_root=self.root,
            artifact_manifest=self.manifest, output_dir=two)
        self.assertEqual((one / "context_pack.md").read_bytes(),
                         (two / "context_pack.md").read_bytes())
        self.assertEqual((one / "acceptance_snapshot.json").read_bytes(),
                         (two / "acceptance_snapshot.json").read_bytes())
        self.assertEqual(first, second)

    def test_missing_or_changed_artifact_refuses_final(self):
        missing = dict(self.manifest)
        missing.pop("B1")
        snapshot = self.build(missing)["snapshot"]
        self.assertEqual(snapshot["eligibility"], "REFUSE_DONE")
        self.assertEqual(snapshot["evidence_errors"][0]["code"], "MISSING_ARTIFACT")
        with self.assertRaises(ContextPackError):
            assert_ready_for_final(snapshot)

        changed = dict(self.manifest)
        changed["B1"] = dict(changed["B1"], sha256="0" * 64)
        snapshot = self.build(changed)["snapshot"]
        self.assertEqual(snapshot["evidence_errors"][0]["code"],
                         "ARTIFACT_HASH_MISMATCH")

    def test_unbound_or_unsafe_artifact_is_rejected(self):
        extra = dict(self.manifest)
        extra["X1"] = {"path": "X1.md", "sha256": "0" * 64}
        with self.assertRaisesRegex(ContextPackError, "unbound"):
            self.build(extra)
        unsafe = dict(self.manifest)
        unsafe["A1"] = {"path": "../outside.md", "sha256": self.manifest["A1"]["sha256"]}
        with self.assertRaisesRegex(ContextPackError, "below artifact root"):
            self.build(unsafe)

    def test_finalized_done_is_bound_to_delivery_receipt_and_ledger_head(self):
        before = self.build()
        artifact = _final_artifact(self.outputs)
        source_claim = self.outputs["A"].split("。", 1)[0] + "。"
        state = read_run(self.ledger)
        finalize_run(
            self.ledger, "SC2-TEST", "main_task", "DONE", _sha(artifact),
            ["A1", "B1"], at=4, owner_id=OWNER, master_epoch=EPOCH,
            expected_revision=state["revision"], final_artifact=artifact,
            final_message="完整结果如下：\n" + artifact,
            worker_outputs=self.outputs,
            claims=[{"id": "CL1", "text": source_claim,
                     "support_type": "source", "source_worker_ids": ["A"],
                     "evidence_quotes": {"A": source_claim}}],
            machine_results={"MC1": {"status": "PASS",
                                      "evidence": "hashes match"}},
            semantic_results={}, final_revision=0)
        after = self.build()
        self.assertNotEqual(before["context_pack_bytes"], after["context_pack_bytes"])
        self.assertNotEqual(before["snapshot"]["ledger_head_sha256"],
                            after["snapshot"]["ledger_head_sha256"])
        self.assertEqual(after["snapshot"]["eligibility"], "DONE_VERIFIED")
        self.assertEqual(after["snapshot"]["final_gate_receipt"]["status"], "PASS")
        assert_done_verified(after["snapshot"])

    def test_tampered_frozen_constraints_stop_generation(self):
        events = self.ledger.read_text(encoding="utf-8").splitlines()
        first = json.loads(events[0])
        first["contract"].pop("hard_constraints")
        body = {key: value for key, value in first["contract"].items()
                if key != "contract_sha256"}
        first["contract"]["contract_sha256"] = hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")).hexdigest()
        first["contract_sha256"] = first["contract"]["contract_sha256"]
        event_body = {key: value for key, value in first.items() if key != "event_hash"}
        first["event_hash"] = hashlib.sha256(
            json.dumps(event_body, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")).hexdigest()
        damaged = self.root / "damaged.jsonl"
        damaged.write_text(json.dumps(first, ensure_ascii=False, sort_keys=True) + "\n",
                           encoding="utf-8")
        with self.assertRaisesRegex(ContextPackError, "cannot be replayed"):
            build_context_bundle(ledger_path=damaged, artifact_root=self.root,
                                 artifact_manifest={})


if __name__ == "__main__":
    unittest.main()
