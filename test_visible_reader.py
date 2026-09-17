import unittest

from .visible_reader import VisibleReadError, fetch_turn


THREAD = "55555555-5555-4555-8555-555555555555"
TURN = "01a0a050-c213-7f91-811d-d535163768ca"
OLDER = "01a0a023-058f-70b2-8692-dfe7be217986"


class VisibleReaderTests(unittest.TestCase):
    def test_exact_turn_final_answer_and_source_hash(self):
        def call(method, params):
            self.assertEqual(method, "thread/turns/list")
            self.assertEqual(params["itemsView"], "full")
            self.assertEqual(params["threadId"], THREAD)
            return {"data": [{"id": TURN, "status": "completed", "items": [
                {"type": "agentMessage", "id": "comment", "phase": "commentary", "text": "working"},
                {"type": "agentMessage", "id": "final", "phase": "final_answer", "text": "done"}]}]}

        result = fetch_turn(call, THREAD, TURN)
        self.assertEqual(result["text"], "done")
        self.assertEqual(result["message_id"], "final")
        self.assertEqual(len(result["sha256"]), 64)

    def test_pages_without_selecting_stale_turn(self):
        def call(method, params):
            if "cursor" not in params:
                return {"data": [{"id": OLDER, "status": "completed", "items": [
                    {"type": "agentMessage", "id": "old", "phase": "final_answer", "text": "stale"}]}],
                    "nextCursor": "page-2"}
            return {"data": [{"id": TURN, "status": "completed", "items": [
                {"type": "agentMessage", "id": "new", "phase": "final_answer", "text": "current"}]}],
                "nextCursor": None}

        self.assertEqual(fetch_turn(call, THREAD, TURN)["text"], "current")

    def test_empty_or_unfinished_target_is_rejected(self):
        for turn in (
            {"id": TURN, "status": "completed", "items": []},
            {"id": TURN, "status": "inProgress", "items": [
                {"type": "agentMessage", "id": "a", "phase": "final_answer", "text": "premature"}]},
        ):
            with self.subTest(turn=turn), self.assertRaises(VisibleReadError):
                fetch_turn(lambda method, params: {"data": [turn]}, THREAD, TURN)

    def test_duplicate_final_answer_is_rejected(self):
        turn = {"id": TURN, "status": "completed", "items": [
            {"type": "agentMessage", "id": "a", "phase": "final_answer", "text": "one"},
            {"type": "agentMessage", "id": "b", "phase": "final_answer", "text": "two"}]}
        with self.assertRaises(VisibleReadError):
            fetch_turn(lambda method, params: {"data": [turn]}, THREAD, TURN)


if __name__ == "__main__":
    unittest.main()
