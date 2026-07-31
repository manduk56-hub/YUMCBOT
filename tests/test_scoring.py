from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from yumcbot import config


class ScoringTests(unittest.TestCase):
    """점수·잠수 코인(yumcbot/scoring.py) 회귀 테스트."""

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

    def _display_name(self, conn, user_id: str) -> str:
        row = conn.execute(
            "SELECT display_name FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["display_name"]

    def scores_of(self, conn, *user_ids: str) -> dict[str, int]:
        placeholders = ", ".join("?" for _ in user_ids)
        return {
            row["user_id"]: row["total_score"]
            for row in conn.execute(
                f"SELECT user_id, total_score FROM users WHERE user_id IN ({placeholders})",
                user_ids,
            )
        }

    def test_add_score_preserves_existing_name_when_none(self) -> None:
        """멤버 캐시 미스로 display_name=None이 와도 기존 이름을 덮지 않는다 (issue #19)."""
        self.add_user("42", "진짜이름", 0)
        with bot.db() as conn:
            bot.add_score(conn, "42", None, 5, "라운드 보상")

            self.assertEqual(self._display_name(conn, "42"), "진짜이름")
            self.assertEqual(self.scores_of(conn, "42")["42"], 5)

    def test_add_score_new_user_with_none_gets_placeholder(self) -> None:
        """행이 없던 유저 + None이면 NOT NULL 위반 없이 플레이스홀더로 채운다 (issue #19)."""
        with bot.db() as conn:
            bot.add_score(conn, "99", None, 3, "라운드 보상")

            self.assertEqual(self._display_name(conn, "99"), "User 99")
            self.assertEqual(self.scores_of(conn, "99")["99"], 3)

    def test_add_score_updates_name_when_present(self) -> None:
        """이름이 읽히면(None 아님) 정상적으로 갱신한다."""
        self.add_user("7", "옛이름", 0)
        with bot.db() as conn:
            bot.add_score(conn, "7", "새이름", 1, "라운드 보상")

            self.assertEqual(self._display_name(conn, "7"), "새이름")

    def test_afk_coins_accumulate_and_are_spent_atomically(self) -> None:
        self.add_user("1", "참가자", 0)
        with bot.db() as conn:
            for _ in range(3):
                bot.grant_afk_coins(conn, "1", bot.AFK_COIN_PER_TICK)
            self.assertEqual(bot.get_afk_coins(conn, "1"), 3)

            # 잔액이 모자라면 차감하지 않고 False를 돌려줘야 한다.
            self.assertFalse(bot.spend_afk_coins(conn, "1", bot.SCORE_REVEAL_COST))
            self.assertEqual(bot.get_afk_coins(conn, "1"), 3)

            bot.grant_afk_coins(conn, "1", bot.SCORE_REVEAL_COST)
            self.assertTrue(bot.spend_afk_coins(conn, "1", bot.SCORE_REVEAL_COST))
            self.assertEqual(bot.get_afk_coins(conn, "1"), 3)

    def test_season_close_blocks_score_and_afk_coin_mutations(self) -> None:
        self.add_user("1", "참가자", 5)
        with bot.db() as conn:
            bot.grant_afk_coins(conn, "1", 3)
            conn.execute(
                "UPDATE season_state SET status = 'closed', closed_at = '2026-01-02' WHERE singleton_id = 1"
            )

            bot.grant_afk_coins(conn, "1", 10)
            self.assertEqual(bot.get_afk_coins(conn, "1"), 3)
            with self.assertRaises(bot.SeasonClosedError):
                bot.add_score(conn, "1", "참가자", 2, "종료 뒤 보상")
            self.assertEqual(self.scores_of(conn, "1")["1"], 5)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM score_logs").fetchone()[0], 0
            )

    def test_spending_afk_coins_without_any_row_fails(self) -> None:
        self.add_user("1", "참가자", 0)
        with bot.db() as conn:
            self.assertFalse(bot.spend_afk_coins(conn, "1", bot.SCORE_REVEAL_COST))
            self.assertEqual(bot.get_afk_coins(conn, "1"), 0)


if __name__ == "__main__":
    unittest.main()
