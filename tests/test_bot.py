from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot


class BotRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(bot, "DB_PATH", Path(self.temp_dir.name) / "test.sqlite3")
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

    def test_score_message_uses_plain_display_name(self) -> None:
        message = bot.format_score_message("테스터", 7)
        self.assertEqual(message, "테스터 현재 점수: **7점**")
        self.assertNotIn("<@", message)

    def test_ranking_includes_only_users_with_game_participation(self) -> None:
        self.add_user("100", "이벤트 봇", 99)
        self.add_user("200", "미참가자", 8)
        self.add_user("300", "참가자", 7)
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, extra_json, created_at)
                VALUES ('balance', '테스트', '선택', 'closed', '{}', '2026-01-01 00:00:00')
                """
            )
            conn.execute(
                """
                INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                VALUES (?, '300', 'A', '2026-01-01 00:00:00', '2026-01-01 00:00:00')
                """,
                (int(cursor.lastrowid),),
            )

        with bot.db() as conn:
            rows = bot.fetch_ranking_rows(conn, "100")

        self.assertEqual([row["user_id"] for row in rows], ["300"])

    def test_sand_registration_transitions_to_playing(self) -> None:
        self.add_user("1", "참가자1", 0)
        self.add_user("2", "참가자2", 0)
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('sand', '모래성', '모래성', 'open', '10', ?, '2026-01-01 00:00:00')
                """,
                (json.dumps({"sand_phase": "registration"}),),
            )
            round_id = int(cursor.lastrowid)
            conn.executemany(
                "INSERT INTO trust_registrations (round_id, user_id, registered_at) VALUES (?, ?, ?)",
                [(round_id, "1", "2026-01-01 00:00:00"), (round_id, "2", "2026-01-01 00:00:01")],
            )

        transition = bot.start_sand_game(round_id)

        self.assertIsNotNone(transition)
        self.assertEqual(transition["state"], "playing")
        self.assertTrue(transition["view"].children)
        with bot.db() as conn:
            row = conn.execute("SELECT extra_json FROM rounds WHERE id = ?", (round_id,)).fetchone()
        self.assertEqual(json.loads(row["extra_json"])["sand_phase"], "playing")

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
        self.assertIn("투표 참여", rendered)
        for removed in ("토론 발언", "마감 방식", "진행 시간", "예정 마감"):
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

    def test_sand_rules_match_requested_settlements(self) -> None:
        embed = bot.build_sand_registration_embed(1, "모래성", 2)
        rendered = json.dumps(embed.to_dict(), ensure_ascii=False)
        for expected in (
            "둘 다 포기 선택 시: 서로 -1점",
            "덜기 성공: A는 -1점, B는 +2점",
            "무너짐: B는 -1점, A는 +1점",
            "서로 덜기를 하다 무너진 경우: 그 턴의 사람이 -1점",
        ):
            self.assertIn(expected, rendered)


if __name__ == "__main__":
    unittest.main()
