from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from yumcbot import config


class PairingTests(unittest.TestCase):
    """1:1 짝 매칭 공용 로직(yumcbot/games/pairing.py) 회귀 테스트."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        # patch 대상이 config인 이유는 test_bot.py setUp의 주석 참고.
        self.db_patch = patch.object(config, "DB_PATH", Path(self.temp_dir.name) / "test.sqlite3")
        self.db_patch.start()
        bot.init_db()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def _insert_round(self, round_id: int, round_type: str) -> None:
        with bot.db() as conn:
            conn.execute(
                """
                INSERT INTO rounds (id, type, title, question, created_at)
                VALUES (?, ?, '제목', '질문', '2026-01-01 00:00:00')
                """,
                (round_id, round_type),
            )

    def _insert_pairing(self, round_id: int, user_a_id: str, user_b_id: str) -> None:
        with bot.db() as conn:
            conn.execute(
                """
                INSERT INTO trust_pairings (round_id, user_a_id, user_b_id, created_at)
                VALUES (?, ?, ?, '2026-01-01 00:00:00')
                """,
                (round_id, user_a_id, user_b_id),
            )

    def test_odd_participants_leave_one_user_unmatched(self) -> None:
        pairs, unmatched_user_id = bot.build_random_trust_pairs(["1", "2", "3"], set())

        self.assertEqual(len(pairs), 1)
        self.assertIsNotNone(unmatched_user_id)
        paired = {user_id for pair in pairs for user_id in pair}
        self.assertNotIn(unmatched_user_id, paired)

    def test_recent_match_pairs_isolates_by_round_type_and_normalizes(self) -> None:
        # trust 라운드 1·3, sand 라운드 2·4 를 섞어 심는다.
        self._insert_round(1, "trust")
        self._insert_round(2, "sand")
        self._insert_round(3, "trust")
        self._insert_round(4, "sand")
        self._insert_pairing(1, "A", "B")  # trust
        self._insert_pairing(2, "C", "A")  # sand, 저장 순서가 뒤집힘
        self._insert_pairing(3, "A", "C")  # trust
        self._insert_pairing(4, "B", "A")  # sand

        with bot.db() as conn:
            sand_recent = bot.recent_match_pairs(conn, 5, "sand")
            trust_recent = bot.recent_match_pairs(conn, 5, "trust")

        # sand 라운드는 sand 짝만 보고, trust 라운드와 섞이지 않는다. 짝은 정렬되어 정규화된다.
        self.assertEqual(sand_recent, {("A", "C"), ("A", "B")})
        self.assertEqual(trust_recent, {("A", "B"), ("A", "C")})

    def test_recent_match_pairs_respects_history_window(self) -> None:
        for round_id in range(1, 6):  # sand 라운드 1~5
            self._insert_round(round_id, "sand")
            self._insert_pairing(round_id, f"a{round_id}", f"b{round_id}")

        with bot.db() as conn:
            recent = bot.recent_match_pairs(conn, 6, "sand", history_rounds=2)

        # 직전 2개 라운드(4·5)의 짝만 회피 대상이 된다.
        self.assertEqual(recent, {("a4", "b4"), ("a5", "b5")})

    def test_recent_match_pairs_empty_when_no_history(self) -> None:
        with bot.db() as conn:
            self.assertEqual(bot.recent_match_pairs(conn, 1, "sand"), set())


if __name__ == "__main__":
    unittest.main()
