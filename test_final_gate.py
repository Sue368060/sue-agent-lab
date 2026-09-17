from __future__ import annotations

from copy import deepcopy
import json
import unittest

from .final_gate import FinalGateError, validate_final_gate
from .short_context_contract import freeze_contract
from .visible_modes import DEFAULT_CONFIG, resolve_mode


def _worker_text(worker: str) -> str:
    lead = (f"Worker {worker} 建议在2026年9月24日执行路线核验，预算上限为15000元，"
            f"并由负责人{worker}保存票价证据。")
    return lead + "补充说明用于覆盖交通、住宿、风险、替代方案和下一步行动。" * 8


def _artifact(outputs: dict[str, str], claim: str) -> str:
    sections = {
        "推荐结论": claim + " 综合后优先选择可验证、可回退的执行路径。" * 4,
        "详细方案": "\n".join(outputs.values()),
        "预算与资源": "预算、时间、人员和模型回合均按冻结上限执行，并逐项保存证据。" * 5,
        "备选方案": "若关键条件未满足，则缩小范围并保留最低成果，随后明确标为部分完成。" * 5,
        "风险与未知项": "价格、时刻与外部状态均视为未知项，使用前核验并记录来源和时间。" * 5,
        "下一步行动": "先运行机械检查，再核验语义目标和事实证据，全部通过后才提交。" * 5,
    }
    return "# 最终成果\n\n" + "\n\n".join(
        f"## {heading}\n\n{body}" for heading, body in sections.items())


class FinalGateTests(unittest.TestCase):
    def setUp(self):
        config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        plan = resolve_mode(config, "small")
        self.contract = freeze_contract(
            run_id="FG-TEST", source_request="produce an evidence-bound answer",
            plan=plan, task_plan={"A": "route", "B": "budget"},
            task_plan_source="leader",
            hard_constraints=[{"id": "HC1", "value": "offline",
                               "source": "explicit_user"}],
            success_criteria={
                "machine_checks": [{"id": "MC1", "description": "hashes",
                                    "checker": "sha256", "required": True}],
                "semantic_goals": [{"id": "SG1", "description": "useful",
                                    "required": True, "verifier": "leader"}],
            }, master_owner="brain", max_leader_turns=3)
        self.outputs = {worker: _worker_text(worker) for worker in ("A", "B")}
        self.claim = self.outputs["A"].split("。", 1)[0] + "。"

    def validate(self, **overrides):
        artifact = overrides.pop("final_artifact", _artifact(self.outputs, self.claim))
        values = {
            "contract": self.contract, "ledger_head_sha256": "1" * 64,
            "final_artifact": artifact, "final_message": "结果如下\n" + artifact,
            "worker_outputs": self.outputs, "required_worker_ids": ["A", "B"],
            "claims": [{"id": "CL1", "text": self.claim,
                        "support_type": "source", "source_worker_ids": ["A"],
                        "evidence_quotes": {"A": self.claim}}],
            "machine_results": {"MC1": {"status": "PASS", "evidence": "ok"}},
            "semantic_results": {"SG1": {"status": "PASS", "verifier": "leader",
                                             "evidence": "reviewed"}},
            "final_revision": 0,
        }
        values.update(overrides)
        return validate_final_gate(**values)

    def test_valid_source_claim_passes(self):
        receipt = self.validate()
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["claim_ids"], ["CL1"])

    def test_invented_value_or_quote_is_rejected(self):
        invented = "Worker A 建议在2026年9月30日执行路线核验，预算上限为99999元。"
        artifact = _artifact(self.outputs, invented)
        claims = [{"id": "CL1", "text": invented, "support_type": "source",
                   "source_worker_ids": ["A"],
                   "evidence_quotes": {"A": self.claim}}]
        with self.assertRaisesRegex(FinalGateError, "key value"):
            self.validate(final_artifact=artifact, claims=claims)
        bad_quote = deepcopy(claims)
        bad_quote[0]["text"] = self.claim
        bad_quote[0]["evidence_quotes"] = {"A": "这段伪造引文长度足够但不在原始输出中出现"}
        with self.assertRaisesRegex(FinalGateError, "quote"):
            self.validate(claims=bad_quote)

    def test_synthesis_requires_two_sources_and_rationale(self):
        synthesis = "综合两份证据后，应优先选择可验证、可回退的执行路径。"
        artifact = _artifact(self.outputs, synthesis)
        claim = {"id": "CL2", "text": synthesis, "support_type": "synthesis",
                 "source_worker_ids": ["A"], "evidence_quotes": {"A": self.claim},
                 "rationale": ("路线和预算需要联合判断，因此采用能够核验且允许回退的路径；"
                               "两份来源分别提供日期、额度与执行责任，组合后才能形成完整结论。")}
        with self.assertRaisesRegex(FinalGateError, "multi-source"):
            self.validate(final_artifact=artifact, claims=[claim])
        claim["source_worker_ids"] = ["A", "B"]
        claim["evidence_quotes"]["B"] = self.outputs["B"].split("。", 1)[0] + "。"
        self.assertEqual(self.validate(final_artifact=artifact, claims=[claim])["status"],
                         "PASS")

    def test_required_machine_and_semantic_evidence_are_enforced(self):
        with self.assertRaisesRegex(FinalGateError, "machine-check"):
            self.validate(machine_results={})
        with self.assertRaisesRegex(FinalGateError, "semantic goal"):
            self.validate(semantic_results={"SG1": {"status": "PASS",
                                                     "verifier": "worker",
                                                     "evidence": "ok"}})

    def test_final_revision_budget_and_claim_presence_are_enforced(self):
        self.assertEqual(self.validate(final_revision=1)["final_revision"], 1)
        with self.assertRaisesRegex(FinalGateError, "revision budget"):
            self.validate(final_revision=2)
        artifact = _artifact(self.outputs, "没有包含已绑定主张的其他结论。").replace(
            self.claim, "已移除原始主张，但仍保留其他足够长的候选内容。")
        with self.assertRaisesRegex(FinalGateError, "claim identity"):
            self.validate(final_artifact=artifact)


if __name__ == "__main__":
    unittest.main()
