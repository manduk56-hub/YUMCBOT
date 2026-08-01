from __future__ import annotations

import asyncio
import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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

    def test_finalization_orders_by_score_only_with_shared_rank(self) -> None:
        """참여·승리 횟수는 최종 순위에 반영하지 않는다 — 총점이 같으면 같은 순위를 공유한다."""
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
                [("200", 3, 1), ("300", 2, 2), ("400", 3, 2), ("500", 3, 2)],
            )

            finalized = bot.finalize_season_rankings(conn, "100", "999")
            # 넷 다 총점이 10으로 같으므로 참여·승리 차이와 무관하게 같은 1위를 공유한다.
            # /점수최적화가 참여·승리로 나중에 풀 수 있도록 두 값 자체는 그대로 저장된다.
            self.assertEqual(
                [row["user_id"] for row in finalized],
                ["200", "300", "400", "500"],
            )
            self.assertEqual([row["final_rank"] for row in finalized], [1, 1, 1, 1])
            self.assertEqual(
                [(row["participation_count"], row["win_count"]) for row in finalized],
                [(3, 1), (2, 2), (3, 2), (3, 2)],
            )
            self.assertTrue(bot.season_is_closed(conn))
            self.assertEqual(
                [row["user_id"] for row in bot.fetch_ranking_rows(conn, "100")],
                ["200", "300", "400", "500"],
            )

        with bot.db() as conn:
            persisted = bot.fetch_final_ranking_rows(conn)
        self.assertEqual([row["user_id"] for row in persisted], ["200", "300", "400", "500"])

    def test_assign_competition_ranks_shares_rank_and_skips_next(self) -> None:
        """3등이 동점이면 1·2·3·3등 다음은 5등으로 건너뛴다 (표준 경쟁 순위)."""
        rows = [
            {"total_score": 30},
            {"total_score": 20},
            {"total_score": 10},
            {"total_score": 10},
            {"total_score": 5},
        ]
        ranks = [rank for _, rank in bot.assign_competition_ranks(rows)]
        self.assertEqual(ranks, [1, 2, 3, 3, 5])

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
        self.assertIn("`/시즌종료`", embed.description)
        self.assertEqual(
            [item.custom_id for item in view.children],
            ["eventbot:seasonend:confirm", "eventbot:seasonend:cancel"],
        )
        self.assertEqual(
            [item.label for item in view.children], ["시즌종료", "시즌종료 취소"]
        )

    def test_closed_season_command_rebriefs_persisted_final_ranking(self) -> None:
        self.add_user("200", "첫째", 9)
        self.add_user("300", "둘째", 7)
        with bot.db() as conn:
            conn.execute(
                """
                UPDATE season_state
                SET status = 'closed', closed_at = '2026-01-02', closed_by_user_id = '999'
                WHERE singleton_id = 1
                """
            )
            conn.executemany(
                """
                INSERT INTO final_rankings (
                    user_id, final_rank, display_name, total_score,
                    participation_count, win_count, finalized_at
                ) VALUES (?, ?, ?, ?, ?, ?, '2026-01-02')
                """,
                [
                    ("200", 1, "첫째", 9, 4, 3),
                    ("300", 2, "둘째", 7, 5, 2),
                ],
            )
            before = [tuple(row) for row in bot.fetch_final_ranking_rows(conn)]

        interaction = SimpleNamespace(
            guild=object(),
            response=SimpleNamespace(send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        asyncio.run(bot.ranking.season_end_command(interaction))

        summary = interaction.response.send_message.await_args.kwargs["embed"]
        self.assertEqual(summary.title, "📌 시즌 종료 · 최종 순위 재브리핑")
        self.assertIn("재계산하거나 재추첨하지 않습니다", summary.description)
        interaction.followup.send.assert_awaited_once()
        ranking_embed = interaction.followup.send.await_args.kwargs["embed"]
        self.assertEqual(ranking_embed.title, "🏆 시즌 최종 순위")
        self.assertIn("**1위** · 첫째 · **9점**", ranking_embed.description)
        self.assertIn("**2위** · 둘째 · **7점**", ranking_embed.description)

        with bot.db() as conn:
            after = [tuple(row) for row in bot.fetch_final_ranking_rows(conn)]
        self.assertEqual(after, before)

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

    def test_final_ranking_lines_show_shared_rank_for_ties(self) -> None:
        rows = [
            {"final_rank": 1, "display_name": "공동 1등 A", "total_score": 9},
            {"final_rank": 1, "display_name": "공동 1등 B", "total_score": 9},
            {"final_rank": 3, "display_name": "3등", "total_score": 5},
        ]
        self.assertEqual(
            bot.format_final_ranking_lines(rows),
            [
                "**1위** · 공동 1등 A · **9점**",
                "**1위** · 공동 1등 B · **9점**",
                "**3위** · 3등 · **5점**",
            ],
        )

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

    def test_score_optimization_command_is_admin_only(self) -> None:
        command = bot.bot.tree.get_command("점수최적화")
        self.assertIsNotNone(command)
        self.assertEqual(command.default_permissions.value, 0)
        self.assertEqual(len(command.checks), 1)

    def test_score_optimization_rejects_open_season(self) -> None:
        interaction = SimpleNamespace(
            guild=object(),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        asyncio.run(bot.ranking.score_optimization_command(interaction))

        interaction.response.send_message.assert_awaited_once()
        args, kwargs = interaction.response.send_message.call_args
        self.assertIn("시즌이 아직 종료되지 않았습니다", args[0])
        self.assertTrue(kwargs["ephemeral"])

    def test_score_optimization_breaks_ties_by_participation_then_wins_then_lot(self) -> None:
        """총점 동점 그룹만 참여→승리→완전 동률 추첨 순으로 풀고, final_rankings는 그대로 둔다."""
        for user_id, name in (
            ("200", "참여4 승3 A"),
            ("210", "참여4 승3 B"),
            ("300", "참여4 승2"),
            ("400", "참여3 승5"),
            ("500", "5등 단독"),
        ):
            self.add_user(user_id, name, 9 if user_id != "500" else 5)
        with bot.db() as conn:
            conn.execute(
                "UPDATE season_state SET status = 'closed', closed_at = '2026-01-02' "
                "WHERE singleton_id = 1"
            )
            conn.executemany(
                """
                INSERT INTO final_rankings (
                    user_id, final_rank, display_name, total_score,
                    participation_count, win_count, finalized_at
                ) VALUES (?, ?, ?, ?, ?, ?, '2026-01-02')
                """,
                [
                    # 200/210은 참여·승리까지 완전 동률이라 추첨이 필요하다.
                    ("200", 1, "참여4 승3 A", 9, 4, 3),
                    ("210", 1, "참여4 승3 B", 9, 4, 3),
                    # 300은 참여는 200/210과 같지만 승수가 낮아 추첨 없이 그 다음.
                    ("300", 1, "참여4 승2", 9, 4, 2),
                    # 400은 승수는 가장 높지만 참여가 우선 기준이라 이 그룹에서 꼴찌.
                    ("400", 1, "참여3 승5", 9, 3, 5),
                    # 총점 자체가 달라 별도 그룹 — 위 동점 여부와 무관하게 마지막.
                    ("500", 5, "5등 단독", 5, 2, 1),
                ],
            )

        class ReverseRng:
            @staticmethod
            def shuffle(values) -> None:
                values.reverse()

        with bot.db() as conn:
            optimized = bot.optimize_scores(conn, "999", rng=ReverseRng())

        self.assertEqual(
            [row["user_id"] for row in optimized],
            ["210", "200", "300", "400", "500"],
        )
        self.assertEqual([row["rank"] for row in optimized], [1, 2, 3, 4, 5])

        with bot.db() as conn:
            unchanged = bot.fetch_final_ranking_rows(conn)
        self.assertEqual(
            [row["final_rank"] for row in unchanged], [1, 1, 1, 1, 5]
        )

    def test_optimize_scores_rolls_back_partial_insert_on_write_time_conflict(self) -> None:
        """score_optimization_is_done()의 사전 확인은 트랜잭션 밖 SELECT라 두 실행이
        동시에 통과할 수 있다. 그 레이스를 흉내 내려고 사전 확인만 강제로 통과시키고,
        먼저 커밋된 것처럼 2등 자리에 행을 미리 심어 둔다. 1등 삽입이 먼저 성공하더라도
        2등에서 PK 충돌이 나면 SAVEPOINT 롤백으로 1등 삽입까지 함께 사라져야 하고,
        호출자에게는 원시 IntegrityError가 아니라 ScoreOptimizationAlreadyDoneError가 가야 한다."""
        self.add_user("300", "1등", 9)
        self.add_user("200", "2등", 5)
        with bot.db() as conn:
            conn.execute(
                "UPDATE season_state SET status = 'closed', closed_at = '2026-01-02' "
                "WHERE singleton_id = 1"
            )
            conn.executemany(
                """
                INSERT INTO final_rankings (
                    user_id, final_rank, display_name, total_score,
                    participation_count, win_count, finalized_at
                ) VALUES (?, ?, ?, ?, ?, ?, '2026-01-02')
                """,
                [
                    ("300", 1, "1등", 9, 4, 3),
                    ("200", 2, "2등", 5, 4, 3),
                ],
            )
            # 다른 실행이 2등 자리를 먼저 커밋해 둔 상황을 흉내 낸다.
            conn.execute(
                """
                INSERT INTO score_optimizations (
                    user_id, rank, display_name, total_score,
                    participation_count, win_count, draw_order, optimized_at
                ) VALUES ('200', 2, '2등', 5, 4, 3, 1, '2026-01-02')
                """
            )

        with bot.db() as conn:
            with patch.object(bot.ranking, "score_optimization_is_done", return_value=False):
                with self.assertRaises(bot.ScoreOptimizationAlreadyDoneError):
                    bot.optimize_scores(conn, "999")

        with bot.db() as conn:
            rows = bot.fetch_score_optimization_rows(conn)
        self.assertEqual([row["user_id"] for row in rows], ["200"])

    def test_score_optimization_command_replays_persisted_result(self) -> None:
        self.add_user("200", "첫째", 9)
        with bot.db() as conn:
            conn.execute(
                "UPDATE season_state SET status = 'closed', closed_at = '2026-01-02' "
                "WHERE singleton_id = 1"
            )
            conn.execute(
                """
                INSERT INTO final_rankings (
                    user_id, final_rank, display_name, total_score,
                    participation_count, win_count, finalized_at
                ) VALUES ('200', 1, '첫째', 9, 4, 3, '2026-01-02')
                """
            )
            conn.execute(
                """
                INSERT INTO score_optimizations (
                    user_id, rank, display_name, total_score,
                    participation_count, win_count, draw_order, optimized_at
                ) VALUES ('200', 1, '첫째', 9, 4, 3, 1, '2026-01-02')
                """
            )
            before = [tuple(row) for row in bot.fetch_score_optimization_rows(conn)]

        interaction = SimpleNamespace(
            guild=object(),
            response=SimpleNamespace(send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        asyncio.run(bot.ranking.score_optimization_command(interaction))

        summary = interaction.response.send_message.await_args.kwargs["embed"]
        self.assertEqual(summary.title, "📌 점수 최적화 · 재브리핑")
        self.assertIn("재추첨하지 않습니다", summary.description)
        ranking_embed = interaction.followup.send.await_args.kwargs["embed"]
        self.assertIn("**1위** · 첫째 · **9점**", ranking_embed.description)

        with bot.db() as conn:
            after = [tuple(row) for row in bot.fetch_score_optimization_rows(conn)]
        self.assertEqual(after, before)

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
