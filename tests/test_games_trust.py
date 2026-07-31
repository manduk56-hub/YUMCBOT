from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from yumcbot import config


class TrustGameTests(unittest.TestCase):
    """딜레마의 섬(yumcbot/games/trust.py) 회귀 테스트."""

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

    def test_trust_public_result_only_shows_aggregate_counts(self) -> None:
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('trust', '신뢰 테스트', '신뢰 테스트', 'open', '10', ?, '2026-01-01 00:00:00')
                """,
                (json.dumps({"trust_phase": "decision"}),),
            )
            round_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO trust_pairings (round_id, user_a_id, user_b_id, created_at)
                VALUES (?, '1', '2', '2026-01-01 00:00:00')
                """,
                (round_id,),
            )
            conn.executemany(
                """
                INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                VALUES (?, ?, ?, '2026-01-01 00:00:00', '2026-01-01 00:00:00')
                """,
                [(round_id, "1", "trust"), (round_id, "2", "betray")],
            )

        result = bot.close_round_record(round_id, None)

        self.assertIsNotNone(result)
        rendered = json.dumps(result[1].to_dict(), ensure_ascii=False)
        self.assertIn("신뢰      : 0", rendered)
        self.assertIn("단독 배신 : 1", rendered)
        self.assertIn("동시 배신 : 0", rendered)
        self.assertNotIn("쌍", rendered)
        self.assertNotIn("짝별 결과", rendered)
        self.assertNotIn("<@1>", rendered)
        self.assertNotIn("<@2>", rendered)

    def test_trust_private_result_reveals_both_choices(self) -> None:
        embed = bot.build_trust_pair_result_embed("신뢰 테스트", "1", "2", "trust", "betray")
        rendered = json.dumps(embed.to_dict(), ensure_ascii=False)

        self.assertIn("<@1>: **신뢰**", rendered)
        self.assertIn("<@2>: **배신**", rendered)
        self.assertIn("단독 배신", rendered)
        self.assertIn("<@2> +5점, 상대 0점", rendered)

    def test_trust_scoring_uses_revised_payoff_table(self) -> None:
        for user_id in ("1", "2", "3", "4", "5", "6"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('trust', '점수표 테스트', '점수표 테스트', 'open', '10', ?, '2026-01-01 00:00:00')
                """,
                (json.dumps({"trust_phase": "decision"}),),
            )
            round_id = int(cursor.lastrowid)
            conn.executemany(
                """
                INSERT INTO trust_pairings (round_id, user_a_id, user_b_id, created_at)
                VALUES (?, ?, ?, '2026-01-01 00:00:00')
                """,
                [
                    (round_id, "1", "2"),
                    (round_id, "3", "4"),
                    (round_id, "5", "6"),
                ],
            )
            conn.executemany(
                """
                INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                VALUES (?, ?, ?, '2026-01-01 00:00:00', '2026-01-01 00:00:00')
                """,
                [
                    (round_id, "1", "trust"),
                    (round_id, "2", "trust"),
                    (round_id, "3", "betray"),
                    (round_id, "4", "trust"),
                    (round_id, "5", "betray"),
                    (round_id, "6", "betray"),
                ],
            )

        bot.close_round_record(round_id, None)

        self.assertEqual(
            {user_id: self.score_of(user_id) for user_id in ("1", "2", "3", "4", "5", "6")},
            {"1": 2, "2": 2, "3": 5, "4": 0, "5": -3, "6": -3},
        )

    def test_trust_pairings_schema_stores_private_thread(self) -> None:
        with bot.db() as conn:
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(trust_pairings)").fetchall()
            }

        self.assertIn("discussion_thread_id", columns)

    def test_trust_assigns_random_choice_to_missing_votes(self) -> None:
        for user_id in ("1", "2"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('trust', '신뢰', '신뢰', 'open', '10', ?, '2026-01-01 00:00:00')
                """,
                (json.dumps({"trust_phase": "decision"}),),
            )
            round_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO trust_pairings (round_id, user_a_id, user_b_id, created_at)
                VALUES (?, '1', '2', '2026-01-01 00:00:00')
                """,
                (round_id,),
            )
            # 1번만 선택했고 2번은 미선택.
            conn.execute(
                """
                INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                VALUES (?, '1', 'trust', '2026-01-01 00:00:00', '2026-01-01 00:00:00')
                """,
                (round_id,),
            )

        with patch.object(bot.random, "choice", return_value="betray"):
            result = bot.close_round_record(round_id, None)

        with bot.db() as conn:
            choice = conn.execute(
                "SELECT choice FROM votes WHERE round_id = ? AND user_id = '2'", (round_id,)
            ).fetchone()["choice"]
        self.assertEqual(choice, "betray")

        rendered = json.dumps(result[1].to_dict(), ensure_ascii=False)
        self.assertIn("미선택 랜덤 배정", rendered)
        self.assertIn("단독 배신 : 1", rendered)
        self.assertNotIn("미선택 : ", rendered)
        # 랜덤 배정된 2번이 단독 배신자가 되어 +5점.
        self.assertEqual(self.score_of("2"), 5)


if __name__ == "__main__":
    unittest.main()
