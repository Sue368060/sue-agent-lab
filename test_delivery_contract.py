import json
import unittest

from .delivery_contract import DeliveryError, validate_delivery
from .visible_modes import DEFAULT_CONFIG, resolve_mode


class DeliveryContractTests(unittest.TestCase):
    def setUp(self):
        config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        self.plan = resolve_mode(config, "small")
        a = ("路线调研确认夜间卧铺优先、白天高铁次之，并建议把拥挤日期留在上海休整。"
             "交通段需要逐项核对真实车次、开售日期和换乘余量，不能把不存在的动车当作默认选项。"
             "每天最多安排两个相邻区域，跨城后保留至少半天缓冲，避免为了打卡连续赶路。"
             "夜间移动必须同时满足到达时刻安全、卧铺真实开行和第二天行程不过载三个条件。"
             "如果首选交通售罄，应按预先写好的高铁、普通卧铺和缩短行程顺序降级。"
             "最终表格还要标明每一段由谁确认以及最迟确认时间。")
        b = ("预算分析采用交通、住宿、餐饮、门票和机动金五类统一口径，所有分支都用同一张总表比较。"
             "住宿按实际夜数计算，上海借住与外地酒店分开标注，黄金周期间使用价格区间而非单点价格。"
             "若总额接近上限，先删除远距离支线，再减少高价住宿，不压缩必要的返程安全余量。"
             "每个价格区间都要注明人数和是否包含机动金，避免出现多个互相冲突的总计。"
             "最终付款前还要重新检查退改规则、酒店取消期限和返程当天的接驳成本。"
             "超出预算时必须记录被删除项目和调整后的新总额。")
        self.worker_outputs = {"A": a, "B": b}
        section_text = {
            "推荐结论": "采用上海作为稳定基地，先完成低峰日周边活动，再安排一段跨城旅行。" + a,
            "详细方案": "按日期列出出发、抵达、住宿和当天重点，每次换城都写清替代交通。" + b,
            "预算与资源": "预算表统一计算两个人的交通、酒店、餐饮、门票与百分之十五机动金，避免重复加总。所有价格必须注明人数、夜数、计算日期和是否可退。",
            "备选方案": "如果车票售罄就缩短跨城段；如果天气恶劣就保留上海与近郊活动，并把远程目的地后移。每个分支都要保留安全返程窗口。",
            "风险与未知项": "尚未确认的调休日、真实车次、酒店价格和同伴请假情况必须在购票前逐项确认。没有可靠来源的事实只能标成待核实，不能直接下结论。",
            "下一步行动": "先确认双方假期与住宿夜数，再查车次和价格，最后按统一预算表决定是否保留支线。购买之前再次检查天气、退改政策和返程余量。",
        }
        self.final_artifact = "# 可执行方案\n\n" + "\n\n".join(
            f"## {heading}\n\n{body}" for heading, body in section_text.items())
        self.final_artifact += "\n\n补充说明：" + "；".join(
            f"检查项目{i}对应日期、价格、余量和负责人" for i in range(1, 18))

    def validate(self, **overrides):
        values = {
            "plan": self.plan,
            "final_artifact": self.final_artifact,
            "final_message": "结果如下\n" + self.final_artifact + "\n运行记录",
            "worker_outputs": self.worker_outputs,
        }
        values.update(overrides)
        return validate_delivery(**values)

    def test_complete_inline_delivery_passes(self):
        result = self.validate()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["validator_version"], 3)
        self.assertEqual(result["covered_workers"], ["A", "B"])

    def test_link_only_or_summary_only_delivery_is_rejected(self):
        with self.assertRaises(DeliveryError):
            self.validate(final_message="已完成，请打开文件")

    def test_worker_contribution_cannot_be_silently_dropped(self):
        changed = dict(self.worker_outputs)
        changed["B"] = ("这是一份长度足够但内容完全无关的独立稿件。" * 20)
        with self.assertRaises(DeliveryError):
            self.validate(worker_outputs=changed)

    def test_overcompressed_artifact_is_rejected(self):
        short = "# 方案\n## 推荐结论\n短\n## 详细方案\n短"
        with self.assertRaises(DeliveryError):
            self.validate(final_artifact=short, final_message=short)

    def test_repeated_filler_and_thin_sections_are_rejected(self):
        headings = ["推荐结论", "详细方案", "预算与资源", "备选方案", "风险与未知项", "下一步行动"]
        filler = "\n".join(f"## {heading}\n1" for heading in headings) + "\n" + "填充" * 600
        with self.assertRaises(DeliveryError):
            self.validate(final_artifact=filler, final_message=filler)

    def test_plain_text_section_labels_are_not_accepted_as_structure(self):
        flattened = self.final_artifact.replace("## ", "")
        with self.assertRaises(DeliveryError):
            self.validate(final_artifact=flattened, final_message=flattened)

    def test_same_realistic_paragraph_cannot_fake_all_sections(self):
        headings = ["推荐结论", "详细方案", "预算与资源", "备选方案", "风险与未知项", "下一步行动"]
        shared = self.worker_outputs["A"] + self.worker_outputs["B"]
        artifact = "# 方案\n\n" + "\n\n".join(
            f"## {heading}\n\n{shared}" for heading in headings
        )
        with self.assertRaisesRegex(DeliveryError, "not distinct"):
            self.validate(final_artifact=artifact, final_message=artifact)

    def test_distinct_worker_fragments_without_synthesis_are_rejected(self):
        headings = ["推荐结论", "详细方案", "预算与资源", "备选方案", "风险与未知项", "下一步行动"]
        seeds = [
            "铁路卧铺核验出发时刻到达站点铺位类型候补顺序以及夜间安全边界。",
            "城市换乘安排行李寄存地铁衔接步行距离午间休息以及备用车站入口。",
            "景点日程控制预约时段游览密度天气变化关门时间以及返回住宿余量。",
            "酒店费用计算实际夜数房型税费取消期限入住证件以及黄金周价格波动。",
            "餐饮门票预算区分两人人均预付款退款条件机动资金以及每日支出上限。",
            "付款复核保存订单截图发票联系人紧急退款渠道以及超额后的删除顺序。",
        ]
        parts = ["".join(f"{seed}记录批次{index}。" for index in range(1, 8))
                 for seed in seeds]
        outputs = {"A": "".join(parts[:3]), "B": "".join(parts[3:])}
        artifact = "# 方案\n\n" + "\n\n".join(
            f"## {heading}\n\n{body}" for heading, body in zip(headings, parts)
        )
        with self.assertRaisesRegex(DeliveryError, "too little original synthesis"):
            self.validate(final_artifact=artifact, final_message=artifact,
                          worker_outputs=outputs)


if __name__ == "__main__":
    unittest.main()
