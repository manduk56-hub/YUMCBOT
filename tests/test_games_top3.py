from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from yumcbot import config


class Top3GameTests(unittest.TestCase):
    """상위 3명 견제(yumcbot/games/top3.py) 회귀 테스트."""

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

    def create_top3_round(self, votes: list[tuple[str, str]]) -> int:
        candidates = [
            {"user_id": "1", "display_name": "1등", "score": 9},
            {"user_id": "2", "display_name": "2등", "score": 8},
            {"user_id": "3", "display_name": "3등", "score": 7},
        ]
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('top3', '견제', '견제', 'open', '10', ?, '2026-01-01 00:00:00')
                """,
                (json.dumps({"candidates": candidates}),),
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

    def test_top3_without_votes_is_void(self) -> None:
        for user_id in ("1", "2", "3"):
            self.add_user(user_id, f"후보{user_id}", 9)
        round_id = self.create_top3_round([])

        result = bot.close_round_record(round_id, None)

        for user_id in ("1", "2", "3"):
            self.assertEqual(self.score_of(user_id), 9)
        self.assertIn("무효", json.dumps(result[1].to_dict(), ensure_ascii=False))

    def test_top3_none_choice_rewards_only_its_voters(self) -> None:
        for user_id in ("1", "2", "3"):
            self.add_user(user_id, f"후보{user_id}", 9)
        for user_id in ("10", "11", "12", "13"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        # 견제 2표 vs 후보 1표 -> 견제 성공.
        # 후보에게 투표한 12번도, 아예 투표하지 않은 13번도 점수를 받지 못한다.
        round_id = self.create_top3_round(
            [("10", bot.TOP3_NONE_CHOICE), ("11", bot.TOP3_NONE_CHOICE), ("12", "1")]
        )

        bot.close_round_record(round_id, None)

        self.assertEqual(self.score_of("10"), 1)
        self.assertEqual(self.score_of("11"), 1)
        self.assertEqual(self.score_of("12"), 0)
        self.assertEqual(self.score_of("13"), 0)
        self.assertEqual(self.score_of("1"), 9)

    def test_top3_majority_hits_most_voted_candidate(self) -> None:
        for user_id in ("1", "2", "3"):
            self.add_user(user_id, f"후보{user_id}", 9)
        self.add_user("10", "참가자", 0)
        round_id = self.create_top3_round([("10", "2")])

        bot.close_round_record(round_id, None)

        self.assertEqual(self.score_of("2"), 8)
        self.assertEqual(self.score_of("1"), 9)


if __name__ == "__main__":
    unittest.main()
