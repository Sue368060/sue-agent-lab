from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from .context_pack import (assert_done_verified, assert_ready_for_final,
                           build_context_bundle)
from .programmatic_main import choose_next_action
from .short_context_contract import freeze_contract
from .visible_ledger import (complete_turn, doctor_ledger, finalize_run, read_run,
                             reserve_turn, start_run)
from .visible_modes import DEFAULT_CONFIG, resolve_mode


OWNER = "v24-e2e-brain"
EPOCH = 1


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _worker_output(worker: str) -> str:
    topics = ("目标", "限制", "方案", "时间", "预算", "风险", "备选", "行动")
    return "".join(
        f"Worker {worker} 的{topic}分析第{index}项给出可核验事实、选择理由和执行证据，"
        f"并说明条件变化时如何调整而不突破冻结要求。"
        for index, topic in enumerate(topics, 1)
    )


def _final_artifact(outputs: dict[str, str]) -> str:
    sections = {
        "推荐结论": (
            outputs["A"].split("。", 1)[0] + "。综合两份候选后，优先采用步骤明确、"
            "证据可以复核且失败后能够安全停止的方案，并保留用户最关心的完整结果。"
        ),
        "详细方案": "\n\n".join(outputs.values()),
        "预算与资源": (
            "本次只使用两个 Worker 初稿，不增加固定互审、自动重试或外部模型检查。"
            "每次调用先预约并记录截止时间，总耗时达到冻结目标后不再启动新调用。"
        ),
        "备选方案": (
            "若一份候选失败，系统只在剩余材料仍能形成安全成果时交付 PARTIAL；"
            "若最低成果不足则 ABORT，并保存失败原因供下一次独立运行参考。"
        ),
        "风险与未知项": (
            "平台返回正文、实际模型身份证据和实时容量都可能不可用。"
            "这些未知项必须明确披露，不能用请求参数或任务完成标志代替实际证据。"
        ),
        "下一步行动": (
            "先核对账本头、候选文件哈希、机械标准和语义标准，再运行最终门禁。"
            "全部凭证通过后写入 DONE，最后才把完整成果发送到主聊天。"
        ),
    }
    return "# v2.4 完整演练成果\n\n" + "\n\n".join(
        f"## {heading}\n\n{body}" for heading, body in sections.items())


class VisibleV24EndToEndTests(unittest.TestCase):
    def test_two_worker_run_reaches_evidence_verified_done(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = root / "run.jsonl"
            config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
            plan = resolve_mode(config, "small")
            contract = freeze_contract(
                run_id="V24-E2E", source_request="完成一次无模型双 Worker 全流程演练",
                plan=plan, task_plan={"A": "提出方案", "B": "独立补充方案"},
                task_plan_source="leader",
                hard_constraints=[{"id": "HC1", "value": "不调用真实模型",
                                   "source": "explicit_user"}],
                success_criteria={
                    "machine_checks": [{"id": "MC1", "description": "账本和哈希有效",
                                        "checker": "ledger+sha256", "required": True}],
                    "semantic_goals": [{"id": "SG1", "description": "结果完整可用",
                                        "required": True, "verifier": "main_task"}],
                }, master_owner=OWNER, master_epoch=EPOCH, max_leader_turns=3)
            start_run(ledger, contract, at=100, owner_id=OWNER,
                      master_epoch=EPOCH, expected_revision=-1)

            first = choose_next_action(ledger_path=ledger, now=101)
            self.assertEqual(first["type"], "DISPATCH_DRAFT_BATCH")
            self.assertEqual(first["details"]["worker_ids"], ["A", "B"])

            outputs = {worker: _worker_output(worker) for worker in ("A", "B")}
            manifest = {}
            for index, worker in enumerate(("A", "B"), 1):
                state = read_run(ledger)
                reserved = reserve_turn(
                    ledger, "V24-E2E", worker, "draft", 400, at=101,
                    owner_id=OWNER, master_epoch=EPOCH,
                    expected_revision=state["revision"])
                candidate_id = f"{worker}1"
                text = outputs[worker]
                artifact_path = root / f"{candidate_id}.md"
                artifact_path.write_text(text, encoding="utf-8")
                thread_id = next(item["thread_id"] for item in plan["workers"]
                                 if item["worker_id"] == worker)
                complete_turn(
                    ledger, "V24-E2E", reserved["attempt_id"], thread_id,
                    f"00000000-0000-4000-8000-{index:012d}", _sha(text),
                    candidate_id=candidate_id, candidate_version=1, at=102,
                    owner_id=OWNER, master_epoch=EPOCH,
                    expected_revision=reserved["revision"])
                manifest[candidate_id] = {"path": artifact_path.name,
                                          "sha256": _sha(text)}

            synthesize = choose_next_action(ledger_path=ledger, now=103)
            self.assertEqual(synthesize["type"], "SYNTHESIZE")
            self.assertEqual(synthesize["details"]["basis_candidate_ids"], ["A1", "B1"])
            before = build_context_bundle(
                ledger_path=ledger, artifact_root=root, artifact_manifest=manifest)
            self.assertEqual(before["snapshot"]["eligibility"], "READY_FOR_FINAL")
            assert_ready_for_final(before["snapshot"])

            artifact = _final_artifact(outputs)
            claim = outputs["A"].split("。", 1)[0] + "。"
            state = read_run(ledger)
            finalized = finalize_run(
                ledger, "V24-E2E", "main_task", "DONE", _sha(artifact),
                ["A1", "B1"], at=104, owner_id=OWNER, master_epoch=EPOCH,
                expected_revision=state["revision"], final_artifact=artifact,
                final_message="完整结果如下：\n" + artifact,
                worker_outputs=outputs,
                claims=[{"id": "CL1", "text": claim,
                         "support_type": "source", "source_worker_ids": ["A"],
                         "evidence_quotes": {"A": claim}}],
                machine_results={"MC1": {"status": "PASS",
                                          "evidence": "账本重放和哈希核验通过"}},
                semantic_results={"SG1": {"status": "PASS",
                                           "verifier": "main_task",
                                           "evidence": "六部分成果完整"}},
                final_revision=0)
            self.assertEqual(finalized["final_gate_receipt"]["status"], "PASS")
            self.assertEqual(choose_next_action(ledger_path=ledger, now=105)["type"],
                             "STOP")

            after = build_context_bundle(
                ledger_path=ledger, artifact_root=root, artifact_manifest=manifest)
            self.assertEqual(after["snapshot"]["eligibility"], "DONE_VERIFIED")
            assert_done_verified(after["snapshot"])
            diagnosis = doctor_ledger(ledger)
            self.assertTrue(diagnosis["healthy"])
            self.assertEqual(diagnosis["run_status"], "DONE")


if __name__ == "__main__":
    unittest.main()
