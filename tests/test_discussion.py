from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from yumcbot import config


class AnonymousDiscussionTests(unittest.TestCase):
    """익명 토론(yumcbot/discussion.py) 회귀 테스트."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        # patch 대상이 config인 이유는 test_bot.py setUp의 주석 참고.
        self.db_patch = patch.object(config, "DB_PATH", Path(self.temp_dir.name) / "test.sqlite3")
        self.db_patch.start()
        bot.init_db()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_anonymous_message_embed_uses_stable_number_color(self) -> None:
        first = bot.build_anonymous_message_embed(1, "첫 발언").to_dict()
        same_user_next_post = bot.build_anonymous_message_embed(1, "다음 발언").to_dict()
        second = bot.build_anonymous_message_embed(2, "다른 발언").to_dict()

        self.assertNotIn("title", first)
        self.assertNotIn("title", second)
        self.assertEqual(first["description"], "첫 발언")
        self.assertEqual(first["color"], same_user_next_post["color"])
        self.assertNotEqual(first["color"], second["color"])

    def test_anonymous_message_embed_reuses_palette_safely(self) -> None:
        palette_size = len(bot.ANONYMOUS_EMBED_COLORS)

        self.assertEqual(palette_size, 20)
        self.assertEqual(len(set(bot.ANONYMOUS_EMBED_COLORS)), palette_size)

        def rgb(color: int) -> tuple[int, int, int]:
            return ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)

        def distance(left: int, right: int) -> float:
            return sum(
                (left_channel - right_channel) ** 2
                for left_channel, right_channel in zip(rgb(left), rgb(right))
            ) ** 0.5

        adjacent_distances = [
            distance(left, right)
            for left, right in zip(
                bot.ANONYMOUS_EMBED_COLORS,
                bot.ANONYMOUS_EMBED_COLORS[1:],
            )
        ]
        self.assertGreater(min(adjacent_distances), 100)

        first = bot.build_anonymous_message_embed(1, "발언").to_dict()
        wrapped = bot.build_anonymous_message_embed(palette_size + 1, "발언").to_dict()

        self.assertEqual(first["color"], wrapped["color"])

    def test_anonymous_numbers_are_stable_per_user(self) -> None:
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, extra_json, created_at)
                VALUES ('balance', '밸런스', '선택', 'open', '{}', '2026-01-01 00:00:00')
                """
            )
            round_id = int(cursor.lastrowid)

            def post(user_id: str) -> int:
                number = bot.assign_anonymous_number(conn, round_id, user_id)
                conn.execute(
                    """
                    INSERT INTO discussion_messages
                        (round_id, user_id, anon_number, message_id, content, created_at)
                    VALUES (?, ?, ?, '1', '내용', '2026-01-01 00:00:00')
                    """,
                    (round_id, user_id, number),
                )
                return number

            # 같은 유저가 여러 번 발언해도 번호가 유지되고, 다른 유저는 다음 번호를 받는다.
            self.assertEqual(post("1"), 1)
            self.assertEqual(post("2"), 2)
            self.assertEqual(post("1"), 1)
            self.assertEqual(post("3"), 3)

            count = conn.execute(
                "SELECT COUNT(*) AS c FROM discussion_messages WHERE round_id = ? AND user_id = '1'",
                (round_id,),
            ).fetchone()["c"]
        self.assertEqual(count, 2)

    def test_thread_view_carries_vote_buttons_with_the_channel_custom_ids(self) -> None:
        """스레드의 시작 메시지 버튼은 눌리지 않는다. 스레드 안에도 같은 custom_id의 선택 버튼이 있어야 한다."""
        channel_ids = {
            button.custom_id for button in bot.vote_view(7, bot.BALANCE_VOTE_CHOICES).children
        }
        thread_ids = {button.custom_id for button in bot.thread_discussion_view(7, "balance").children}

        self.assertEqual(channel_ids, {"eventbot:vote:7:A", "eventbot:vote:7:B"})
        self.assertTrue(channel_ids < thread_ids)
        self.assertIn("eventbot:anonymous:7", thread_ids)


if __name__ == "__main__":
    unittest.main()
