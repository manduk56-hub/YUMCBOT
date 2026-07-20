from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


class ActivityLogTests(unittest.TestCase):
    """활동 로그는 '현재 상태' 테이블이 덮어쓰는 이력을 붙잡는 게 목적이다.

    그래서 여기 테스트는 대부분 '상태 테이블에서는 사라졌지만 로그에는 남았는가'를 본다.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(bot, "DB_PATH", Path(self.temp_dir.name) / "test.sqlite3")
        self.db_patch.start()
        bot.init_db()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def logs(self, category: str | None = None, action: str | None = None) -> list[dict]:
        query = "SELECT * FROM activity_logs"
        clauses, params = [], []
        if category:
            clauses.append("category = ?")
            params.append(category)
        if action:
            clauses.append("action = ?")
            params.append(action)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id ASC"
        with bot.db() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def add_user(self, user_id: str, display_name: str, score: int = 0) -> None:
        with bot.db() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, display_name, total_score, created_at, updated_at)
                VALUES (?, ?, ?, '2026-01-01 00:00:00', '2026-01-01 00:00:00')
                """,
                (user_id, display_name, score),
            )

    def open_event(self, status: str = "recruiting") -> int:
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO event_sessions (status, channel_id, created_at)
                VALUES (?, '10', '2026-01-01 00:00:00')
                """,
                (status,),
            )
            return int(cursor.lastrowid)

    def fake_interaction(self, user_id: int, channel_id: int = 10) -> SimpleNamespace:
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id, bot=False, display_name=f"유저{user_id}"),
            channel_id=channel_id,
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    # --- 스키마 ---

    def test_activity_logs_table_exists_at_current_schema(self) -> None:
        with bot.db() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(activity_logs)")}
        self.assertEqual(
            columns,
            {
                "id",
                "created_at",
                "category",
                "action",
                "event_session_id",
                "round_id",
                "actor_user_id",
                "target_user_id",
                "payload_json",
            },
        )

    def test_log_activity_serializes_korean_payload_without_escaping(self) -> None:
        with bot.db() as conn:
            bot.log_activity(conn, "event", "open", payload={"제목": "밸런스 게임"})

        entry = self.logs()[0]
        self.assertEqual(entry["payload"], {"제목": "밸런스 게임"})
        self.assertIn("밸런스 게임", entry["payload_json"])

    def test_log_activity_failure_does_not_break_caller(self) -> None:
        """로깅 실패가 게임 진행을 막으면 안 된다."""
        with bot.db() as conn:
            conn.execute("DROP TABLE activity_logs")
            # 예외가 밖으로 새면 이 호출이 터진다.
            bot.log_activity(conn, "event", "open", payload={"a": 1})

    # --- 참여 추적 ---

    def test_event_cancel_is_recorded_even_though_row_is_deleted(self) -> None:
        event_session_id = self.open_event()
        self.add_user("100", "취소할 사람")
        with bot.db() as conn:
            conn.execute(
                """
                INSERT INTO event_participants (event_session_id, user_id, joined_at)
                VALUES (?, '100', '2026-01-01 00:00:00')
                """,
                (event_session_id,),
            )

        asyncio.run(bot.handle_event_cancel(self.fake_interaction(100), event_session_id))

        with bot.db() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) AS c FROM event_participants WHERE event_session_id = ?",
                (event_session_id,),
            ).fetchone()["c"]
        self.assertEqual(remaining, 0, "참가자 행은 삭제되어야 한다")

        entries = self.logs("participation", "cancel")
        self.assertEqual(len(entries), 1, "행이 지워져도 취소 사실은 로그에 남아야 한다")
        self.assertEqual(entries[0]["actor_user_id"], "100")
        self.assertEqual(entries[0]["event_session_id"], event_session_id)

    def test_event_join_is_recorded_with_participant_count(self) -> None:
        event_session_id = self.open_event()

        asyncio.run(bot.handle_event_join(self.fake_interaction(100), event_session_id))

        entries = self.logs("participation", "join")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["actor_user_id"], "100")
        self.assertEqual(entries[0]["payload"]["participant_count"], 1)

    # --- 투표 변경 이력 ---

    def make_round(self, round_type: str, extra: dict, event_session_id: int | None = None) -> int:
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at, event_session_id)
                VALUES (?, '제목', '질문', 'open', '10', ?, '2026-01-01 00:00:00', ?)
                """,
                (round_type, json.dumps(extra, ensure_ascii=False), event_session_id),
            )
            return int(cursor.lastrowid)

    def test_trust_decision_change_keeps_previous_choice(self) -> None:
        """votes는 choice를 덮어쓴다. 바꾸기 전 값이 로그에 남아야 한다."""
        round_id = self.make_round("trust", {"trust_phase": "decision"})
        with bot.db() as conn:
            conn.execute(
                """
                INSERT INTO trust_pairings (round_id, user_a_id, user_b_id, discussion_thread_id, created_at)
                VALUES (?, '100', '200', '99', '2026-01-01 00:00:00')
                """,
                (round_id,),
            )

        interaction = self.fake_interaction(100, channel_id=99)
        asyncio.run(bot.handle_trust_decision(interaction, round_id, "trust"))
        asyncio.run(bot.handle_trust_decision(interaction, round_id, "betray"))

        with bot.db() as conn:
            final = conn.execute(
                "SELECT choice FROM votes WHERE round_id = ? AND user_id = '100'",
                (round_id,),
            ).fetchone()["choice"]
        self.assertEqual(final, "betray", "votes에는 최종 선택만 남는다")

        entries = self.logs("vote", "trust_decision")
        self.assertEqual([entry["payload"]["choice"] for entry in entries], ["trust", "betray"])
        self.assertEqual(entries[0]["payload"]["previous_choice"], None)
        self.assertEqual(
            entries[1]["payload"]["previous_choice"],
            "trust",
            "덮어쓰기 전 선택이 로그에 남아야 한다",
        )
        self.assertFalse(entries[0]["payload"]["changed"])
        self.assertTrue(entries[1]["payload"]["changed"])

    def test_auto_assigned_vote_is_distinguishable_from_real_choice(self) -> None:
        """마감 시 랜덤 배정된 표가 본인이 고른 표와 섞이면 안 된다."""
        round_id = self.make_round("trust", {"trust_phase": "decision"})
        self.add_user("100", "가")
        self.add_user("200", "나")
        with bot.db() as conn:
            conn.execute(
                """
                INSERT INTO trust_pairings (round_id, user_a_id, user_b_id, created_at)
                VALUES (?, '100', '200', '2026-01-01 00:00:00')
                """,
                (round_id,),
            )
            conn.execute(
                """
                INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                VALUES (?, '100', 'trust', '2026-01-01 00:00:00', '2026-01-01 00:00:00')
                """,
                (round_id,),
            )

        bot.close_round_record(round_id, None)

        auto = self.logs("vote", "auto_assigned")
        self.assertEqual([entry["actor_user_id"] for entry in auto], ["200"])
        self.assertIn(auto[0]["payload"]["choice"], {"trust", "betray"})

    # --- 정산 요약 ---

    def test_round_settlement_summary_records_actual_score_changes(self) -> None:
        event_session_id = self.open_event("active")
        round_id = self.make_round("balance", {}, event_session_id)
        self.add_user("100", "다수")
        self.add_user("200", "다수2")
        self.add_user("300", "소수")
        with bot.db() as conn:
            for user_id, choice in (("100", "A"), ("200", "A"), ("300", "B")):
                conn.execute(
                    """
                    INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                    VALUES (?, ?, ?, '2026-01-01 00:00:00', '2026-01-01 00:00:00')
                    """,
                    (round_id, user_id, choice),
                )

        bot.close_round_record(round_id, None)

        entries = self.logs("score", "round_settled")
        self.assertEqual(len(entries), 1)
        payload = entries[0]["payload"]
        self.assertEqual(payload["vote_count"], 3)
        self.assertEqual(payload["vote_distribution"], {"A": 2, "B": 1})
        # 혼자 소수파를 고른 사람이 +2점을 받는 규칙.
        self.assertEqual(payload["awarded"].get("300"), 2)
        self.assertEqual(entries[0]["round_id"], round_id)
        self.assertEqual(entries[0]["event_session_id"], event_session_id)

    def test_settlement_snapshot_records_bot_version(self) -> None:
        """규칙이 코드에 하드코딩돼 있어도 커밋 SHA로 당시 규칙을 되짚을 수 있어야 한다."""
        round_id = self.make_round("balance", {})
        bot.close_round_record(round_id, None)

        payload = self.logs("score", "round_settled")[0]["payload"]
        self.assertEqual(payload["settings"]["bot_version"], bot.BOT_VERSION)

    def test_cutting_settings_snapshot_captures_tunable_rules(self) -> None:
        """돌세공 확률/점수는 실제로 조정된 이력이 있다. 값으로 남아야 한다."""
        snapshot = bot.game_settings_snapshot("cutting")
        self.assertEqual(snapshot["reward_points"], bot.CUTTING_REWARD_POINTS)
        self.assertEqual(snapshot["success_rates"], bot.CUTTING_SUCCESS_RATES)

    # --- 러시안룰렛 ---

    def test_each_shot_is_logged_separately(self) -> None:
        """shots_fired는 카운터라 누가 몇 번째를 쐈는지 남지 않는다."""
        self.add_user("100", "도전자", 10)
        self.add_user("200", "상대", 10)
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO russian_roulette_games (
                    guild_id, channel_id, challenger_id, target_id,
                    challenger_name, target_name, status, challenger_bet, target_bet,
                    current_player_id, live_chamber, shots_fired, created_at
                )
                VALUES ('1', '10', '100', '200', '도전자', '상대', 'playing', 3, 3,
                        '100', 2, 0, '2026-01-01 00:00:00')
                """
            )
            game_id = int(cursor.lastrowid)

            # live_chamber=2 이므로 0번, 1번 격발은 빈 탄, 2번째에 발사된다.
            bot.pull_roulette_trigger(conn, game_id, "100")
            bot.pull_roulette_trigger(conn, game_id, "200")
            bot.pull_roulette_trigger(conn, game_id, "100")

        shots = self.logs("roulette", "shot")
        self.assertEqual([entry["payload"]["shot_index"] for entry in shots], [0, 1, 2])
        self.assertEqual([entry["actor_user_id"] for entry in shots], ["100", "200", "100"])
        self.assertEqual(
            [entry["payload"]["result"] for entry in shots],
            ["blank", "blank", "fired"],
        )
        self.assertEqual(shots[-1]["payload"]["winner_id"], "200")
        self.assertEqual(shots[-1]["payload"]["loser_id"], "100")
        self.assertEqual(shots[-1]["payload"]["pot"], 6)


if __name__ == "__main__":
    unittest.main()
