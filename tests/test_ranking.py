from __future__ import annotations

import asyncio
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from yumcbot import config


class RankingTests(unittest.TestCase):
    """점수 조회·랭킹 공개·백업(yumcbot/ranking.py) 회귀 테스트."""

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

    def test_my_score_message_shows_only_scores(self) -> None:
        message = bot.format_my_score_message(7, 5)
        self.assertEqual(message, "3등 점수: **7점**\n내 점수: **5점**")
        self.assertNotIn("<@", message)

    def test_my_score_message_without_third_place(self) -> None:
        self.assertIn("아직 없음", bot.format_my_score_message(None, 2))

    def test_third_place_is_fixed_to_one_user_when_tied(self) -> None:
        """3등이 동점이어도 항상 같은 한 명이 3등으로 뽑혀야 한다.

        점수가 그대로인 한 몇 번을 조회해도 같은 유저·같은 점수가 나와야 하고,
        그래야 '3등이 두 명이구나'를 유저가 눈치챌 수 없다.
        """
        self.add_user("100", "봇", 0)
        self.add_user("200", "1등", 9)
        self.add_user("300", "공동 3등 A", 4)
        self.add_user("400", "2등", 7)
        self.add_user("500", "공동 3등 B", 4)
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, extra_json, created_at)
                VALUES ('balance', '테스트', '선택', 'closed', '{}', '2026-01-01 00:00:00')
                """
            )
            conn.executemany(
                """
                INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                VALUES (?, ?, 'A', '2026-01-01 00:00:00', '2026-01-01 00:00:00')
                """,
                [(int(cursor.lastrowid), user_id) for user_id in ("200", "300", "400", "500")],
            )

        thirds = []
        for _ in range(5):
            with bot.db() as conn:
                rows = bot.fetch_ranking_rows(conn, "100")
            thirds.append((rows[2]["user_id"], rows[2]["total_score"]))

        self.assertEqual(thirds, [("300", 4)] * 5)

    def test_third_place_stays_fixed_after_unrelated_score_change(self) -> None:
        """동점자의 updated_at이 갱신돼도 3등이 다른 사람으로 바뀌면 안 된다."""
        self.add_user("100", "봇", 0)
        self.add_user("300", "공동 1등 A", 4)
        self.add_user("500", "공동 1등 B", 4)
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, extra_json, created_at)
                VALUES ('balance', '테스트', '선택', 'closed', '{}', '2026-01-01 00:00:00')
                """
            )
            round_id = int(cursor.lastrowid)
            conn.executemany(
                """
                INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                VALUES (?, ?, 'A', '2026-01-01 00:00:00', '2026-01-01 00:00:00')
                """,
                [(round_id, user_id) for user_id in ("300", "500")],
            )
            before = [row["user_id"] for row in bot.fetch_ranking_rows(conn, "100")]

            # 500번이 +1 되었다가 -1 되어 같은 점수로 돌아온다 (updated_at만 바뀐다).
            bot.add_score(conn, "500", "공동 1등 B", 1, "테스트", round_id)
            bot.add_score(conn, "500", "공동 1등 B", -1, "테스트", round_id)
            after = [row["user_id"] for row in bot.fetch_ranking_rows(conn, "100")]

        self.assertEqual(before, after)

    def test_win_count_checks_each_game_score_without_cross_game_sum(self) -> None:
        """게임별 +2/-3/0은 합계 -1이 아니라 양수였던 게임 1개로 계산한다."""
        self.add_user("200", "참가자", -1)
        with bot.db() as conn:
            event = conn.execute(
                """
                INSERT INTO event_sessions (status, channel_id, created_at, closed_at)
                VALUES ('closed', '10', '2026-01-01', '2026-01-01')
                """
            )
            event_id = int(event.lastrowid)
            conn.execute(
                """
                INSERT INTO event_participants (event_session_id, user_id, joined_at)
                VALUES (?, '200', '2026-01-01')
                """,
                (event_id,),
            )
            round_ids = []
            for index in range(3):
                cursor = conn.execute(
                    """
                    INSERT INTO rounds (
                        type, title, question, status, extra_json,
                        created_at, closed_at, event_session_id
                    ) VALUES (
                        'balance', ?, '선택', 'closed', '{}',
                        '2026-01-01', '2026-01-01', ?
                    )
                    """,
                    (f"게임 {index + 1}", event_id),
                )
                round_ids.append(int(cursor.lastrowid))
            conn.executemany(
                """
                INSERT INTO score_logs (round_id, user_id, delta, reason, created_at)
                VALUES (?, '200', ?, '게임 최종 정산', '2026-01-01')
                """,
                [(round_ids[0], 2), (round_ids[1], -3), (round_ids[2], 0)],
            )

            rows = bot.fetch_ranking_stat_rows(conn, "100")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["participation_count"], 3)
        self.assertEqual(rows[0]["win_count"], 1)

    def test_finalization_orders_by_score_participation_wins_then_draw(self) -> None:
        """참여 장려가 승수보다 앞서고, 세 값이 모두 같은 그룹만 한 번 추첨한다."""
        for user_id, name in (
            ("100", "봇"),
            ("200", "참여 우선"),
            ("300", "승수는 많지만 참여 적음"),
            ("400", "완전 동률 A"),
            ("500", "완전 동률 B"),
        ):
            self.add_user(user_id, name, 10 if user_id != "100" else 0)

        with bot.db() as conn:
            round_ids: list[int] = []
            for event_index in range(3):
                event = conn.execute(
                    """
                    INSERT INTO event_sessions (status, channel_id, created_at, closed_at)
                    VALUES ('closed', '10', ?, ?)
                    """,
                    (
                        f"2026-01-0{event_index + 1} 00:00:00",
                        f"2026-01-0{event_index + 1} 01:00:00",
                    ),
                )
                event_id = int(event.lastrowid)
                participants = ["200", "400", "500"]
                if event_index < 2:
                    participants.append("300")
                conn.executemany(
                    """
                    INSERT INTO event_participants (event_session_id, user_id, joined_at)
                    VALUES (?, ?, '2026-01-01 00:00:00')
                    """,
                    [(event_id, user_id) for user_id in participants],
                )
                round_cursor = conn.execute(
                    """
                    INSERT INTO rounds (
                        type, title, question, status, extra_json,
                        created_at, closed_at, event_session_id
                    ) VALUES (
                        'balance', ?, '선택', 'closed', '{}',
                        '2026-01-01 00:00:00', '2026-01-01 01:00:00', ?
                    )
                    """,
                    (f"라운드 {event_index + 1}", event_id),
                )
                round_ids.append(int(round_cursor.lastrowid))

            # 1라운드: 전원 승리, 2라운드: 300/400/500만 승리, 3라운드: 승자 없음.
            for user_id in ("200", "300", "400", "500"):
                conn.execute(
                    """
                    INSERT INTO score_logs (round_id, user_id, delta, reason, created_at)
                    VALUES (?, ?, 1, '테스트 승리', '2026-01-01 01:00:00')
                    """,
                    (round_ids[0], user_id),
                )
            for user_id in ("300", "400", "500"):
                conn.execute(
                    """
                    INSERT INTO score_logs (round_id, user_id, delta, reason, created_at)
                    VALUES (?, ?, 2, '테스트 승리', '2026-01-02 01:00:00')
                    """,
                    (round_ids[1], user_id),
                )

            stats = bot.fetch_ranking_stat_rows(conn, "100")
            self.assertEqual(
                [
                    (row["user_id"], row["participation_count"], row["win_count"])
                    for row in stats
                ],
                [("400", 3, 2), ("500", 3, 2), ("200", 3, 1), ("300", 2, 2)],
            )

            class ReverseRng:
                @staticmethod
                def shuffle(values) -> None:
                    values.reverse()

            finalized = bot.finalize_season_rankings(
                conn, "100", "999", rng=ReverseRng()
            )
            self.assertEqual(
                [row["user_id"] for row in finalized],
                ["500", "400", "200", "300"],
            )
            self.assertEqual(
                [row["final_rank"] for row in finalized], [1, 2, 3, 4]
            )
            self.assertTrue(bot.season_is_closed(conn))
            self.assertEqual(
                [row["user_id"] for row in bot.fetch_ranking_rows(conn, "100")],
                ["500", "400", "200", "300"],
            )

        with bot.db() as conn:
            persisted = bot.fetch_final_ranking_rows(conn)
        self.assertEqual([row["user_id"] for row in persisted], ["500", "400", "200", "300"])

    def test_finalization_rejects_an_open_event(self) -> None:
        with bot.db() as conn:
            conn.execute(
                """
                INSERT INTO event_sessions (status, channel_id, created_at)
                VALUES ('recruiting', '10', '2026-01-01 00:00:00')
                """
            )
            with self.assertRaises(bot.SeasonFinalizationBlockedError) as raised:
                bot.finalize_season_rankings(conn, "100", "999")
        self.assertIn("진행 중인 이벤트", str(raised.exception))

    def test_finalized_ranking_cannot_be_drawn_again(self) -> None:
        self.add_user("200", "참가자", 3)
        with bot.db() as conn:
            round_cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, extra_json, created_at, closed_at)
                VALUES ('balance', '완료', '선택', 'closed', '{}', '2026-01-01', '2026-01-01')
                """
            )
            conn.execute(
                """
                INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                VALUES (?, '200', 'A', '2026-01-01', '2026-01-01')
                """,
                (int(round_cursor.lastrowid),),
            )
            bot.finalize_season_rankings(conn, "100", "999")
            with self.assertRaises(bot.SeasonAlreadyClosedError):
                bot.finalize_season_rankings(conn, "100", "999")

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

    def test_public_ranking_lines_show_users_and_scores(self) -> None:
        rows = [
            {"user_id": "200", "display_name": "1등", "total_score": 9},
            {"user_id": "300", "display_name": "2등", "total_score": 7},
        ]

        self.assertEqual(
            bot.format_public_ranking_lines(rows),
            ["1. <@200> - **9점**", "2. <@300> - **7점**"],
        )
        self.assertEqual(
            bot.format_public_ranking_lines([rows[1]], start_rank=2),
            ["2. <@300> - **7점**"],
        )

    def test_season_end_confirmation_explains_irreversible_lock(self) -> None:
        embed = bot.season_end_confirmation_embed()
        view = bot.season_end_confirmation_view()

        self.assertIn("시즌을 종료", embed.title)
        self.assertIn("더 이상", embed.description)
        self.assertIn("`/점수`", embed.description)
        self.assertEqual(
            [item.custom_id for item in view.children],
            ["eventbot:seasonend:confirm", "eventbot:seasonend:cancel"],
        )
        self.assertEqual(
            [item.label for item in view.children], ["시즌종료", "시즌종료 취소"]
        )

    def test_final_ranking_lines_show_unique_rank_name_and_score(self) -> None:
        rows = [
            {"final_rank": 1, "display_name": "첫째", "total_score": 9},
            {"final_rank": 2, "display_name": "둘째", "total_score": 7},
        ]
        self.assertEqual(
            bot.format_final_ranking_lines(rows),
            ["**1위** · 첫째 · **9점**", "**2위** · 둘째 · **7점**"],
        )
        embeds = bot.build_final_ranking_embeds(rows)
        self.assertEqual(len(embeds), 1)
        self.assertIn("첫째", embeds[0].description)
        self.assertIn("둘째", embeds[0].description)

    def test_placeholder_name_is_refetched_before_finalization(self) -> None:
        self.add_user("200", "User 200", 3)
        with bot.db() as conn:
            round_cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, extra_json, created_at, closed_at)
                VALUES ('balance', '완료', '선택', 'closed', '{}', '2026-01-01', '2026-01-01')
                """
            )
            conn.execute(
                """
                INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                VALUES (?, '200', 'A', '2026-01-01', '2026-01-01')
                """,
                (int(round_cursor.lastrowid),),
            )

        class FakeMember:
            display_name = "실제 이름"

        class FakeGuild:
            @staticmethod
            async def fetch_member(user_id: int):
                if user_id != 200:
                    raise AssertionError(user_id)
                return FakeMember()

        class FakeClient:
            @staticmethod
            async def fetch_user(user_id: int):
                raise AssertionError(user_id)

        refreshed = asyncio.run(
            bot.refresh_placeholder_user_names(FakeGuild(), FakeClient(), "100")
        )
        with bot.db() as conn:
            name = conn.execute(
                "SELECT display_name FROM users WHERE user_id = '200'"
            ).fetchone()["display_name"]
        self.assertEqual(refreshed, 1)
        self.assertEqual(name, "실제 이름")

    def test_season_end_command_is_admin_only(self) -> None:
        command = bot.bot.tree.get_command("시즌종료")
        self.assertIsNotNone(command)
        self.assertEqual(command.default_permissions.value, 0)
        self.assertEqual(len(command.checks), 1)

    def test_ranking_command_is_admin_only(self) -> None:
        command = bot.bot.tree.get_command("랭킹")

        self.assertIsNotNone(command)
        self.assertEqual(command.default_permissions.value, 0)
        self.assertEqual(len(command.checks), 1)
        rank_parameter = command.parameters[0]
        self.assertEqual(rank_parameter.name, "등수")
        self.assertFalse(rank_parameter.required)
        self.assertEqual(rank_parameter.min_value, 1)
        self.assertEqual(rank_parameter.max_value, 1000)

    def test_anonymous_ranking_hides_all_user_information(self) -> None:
        rows = [
            {"user_id": "200", "display_name": "1등 이름", "total_score": 9},
            {"user_id": "300", "display_name": "2등 이름", "total_score": 7},
        ]

        lines = bot.format_anonymous_ranking_lines(rows)
        self.assertEqual(lines, ["1등 - **9점**", "2등 - **7점**"])
        self.assertNotIn("200", "".join(lines))
        self.assertNotIn("300", "".join(lines))
        self.assertNotIn("이름", "".join(lines))
        self.assertEqual(
            bot.format_anonymous_ranking_lines([rows[1]], start_rank=2),
            ["2등 - **7점**"],
        )

    def test_anonymous_ranking_command_is_admin_only(self) -> None:
        command = bot.bot.tree.get_command("익명랭킹")

        self.assertIsNotNone(command)
        self.assertEqual(command.default_permissions.value, 0)
        self.assertEqual(len(command.checks), 1)
        rank_parameter = command.parameters[0]
        self.assertEqual(rank_parameter.name, "등수")
        self.assertFalse(rank_parameter.required)
        self.assertEqual(rank_parameter.min_value, 1)
        self.assertEqual(rank_parameter.max_value, 1000)

    def test_admin_score_report_records_user_names_and_scores(self) -> None:
        report_path = Path(self.temp_dir.name) / "score_report.csv"
        rows = [
            {
                "user_id": "200",
                "display_name": "첫번째 참가자",
                "total_score": 7,
                "updated_at": "2026-01-01 00:00:00",
            },
            {
                "user_id": "300",
                "display_name": "두번째 참가자",
                "total_score": 4,
                "updated_at": "2026-01-01 00:00:01",
            },
        ]

        bot.write_admin_score_report(report_path, rows)

        with report_path.open("r", encoding="utf-8-sig", newline="") as report_file:
            report_rows = list(csv.DictReader(report_file))
        self.assertEqual(report_rows[0]["display_name"], "첫번째 참가자")
        self.assertEqual(report_rows[0]["total_score"], "7")
        self.assertEqual(report_rows[1]["user_id"], "300")


if __name__ == "__main__":
    unittest.main()
