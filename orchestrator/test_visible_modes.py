import json
import unittest

from .visible_modes import DEFAULT_CONFIG, ModeConfigError, resolve_mode


class VisibleModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))

    def test_default_uses_existing_two_workers(self):
        plan = resolve_mode(self.config)
        self.assertEqual(plan["mode"], "small")
        self.assertEqual([w["worker_id"] for w in plan["workers"]], ["A", "B"])
        self.assertEqual([(w["requested_model"], w["requested_thinking"])
                          for w in plan["workers"]],
                         [("gpt-5.6-luna", "medium")] * 2)
        self.assertEqual(plan["review_policy"], "none")
        self.assertEqual(plan["max_worker_turns"], 2)
        self.assertEqual(plan["target_elapsed_seconds"], 480)
        self.assertEqual(plan["capacity_policy"]["capacity_floor"], 1)
        self.assertEqual(plan["capacity_policy"]["runtime_backoff"]["max_steps"], 2)
        self.assertEqual(plan["mode_provenance"]["resolved_policy"]["mode"], "small")
        self.assertEqual(len(plan["mode_provenance"]["config_sha256"]), 64)

    def test_large_adds_two_workers_and_allows_role_change(self):
        plan = resolve_mode(self.config, "large", {"C": "独立验证"})
        self.assertEqual([w["worker_id"] for w in plan["workers"]], ["A", "B", "C", "D"])
        self.assertEqual(plan["workers"][2]["role"], "独立验证")
        self.assertIsNone(plan["workers"][0]["role"])
        self.assertEqual([(w["requested_model"], w["requested_thinking"])
                          for w in plan["workers"][2:]],
                         [("gpt-5.6-terra", "low"),
                          ("gpt-5.6-sol", "low")])
        self.assertEqual(plan["max_worker_turns"], 4)
        self.assertEqual(plan["max_blocker_rechecks"], 0)
        self.assertEqual(plan["review_policy"], "none")

    def test_reviewed_modes_preserve_full_peer_review_path(self):
        small = resolve_mode(self.config, "small_reviewed")
        large = resolve_mode(self.config, "large_reviewed")
        self.assertEqual((small["review_policy"], small["max_worker_turns"]),
                         ("full", 8))
        self.assertEqual((large["review_policy"], large["max_worker_turns"]),
                         ("full", 12))

    def test_delivery_and_external_review_are_cost_bounded(self):
        plan = resolve_mode(self.config, "small")
        self.assertTrue(plan["delivery_contract"]["inline_full_result"])
        self.assertEqual(plan["delivery_contract"]["minimum_worker_overlap_chars"], 24)
        self.assertEqual(plan["delivery_contract"]["minimum_section_chars"], 40)
        self.assertEqual(plan["external_review"]["explicit_request_rounds"], 1)
        self.assertFalse(plan["external_review"]["automatic_recheck"])

    def test_speed_is_left_to_the_user(self):
        plan = resolve_mode(self.config, "large")
        for worker in plan["workers"]:
            self.assertNotIn("requested_service_tier", worker)
            self.assertNotIn("speed_check_required", worker)

    def test_inactive_worker_role_and_duplicate_thread_are_rejected(self):
        with self.assertRaises(ModeConfigError):
            resolve_mode(self.config, "small", {"C": "审查"})
        config = json.loads(json.dumps(self.config))
        config["workers"]["D"]["thread_id"] = config["workers"]["C"]["thread_id"]
        with self.assertRaises(ModeConfigError):
            resolve_mode(config, "large")

    def test_mode_fingerprint_changes_when_resolved_policy_changes(self):
        baseline = resolve_mode(self.config, "small")["mode_provenance"]
        changed = json.loads(json.dumps(self.config))
        changed["modes"]["small"]["target_elapsed_seconds"] += 1
        updated = resolve_mode(changed, "small")["mode_provenance"]
        self.assertNotEqual(baseline["config_sha256"], updated["config_sha256"])
        self.assertNotEqual(baseline["resolved_policy_sha256"],
                            updated["resolved_policy_sha256"])

    def test_schema2_rejects_broken_takeover_history(self):
        config = json.loads(json.dumps(self.config))
        config["workers"]["A"]["generation"] = 2
        config["workers"]["A"]["previous_thread_ids"] = ["old-a"]
        config["workers"]["A"]["takeover_history"] = []
        with self.assertRaises(ModeConfigError):
            resolve_mode(config, "small")


if __name__ == "__main__":
    unittest.main()
