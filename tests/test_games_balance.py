from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from yumcbot import config


class BalanceGameTests(unittest.TestCase):
    """밸런스·소수파 밸런스(yumcbot/games/balance.py) 회귀 테스트."""

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

    def create_balance_round(self, votes: list[tuple[str, str]]) -> int:
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, option_a, option_b, status, channel_id, extra_json, created_at)
                VALUES ('balance', '밸런스', '선택', '짜장', '짬뽕', 'open', '10', '{}', '2026-01-01 00:00:00')
                """
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

    def create_minority_balance_round(self, votes: list[tuple[str, str]]) -> int:
        extra = {
            "mode": "minority_balance",
            "balance_phase": "initial",
            "duration_minutes": 1,
            "auto_close_at": 1,
        }
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, option_a, option_b, status, channel_id, extra_json, created_at)
                VALUES ('balance', '밸런스 게임 라운드', '선택', '짜장', '짬뽕', 'open', '10', ?,
                        '2026-01-01 00:00:00')
                """,
                (json.dumps(extra, ensure_ascii=False),),
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

    def test_balance_one_sided_vote_penalizes_every_voter(self) -> None:
        for user_id in ("1", "2", "3"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        round_id = self.create_balance_round([("1", "A"), ("2", "A"), ("3", "A")])

        result = bot.close_round_record(round_id, None)

        self.assertIsNotNone(result)
        for user_id in ("1", "2", "3"):
            self.assertEqual(self.score_of(user_id), -2)
        self.assertIn("-2점", json.dumps(result[1].to_dict(), ensure_ascii=False))

    def test_balance_only_lone_minority_gets_two_points(self) -> None:
        for user_id in ("1", "2", "3"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        round_id = self.create_balance_round([("1", "A"), ("2", "A"), ("3", "B")])

        result = bot.close_round_record(round_id, None)

        self.assertEqual(self.score_of("1"), 0)
        self.assertEqual(self.score_of("2"), 0)
        self.assertEqual(self.score_of("3"), 2)
        self.assertIn(
            "소수파 `B` 단독 선택자만 +2점",
            json.dumps(result[1].to_dict(), ensure_ascii=False),
        )
        self.assertIn(
            "다수파 `A` 선택자는 점수 없음",
            json.dumps(result[1].to_dict(), ensure_ascii=False),
        )

    def test_balance_minority_with_multiple_voters_gets_no_bonus(self) -> None:
        for user_id in ("1", "2", "3", "4", "5"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        round_id = self.create_balance_round(
            [("1", "A"), ("2", "A"), ("3", "A"), ("4", "B"), ("5", "B")]
        )

        bot.close_round_record(round_id, None)

        self.assertEqual(self.score_of("1"), 1)
        self.assertEqual(self.score_of("2"), 1)
        self.assertEqual(self.score_of("3"), 1)
        self.assertEqual(self.score_of("4"), 0)
        self.assertEqual(self.score_of("5"), 0)

    def test_balance_result_hides_who_voted_what(self) -> None:
        for user_id in ("1", "2"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        round_id = self.create_balance_round([("1", "A"), ("2", "B")])

        result = bot.close_round_record(round_id, None)

        rendered = json.dumps(result[1].to_dict(), ensure_ascii=False)
        self.assertNotIn("<@", rendered)
        self.assertIn("1명", rendered)

    def test_balance_without_votes_scores_nothing(self) -> None:
        self.add_user("1", "참가자", 3)
        round_id = self.create_balance_round([])

        bot.close_round_record(round_id, None)

        self.assertEqual(self.score_of("1"), 3)

    def test_minority_balance_initial_guide_is_identical_to_normal_balance(self) -> None:
        normal = bot.build_balance_embed(1, "선택", "짜장", "짬뽕").to_dict()
        disguised = bot.build_balance_embed(2, "선택", "짜장", "짬뽕").to_dict()

        self.assertIn("balance", bot.MINI_GAME_TYPES)
        self.assertIn("minority_balance", bot.MINI_GAME_TYPES)
        self.assertNotEqual("balance", "minority_balance")
        self.assertEqual(normal, disguised)
        self.assertNotIn("소수", json.dumps(disguised, ensure_ascii=False))
        self.assertEqual(bot.GAME_NAMES["minority_balance"], "밸런스 게임")

        round_id = self.create_minority_balance_round([])
        with bot.db() as conn:
            row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        self.assertEqual(bot.round_game_key(row), "minority_balance")

    def test_minority_balance_first_tally_keeps_votes_and_starts_revote(self) -> None:
        round_id = self.create_minority_balance_round(
            [("1", "A"), ("2", "A"), ("3", "B")]
        )

        transition = bot.start_minority_balance_revote(round_id)

        self.assertIsNotNone(transition)
        self.assertEqual(transition["first_a_count"], 2)
        self.assertEqual(transition["first_b_count"], 1)
        with bot.db() as conn:
            row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
            vote_count = conn.execute(
                "SELECT COUNT(*) AS count FROM votes WHERE round_id = ?", (round_id,)
            ).fetchone()["count"]
        self.assertEqual(row["status"], "open")
        self.assertEqual(bot.round_extra(row)["balance_phase"], "revote")
        self.assertEqual(vote_count, 3)

        rendered = json.dumps(
            bot.build_minority_balance_revote_embed(
                transition,
                transition["first_a_count"],
                transition["first_b_count"],
                transition["close_at"],
            ).to_dict(),
            ensure_ascii=False,
        )
        self.assertIn("🅰️ 짜장", rendered)
        self.assertIn("2명", rendered)
        self.assertIn("인원이 더 적은 선택지", rendered)
        self.assertIn("1차 선택은 그대로 유지", rendered)

    def test_minority_balance_rewards_only_final_minority(self) -> None:
        for user_id in ("1", "2", "3"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        round_id = self.create_minority_balance_round(
            [("1", "A"), ("2", "A"), ("3", "B")]
        )
        bot.start_minority_balance_revote(round_id)
        with bot.db() as conn:
            # 1번은 1차 선택(A)을 유지하고, 2번만 A에서 B로 바꾼다.
            conn.execute(
                """
                INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                VALUES (?, ?, ?, '2026-01-01 00:01:00', '2026-01-01 00:01:00')
                ON CONFLICT(round_id, user_id) DO UPDATE SET
                    choice = excluded.choice,
                    updated_at = excluded.updated_at
                """,
                (round_id, "2", "B"),
            )

        result = bot.close_round_record(round_id, None)

        self.assertEqual(self.score_of("1"), 1)
        self.assertEqual(self.score_of("2"), 0)
        self.assertEqual(self.score_of("3"), 0)
        self.assertIn("소수파 `A`", json.dumps(result[1].to_dict(), ensure_ascii=False))

    def test_minority_balance_tie_and_empty_side_score_nothing(self) -> None:
        for user_id in ("1", "2"):
            self.add_user(user_id, f"참가자{user_id}", 0)

        tied_round = self.create_minority_balance_round([])
        bot.start_minority_balance_revote(tied_round)
        with bot.db() as conn:
            conn.executemany(
                """
                INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                VALUES (?, ?, ?, '2026-01-01', '2026-01-01')
                """,
                [(tied_round, "1", "A"), (tied_round, "2", "B")],
            )
        bot.close_round_record(tied_round, None)

        empty_side_round = self.create_minority_balance_round([])
        bot.start_minority_balance_revote(empty_side_round)
        with bot.db() as conn:
            conn.executemany(
                """
                INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                VALUES (?, ?, ?, '2026-01-01', '2026-01-01')
                """,
                [(empty_side_round, "1", "A"), (empty_side_round, "2", "A")],
            )
        bot.close_round_record(empty_side_round, None)

        self.assertEqual(self.score_of("1"), 0)
        self.assertEqual(self.score_of("2"), 0)


if __name__ == "__main__":
    unittest.main()
