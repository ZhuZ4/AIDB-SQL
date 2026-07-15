import unittest

from agent import AdkAgent


class AgentStreamDedupTests(unittest.TestCase):
    def test_repeated_final_event_is_dropped(self):
        text = "我来分析一下您的需求：\n\n**需求理解：** test"
        self.assertEqual(AdkAgent._dedup_stream_text(text, text), "")

    def test_cumulative_partial_returns_only_new_suffix(self):
        seen = "我来分析一下您的需求："
        cumulative = seen + "\n\n**执行计划：**"
        self.assertEqual(
            AdkAgent._dedup_stream_text(cumulative, seen),
            "\n\n**执行计划：**",
        )

    def test_latest_message_repeat_is_dropped_after_prior_messages(self):
        latest = "查询执行成功，返回 1,729 条。"
        seen = "较早的规划文本。\n" + latest
        self.assertEqual(AdkAgent._dedup_stream_text(latest, seen), "")

    def test_delta_text_is_preserved(self):
        self.assertEqual(
            AdkAgent._dedup_stream_text("新的增量", "已有文本"),
            "新的增量",
        )

    def test_visible_plan_uses_last_marker_and_drops_self_talk(self):
        noisy = (
            "The user is asking for a count.\n"
            "我来分析一下您的需求：旧计划\n"
            "Let me restate it.\n"
            "我来分析一下您的需求：\n\n**需求理解：** 统计学校数量"
        )
        self.assertEqual(
            AdkAgent._extract_visible_plan(noisy),
            "我来分析一下您的需求：\n\n**需求理解：** 统计学校数量",
        )

    def test_visible_answer_uses_last_answer_heading(self):
        noisy = (
            "SQL submitted successfully. Now answer the user.\n"
            "## 回答\n旧答案\n"
            "SQL submitted successfully. Now answer the user.\n"
            "---\n\n## 回答\n**最终答案：1,729。**"
        )
        self.assertEqual(
            AdkAgent._extract_visible_answer(noisy),
            "---\n\n## 回答\n**最终答案：1,729。**",
        )


if __name__ == "__main__":
    unittest.main()
