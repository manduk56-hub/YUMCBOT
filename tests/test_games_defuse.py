from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from yumcbot import config


class DefuseGameTests(unittest.TestCase):
    """3선 폭탄 게임(yumcbot/games/defuse.py) 회귀 테스트."""

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

    def create_defuse_round(self, votes: list[tuple[str, str]]) -> int:
        wires = {
            color["id"]: f"{color['emoji']} {color['name']}"
            for color in bot.DEFUSE_COLORS
        }
        extra = {
            "mode": "defuse4",
            "wires": wires,
            "settlement_rule": "top_vote_penalty",
        }
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('bomb', '3선 폭탄', '3선 폭탄', 'open', '10', ?, '2026-01-01 00:00:00')
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

    def test_defuse_penalizes_only_unique_top_voters(self) -> None:
        for user_id in ("1", "2", "3", "4"):
            self.add_user(user_id, f"참가자 {user_id}", 0)
        round_id = self.create_defuse_round(
            [("1", "red"), ("2", "red"), ("3", "blue"), ("4", "green")]
        )

        result = bot.close_round_record(round_id, None)

        self.assertEqual(self.score_of("1"), -1)
        self.assertEqual(self.score_of("2"), -1)
        self.assertEqual(self.score_of("3"), 0)
        self.assertEqual(self.score_of("4"), 0)
        rendered = json.dumps(result[1].to_dict(), ensure_ascii=False)
        self.assertIn("최다 득표 선을 고른 2명 전원 -1점", rendered)

    def test_defuse_two_way_top_tie_penalizes_both_lines(self) -> None:
        for user_id in ("1", "2", "3", "4", "5"):
            self.add_user(user_id, f"참가자 {user_id}", 0)
        round_id = self.create_defuse_round(
            [
                ("1", "red"),
                ("2", "red"),
                ("3", "blue"),
                ("4", "blue"),
                ("5", "green"),
            ]
        )

        result = bot.close_round_record(round_id, None)

        for user_id in ("1", "2", "3", "4"):
            self.assertEqual(self.score_of(user_id), -1)
        self.assertEqual(self.score_of("5"), 0)
        rendered = json.dumps(result[1].to_dict(), ensure_ascii=False)
        self.assertIn("공동 1위 2개 선이 모두 폭발", rendered)

    def test_defuse_three_way_top_tie_penalizes_every_voter(self) -> None:
        for user_id in ("1", "2", "3"):
            self.add_user(user_id, f"참가자 {user_id}", 0)
        round_id = self.create_defuse_round(
            [("1", "red"), ("2", "blue"), ("3", "green")]
        )

        result = bot.close_round_record(round_id, None)

        for user_id in ("1", "2", "3"):
            self.assertEqual(self.score_of(user_id), -1)
        rendered = json.dumps(result[1].to_dict(), ensure_ascii=False)
        self.assertIn("공동 1위 3개 선이 모두 폭발", rendered)

    def test_defuse_without_votes_scores_nothing(self) -> None:
        self.add_user("1", "참가자", 3)
        round_id = self.create_defuse_round([])

        result = bot.close_round_record(round_id, None)

        self.assertEqual(self.score_of("1"), 3)
        rendered = json.dumps(result[1].to_dict(), ensure_ascii=False)
        self.assertIn("투표가 없어 점수 변동이 없습니다", rendered)

    def test_legacy_defuse_round_still_uses_its_safe_wire(self) -> None:
        self.add_user("1", "참가자", 0)
        self.add_user("2", "참가자", 0)
        extra = {
            "mode": "defuse4",
            "wires": {"red": "🟥 빨강", "black": "⬛ 검정"},
            "safe_wire_ids": ["black"],
            "hint": "구방식 힌트",
        }
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('bomb', '구방식 폭탄', '구방식 폭탄', 'open', '10', ?, '2026-01-01 00:00:00')
                """,
                (json.dumps(extra, ensure_ascii=False),),
            )
            round_id = int(cursor.lastrowid)
            conn.executemany(
                """
                INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                VALUES (?, ?, ?, '2026-01-01 00:00:00', '2026-01-01 00:00:00')
                """,
                [(round_id, "1", "black"), (round_id, "2", "red")],
            )

        bot.close_round_record(round_id, None)

        self.assertEqual(self.score_of("1"), 1)
        self.assertEqual(self.score_of("2"), 0)

    def test_defuse_thread_view_fits_in_one_action_row(self) -> None:
        """색 3개 + 익명 1개가 한 줄에 들어가며, 현재 세 색만 투표할 수 있어야 한다."""
        buttons = bot.thread_discussion_view(7, "defuse4").children

        self.assertEqual(len(buttons), 4)
        self.assertEqual(
            {button.custom_id for button in buttons if button.custom_id.startswith("eventbot:vote")},
            {f"eventbot:vote:7:{color['id']}" for color in bot.DEFUSE_COLORS},
        )

    def test_defuse_setup_only_asks_for_title_and_duration(self) -> None:
        modal = bot.StartSetupModal("defuse4", standalone_mini_game=True)

        self.assertEqual(len(modal.children), 2)
        self.assertFalse(hasattr(modal, "safe_color"))
        self.assertFalse(hasattr(modal, "defuse_hint"))

    def test_defuse_rules_explain_top_vote_and_tie_penalties(self) -> None:
        rendered = json.dumps(
            bot.build_defuse_embed(7, "폭탄").to_dict(), ensure_ascii=False
        )

        self.assertIn("가장 많은 표를 받은 선이 폭발", rendered)
        self.assertIn("공동 1위인 선이 모두 폭발", rendered)
        self.assertIn("-1점", rendered)

    def test_round_game_key_maps_bomb_rows_back_to_defuse4(self) -> None:
        with bot.db() as conn:
            row = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, extra_json, created_at)
                VALUES ('bomb', '폭탄', '폭탄', 'open', ?, '2026-01-01 00:00:00')
                RETURNING *
                """,
                (json.dumps({"mode": "defuse4"}),),
            ).fetchone()
            balance_row = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, extra_json, created_at)
                VALUES ('balance', '밸런스', '밸런스', 'open', '{}', '2026-01-01 00:00:00')
                RETURNING *
                """
            ).fetchone()

        self.assertEqual(bot.round_game_key(row), "defuse4")
        self.assertEqual(bot.round_game_key(balance_row), "balance")


if __name__ == "__main__":
    unittest.main()
