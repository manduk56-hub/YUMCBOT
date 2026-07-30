from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from yumcbot import config


class EventLifecycleTests(unittest.TestCase):
    """이벤트/라운드 생애주기(yumcbot/events.py) 회귀 테스트."""

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

    def test_event_join_view_has_join_and_cancel_buttons(self) -> None:
        view = bot.event_join_view(12, 3)

        self.assertEqual([item.label for item in view.children], ["참여하기 (3명)", "참여 취소하기"])
        self.assertEqual(
            [item.custom_id for item in view.children],
            ["eventbot:eventjoin:12", "eventbot:eventcancel:12"],
        )
        self.assertEqual(view.children[1].style, bot.discord.ButtonStyle.danger)

    def test_result_embed_omits_removed_statistics(self) -> None:
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, option_a, option_b, status, channel_id, extra_json, created_at)
                VALUES ('balance', '결과 테스트', '선택', 'A', 'B', 'open', '10', ?, '2026-01-01 00:00:00')
                """,
                (json.dumps({"duration_minutes": 5, "auto_close_at": 12345}),),
            )
            round_id = int(cursor.lastrowid)

        result = bot.close_round_record(round_id, None, closed_by="자동 마감")

        self.assertIsNotNone(result)
        rendered = json.dumps(result[1].to_dict(), ensure_ascii=False)
        for removed in ("투표 참여", "통계", "토론 발언", "마감 방식", "진행 시간", "예정 마감"):
            self.assertNotIn(removed, rendered)

    def test_force_close_current_event_cancels_open_round_without_scoring(self) -> None:
        self.add_user("1", "참가자", 5)
        with bot.db() as conn:
            event_cursor = conn.execute(
                """
                INSERT INTO event_sessions (status, channel_id, created_at)
                VALUES ('active', '10', '2026-01-01 00:00:00')
                """
            )
            event_id = int(event_cursor.lastrowid)
            round_cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at, event_session_id)
                VALUES ('trust', '개발 테스트', '개발 테스트', 'open', '10', ?,
                        '2026-01-01 00:00:00', ?)
                """,
                (json.dumps({"trust_phase": "decision", "auto_close_at": 12345}), event_id),
            )
            round_id = int(round_cursor.lastrowid)

            result = bot.force_close_current_event(conn)

            event_row = conn.execute(
                "SELECT status, closed_at FROM event_sessions WHERE id = ?", (event_id,)
            ).fetchone()
            round_row = conn.execute(
                "SELECT status, closed_at, extra_json FROM rounds WHERE id = ?", (round_id,)
            ).fetchone()
            score = conn.execute("SELECT total_score FROM users WHERE user_id = '1'").fetchone()[0]

        self.assertIsNotNone(result)
        self.assertEqual(event_row["status"], "closed")
        self.assertIsNotNone(event_row["closed_at"])
        self.assertEqual(round_row["status"], "closed")
        self.assertIsNotNone(round_row["closed_at"])
        extra = json.loads(round_row["extra_json"])
        self.assertTrue(extra["force_closed"])
        self.assertEqual(extra["trust_phase"], "cancelled")
        self.assertEqual(score, 5)

    def test_force_close_current_event_without_open_event_is_noop(self) -> None:
        with bot.db() as conn:
            self.assertIsNone(bot.force_close_current_event(conn))

    def test_force_close_current_event_cancels_unresolved_cutting(self) -> None:
        with bot.db() as conn:
            event_cursor = conn.execute(
                """
                INSERT INTO event_sessions (status, channel_id, created_at)
                VALUES ('active', '10', '2026-01-01 00:00:00')
                """
            )
            event_id = int(event_cursor.lastrowid)
            round_cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at,
                     closed_at, event_session_id)
                VALUES ('cutting', '세공', '세공', 'closed', '10', '{}',
                        '2026-01-01 00:00:00', '2026-01-01 00:05:00', ?)
                """,
                (event_id,),
            )
            round_id = int(round_cursor.lastrowid)

            bot.force_close_current_event(conn)

            round_row = conn.execute(
                "SELECT extra_json FROM rounds WHERE id = ?", (round_id,)
            ).fetchone()

        self.assertTrue(json.loads(round_row["extra_json"])["force_closed"])

    def test_event_stays_open_until_planned_mini_game_finishes(self) -> None:
        with bot.db() as conn:
            event_cursor = conn.execute(
                """
                INSERT INTO event_sessions (status, channel_id, created_at)
                VALUES ('active', '10', '2026-01-01 00:00:00')
                """
            )
            event_id = int(event_cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at,
                     closed_at, event_session_id)
                VALUES ('trust', '메인', '메인', 'closed', '10', ?,
                        '2026-01-01 00:00:00', '2026-01-01 00:05:00', ?)
                """,
                (json.dumps({"trust_phase": "scored", "planned_mini_game": "balance"}), event_id),
            )

        self.assertIsNone(bot.close_completed_event(event_id))
        with bot.db() as conn:
            status = conn.execute(
                "SELECT status FROM event_sessions WHERE id = ?", (event_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "active")

    def test_event_completion_closes_session_and_builds_final_summary(self) -> None:
        self.add_user("1", "참가자", 1)
        with bot.db() as conn:
            event_cursor = conn.execute(
                """
                INSERT INTO event_sessions (status, channel_id, created_at)
                VALUES ('active', '10', '2026-01-01 00:00:00')
                """
            )
            event_id = int(event_cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO event_participants (event_session_id, user_id, joined_at)
                VALUES (?, '1', '2026-01-01 00:00:00')
                """,
                (event_id,),
            )
            main_cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at,
                     closed_at, event_session_id)
                VALUES ('trust', '딜레마', '딜레마', 'closed', '10', ?,
                        '2026-01-01 00:00:00', '2026-01-01 00:05:00', ?)
                """,
                (json.dumps({"trust_phase": "scored", "planned_mini_game": "balance"}), event_id),
            )
            mini_cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, option_a, option_b, status, channel_id,
                     extra_json, created_at, closed_at, event_session_id)
                VALUES ('balance', '결승 선택', '선택', 'A', 'B', 'closed', '10', '{}',
                        '2026-01-01 00:05:00', '2026-01-01 00:10:00', ?)
                """,
                (event_id,),
            )
            conn.execute(
                """
                INSERT INTO score_logs (round_id, user_id, delta, reason, created_at)
                VALUES (?, '1', 1, '테스트 정산', '2026-01-01 00:10:00')
                """,
                (int(mini_cursor.lastrowid),),
            )
            main_round_id = int(main_cursor.lastrowid)

        result = bot.close_completed_event(event_id)

        self.assertIsNotNone(result)
        channel_id, embed = result
        self.assertEqual(channel_id, "10")
        rendered = json.dumps(embed.to_dict(), ensure_ascii=False)
        self.assertIn("이벤트 종료", rendered)
        self.assertIn("예정된 게임이 모두 종료", rendered)
        self.assertIn("수고하셨습니다", rendered)
        with bot.db() as conn:
            event_row = conn.execute(
                "SELECT status, closed_at FROM event_sessions WHERE id = ?", (event_id,)
            ).fetchone()
        self.assertEqual(event_row["status"], "closed")
        self.assertIsNotNone(event_row["closed_at"])
        self.assertIsNone(bot.close_completed_event(event_id))

    def test_unresolved_cutting_does_not_finish_event(self) -> None:
        with bot.db() as conn:
            event_cursor = conn.execute(
                """
                INSERT INTO event_sessions (status, channel_id, created_at)
                VALUES ('active', '10', '2026-01-01 00:00:00')
                """
            )
            event_id = int(event_cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at,
                     closed_at, event_session_id)
                VALUES ('cutting', '세공', '세공', 'closed', '10', '{}',
                        '2026-01-01 00:00:00', '2026-01-01 00:05:00', ?)
                """,
                (event_id,),
            )

        self.assertIsNone(bot.close_completed_event(event_id))


if __name__ == "__main__":
    unittest.main()
