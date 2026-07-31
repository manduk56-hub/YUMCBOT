from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from yumcbot import config


class UniqueNumberGameTests(unittest.TestCase):
    """유일 숫자 게임(yumcbot/games/unique_number.py) 회귀 테스트."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        # patch 대상이 config인 이유는 test_bot.py setUp의 주석 참고.
        self.db_patch = patch.object(config, "DB_PATH", Path(self.temp_dir.name) / "test.sqlite3")
        self.db_patch.start()
        bot.init_db()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def add_user(self, user_id: str, display_name: str, score: int) -> None:
        with bot.db() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, display_name, total_score, created_at, updated_at)
                VALUES (?, ?, ?, '2026-01-01 00:00:00', '2026-01-01 00:00:00')
                """,
                (user_id, display_name, score),
            )

    def score_of(self, user_id: str) -> int:
        with bot.db() as conn:
            return int(
                conn.execute(
                    "SELECT total_score FROM users WHERE user_id = ?", (user_id,)
                ).fetchone()["total_score"]
            )

    def create_unique_number_round(self, votes: list[tuple[str, str]]) -> int:
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('grid', '유일 숫자', '유일 숫자', 'open', '10', ?,
                        '2026-01-01 00:00:00')
                """,
                (json.dumps({"mode": "unique_number"}),),
            )
            round_id = int(cursor.lastrowid)
            conn.executemany(
                """
                INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                VALUES (?, ?, ?, '2026-01-01 00:00:00', '2026-01-01 00:00:00')
                """,
                [(round_id, user_id, choice) for user_id, choice in votes],
            )
        return round_id

    def test_unique_number_example_rewards_nine_after_duplicate_elevens_are_removed(self) -> None:
        for user_id in ("1", "2", "3", "4", "5"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        round_id = self.create_unique_number_round(
            [("1", "4"), ("2", "6"), ("3", "9"), ("4", "11"), ("5", "11")]
        )

        result = bot.close_round_record(round_id, None)

        self.assertIsNotNone(result)
        self.assertEqual(self.score_of("1"), 0)
        self.assertEqual(self.score_of("2"), 0)
        self.assertEqual(self.score_of("3"), 2)
        self.assertEqual(self.score_of("4"), 0)
        self.assertEqual(self.score_of("5"), 0)
        rendered = json.dumps(result[1].to_dict(), ensure_ascii=False)
        self.assertIn("최고 유일 숫자 **9**", rendered)
        self.assertIn("**+2점**", rendered)
        self.assertIn("중복 숫자 선택", rendered)

    def test_unique_number_reward_tiers(self) -> None:
        expected = {
            1: 1,
            5: 1,
            6: 2,
            10: 2,
            11: 3,
            15: 3,
        }
        for number, points in expected.items():
            with self.subTest(number=number):
                self.assertEqual(bot.unique_number_reward(number), points)

    def test_unique_number_all_duplicates_score_nothing(self) -> None:
        for user_id in ("1", "2", "3", "4"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        round_id = self.create_unique_number_round(
            [("1", "15"), ("2", "15"), ("3", "4"), ("4", "4")]
        )

        result = bot.close_round_record(round_id, None)

        for user_id in ("1", "2", "3", "4"):
            self.assertEqual(self.score_of(user_id), 0)
        self.assertIn(
            "모든 선택 숫자가 중복",
            json.dumps(result[1].to_dict(), ensure_ascii=False),
        )

    def test_unique_number_view_uses_three_rows_of_five_buttons(self) -> None:
        buttons = bot.unique_number_vote_view(7).children

        self.assertEqual(len(buttons), 15)
        self.assertEqual([button.row for button in buttons], [0] * 5 + [1] * 5 + [2] * 5)
        self.assertEqual(
            {button.custom_id for button in buttons},
            {f"eventbot:vote:7:{number}" for number in range(1, 16)},
        )
        rendered = json.dumps(bot.build_unique_number_embed(7, "숫자").to_dict(), ensure_ascii=False)
        self.assertIn("중복되지 않은 숫자 중", rendered)
        self.assertIn("11~15", rendered)
        self.assertIn("+3점", rendered)

    def test_unique_number_round_uses_mode_to_distinguish_it_from_legacy_grid(self) -> None:
        round_id = self.create_unique_number_round([])
        with bot.db() as conn:
            row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()

        self.assertEqual(row["type"], "grid")
        self.assertEqual(bot.round_game_key(row), "unique_number")

    def test_grid_game_is_removed(self) -> None:
        self.assertNotIn("grid", bot.MINI_GAME_TYPES)
        self.assertNotIn("grid", bot.GAME_NAMES)
        self.assertFalse(hasattr(bot, "build_grid_embed"))
        self.assertIn("unique_number", bot.MINI_GAME_TYPES)

    def test_legacy_grid_round_closes_without_scoring(self) -> None:
        """CHECK 제약에 남아 있는 옛 grid 라운드가 열려 있어도 마감이 터지지 않아야 한다."""
        self.add_user("1", "참가자", 5)
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('grid', '옛 타일', '옛 타일', 'open', '10', '{}', '2026-01-01 00:00:00')
                """
            )
            round_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                VALUES (?, '1', '5', '2026-01-01 00:00:00', '2026-01-01 00:00:00')
                """,
                (round_id,),
            )

        result = bot.close_round_record(round_id, None)

        self.assertIsNotNone(result)
        self.assertEqual(self.score_of("1"), 5)


if __name__ == "__main__":
    unittest.main()
