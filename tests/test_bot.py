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

    def test_event_join_view_has_join_and_cancel_buttons(self) -> None:
        view = bot.event_join_view(12, 3)

        self.assertEqual([item.label for item in view.children], ["참여하기 (3명)", "참여 취소하기"])
        self.assertEqual(
            [item.custom_id for item in view.children],
            ["eventbot:eventjoin:12", "eventbot:eventcancel:12"],
        )
        self.assertEqual(view.children[1].style, bot.discord.ButtonStyle.danger)

    def test_roulette_views_have_persistent_custom_ids(self) -> None:
        challenge = bot.roulette_challenge_view(7)
        betting = bot.roulette_betting_view(7)
        playing = bot.roulette_play_view(7)

        self.assertEqual(
            [item.custom_id for item in challenge.children],
            [
                "eventbot:rouletteaccept:7",
                "eventbot:roulettedecline:7",
                "eventbot:roulettescore:7",
            ],
        )
        self.assertEqual(
            [item.custom_id for item in betting.children],
            ["eventbot:roulettebet:7", "eventbot:roulettescore:7"],
        )
        self.assertEqual(
            [item.custom_id for item in playing.children],
            ["eventbot:roulettetrigger:7", "eventbot:roulettescore:7"],
        )

    def test_roulette_score_message_shows_current_score_and_escrow(self) -> None:
        self.add_user("1", "도전자", 6)
        game = {
            "challenger_id": "1",
            "target_id": "2",
            "challenger_bet": 4,
            "target_bet": 3,
            "status": "playing",
        }

        with bot.db() as conn:
            message = bot.roulette_player_score_message(conn, game, "1")

        self.assertEqual(
            message,
            "현재 보유 점수: **6점**\n러시안룰렛 예치 점수: **4점**",
        )

    def test_roulette_score_message_rejects_spectators(self) -> None:
        game = {
            "challenger_id": "1",
            "target_id": "2",
            "challenger_bet": None,
            "target_bet": None,
            "status": "betting",
        }

        with bot.db() as conn, self.assertRaisesRegex(ValueError, "플레이어만"):
            bot.roulette_player_score_message(conn, game, "3")

    def test_roulette_escrows_bets_and_awards_the_whole_pot(self) -> None:
        self.add_user("1", "도전자", 10)
        self.add_user("2", "상대", 8)
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO russian_roulette_games (
                    guild_id, channel_id, challenger_id, target_id,
                    challenger_name, target_name, status, created_at, expires_at
                )
                VALUES ('10', '20', '1', '2', '도전자', '상대', 'betting', ?, ?)
                """,
                (bot.now_text(), 9_999_999_999),
            )
            game_id = int(cursor.lastrowid)

            state, _ = bot.submit_roulette_bet(conn, game_id, "1", 4)
            self.assertEqual(state, "waiting")
            with patch.object(bot.secrets, "randbelow", side_effect=[0, 2]):
                state, game = bot.submit_roulette_bet(conn, game_id, "2", 3)

            self.assertEqual(state, "started")
            self.assertEqual(game["current_player_id"], "1")
            self.assertEqual(game["live_chamber"], 2)
            scores = {
                row["user_id"]: row["total_score"]
                for row in conn.execute(
                    "SELECT user_id, total_score FROM users WHERE user_id IN ('1', '2')"
                )
            }
            self.assertEqual(scores, {"1": 6, "2": 5})

            self.assertEqual(bot.pull_roulette_trigger(conn, game_id, "1")[0], "blank")
            self.assertEqual(bot.pull_roulette_trigger(conn, game_id, "2")[0], "blank")
            result, finished = bot.pull_roulette_trigger(conn, game_id, "1")
            self.assertEqual(result, "fired")
            self.assertEqual(finished["winner_id"], "2")
            self.assertEqual(finished["loser_id"], "1")
            rendered = json.dumps(bot.build_roulette_embed(finished).to_dict(), ensure_ascii=False)
            self.assertIn("<@2>님이 <@1>님의 **4점**을 빼앗았습니다", rendered)

            final_scores = {
                row["user_id"]: row["total_score"]
                for row in conn.execute(
                    "SELECT user_id, total_score FROM users WHERE user_id IN ('1', '2')"
                )
            }
            self.assertEqual(final_scores, {"1": 6, "2": 12})
            self.assertEqual(sum(final_scores.values()), 18)

    def test_roulette_rejects_a_bet_over_the_current_balance(self) -> None:
        self.add_user("1", "도전자", 2)
        self.add_user("2", "상대", 3)
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO russian_roulette_games (
                    guild_id, channel_id, challenger_id, target_id,
                    challenger_name, target_name, status, created_at, expires_at
                )
                VALUES ('10', '20', '1', '2', '도전자', '상대', 'betting', ?, ?)
                """,
                (bot.now_text(), 9_999_999_999),
            )
            with self.assertRaisesRegex(ValueError, "가진 점수보다 많이"):
                bot.submit_roulette_bet(conn, int(cursor.lastrowid), "1", 3)

    def test_close_time_text_shows_exact_discord_time_without_relative_time(self) -> None:
        rendered = bot.close_time_text(1_800_000_000.9)

        self.assertEqual(rendered, "<t:1800000000:F>")
        self.assertNotIn(":R>", rendered)
        self.assertEqual(bot.close_time_text(None), "수동 마감")

    def test_auto_close_countdown_announces_ten_then_counts_five_to_one(self) -> None:
        warning_message = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        countdown_message = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        channel = SimpleNamespace(
            send=AsyncMock(side_effect=[warning_message, countdown_message])
        )

        with patch.object(bot.time, "time", return_value=990), patch.object(
            bot.asyncio,
            "sleep",
            new=AsyncMock(),
        ):
            asyncio.run(bot.publish_auto_close_countdown(channel, "게임 종료", 1000))

        self.assertEqual(
            [call.args[0] for call in channel.send.await_args_list],
            [
                "⏳ **게임 종료 10초 전입니다.**",
                "⏳ **게임 종료까지 5**",
            ],
        )
        warning_message.edit.assert_not_awaited()
        self.assertEqual(
            [call.kwargs["content"] for call in countdown_message.edit.await_args_list],
            [
                "⏳ **게임 종료까지 4**",
                "⏳ **게임 종료까지 3**",
                "⏳ **게임 종료까지 2**",
                "⏳ **게임 종료까지 1**",
                "🏁 **게임 종료!**",
            ],
        )

    def test_timed_game_boards_include_exact_deadline(self) -> None:
        close_at = 1_800_000_000
        trust_embed = bot.build_trust_decision_embed(
            1,
            "딜레마",
            [("1", "2")],
            None,
            close_at=close_at,
        )
        anonymous_embed = bot.build_anonymous_guide_embed(0, close_at)
        sand_embed = bot.build_sand_pair_embed(
            "모래성",
            {
                "user_a_id": "1",
                "user_b_id": "2",
                "phase": "number_selection",
                "number_a": None,
                "number_b": None,
            },
            {"play_close_at": close_at},
        )

        deadline = "<t:1800000000:F>"
        for embed in (trust_embed, anonymous_embed, sand_embed):
            rendered = json.dumps(embed.to_dict(), ensure_ascii=False)
            self.assertIn(deadline, rendered)
            self.assertNotIn(":R>", rendered)

        self.assertIn(deadline, bot.trust_pair_intro("1", "2", close_at))

    def test_event_cancel_removes_participant_and_refreshes_count(self) -> None:
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO event_sessions (status, channel_id, created_at)
                VALUES ('recruiting', '10', '2026-01-01 00:00:00')
                """
            )
            event_session_id = int(cursor.lastrowid)
            conn.executemany(
                """
                INSERT INTO users (user_id, display_name, total_score, created_at, updated_at)
                VALUES (?, ?, 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00')
                """,
                [("100", "취소할 사람"), ("200", "남을 사람")],
            )
            conn.executemany(
                """
                INSERT INTO event_participants (event_session_id, user_id, joined_at)
                VALUES (?, ?, '2026-01-01 00:00:00')
                """,
                [(event_session_id, "100"), (event_session_id, "200")],
            )

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=100, bot=False),
            response=SimpleNamespace(
                send_message=AsyncMock(),
                edit_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        asyncio.run(bot.handle_event_cancel(interaction, event_session_id))

        with bot.db() as conn:
            participants = conn.execute(
                """
                SELECT user_id FROM event_participants
                WHERE event_session_id = ? ORDER BY user_id
                """,
                (event_session_id,),
            ).fetchall()
        self.assertEqual([row["user_id"] for row in participants], ["200"])
        interaction.response.send_message.assert_not_awaited()
        interaction.response.edit_message.assert_awaited_once()
        refreshed_view = interaction.response.edit_message.await_args.kwargs["view"]
        self.assertEqual(refreshed_view.children[0].label, "참여하기 (1명)")
        interaction.followup.send.assert_awaited_once_with("참여가 취소되었습니다.", ephemeral=True)

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
        self.assertEqual({tuple(sorted(pair)) for pair in transition["pairs"]}, {("1", "2")})
        # 숫자·행동 버튼은 짝별 비공개 방 안에만 있다. 짝수 인원이라 긴급 투입 버튼도 없다.
        self.assertIsNone(transition["view"])
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
        for removed in ("투표 참여", "통계", "토론 발언", "마감 방식", "진행 시간", "예정 마감"):
            self.assertNotIn(removed, rendered)

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

    def test_trust_pairings_schema_stores_private_thread(self) -> None:
        with bot.db() as conn:
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(trust_pairings)").fetchall()
            }

        self.assertIn("discussion_thread_id", columns)

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
        self.assertIn("이벤트 종료 · 최종 결과 요약", rendered)
        self.assertIn(f"#{main_round_id}", rendered)
        self.assertIn("딜레마의 섬", rendered)
        self.assertIn("밸런스 게임", rendered)
        self.assertIn("점수 합계 **+1점**", rendered)
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

    def test_sand_rules_match_requested_settlements(self) -> None:
        embed = bot.build_sand_registration_embed(1, "모래성", 2)
        rendered = json.dumps(embed.to_dict(), ensure_ascii=False)
        for expected in (
            "둘 다 포기 선택 시: **서로 -1점**",
            "덜기 성공: A는 **-1점**, B는 **+2점**",
            "무너짐: B는 **-1점**, A는 **+1점**",
            "서로 덜기를 하다 무너진 경우: 그 턴의 사람이 **-1점**",
        ):
            self.assertIn(expected, rendered)

    # --- 밸런스 -------------------------------------------------------------

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

    def score_of(self, user_id: str) -> int:
        with bot.db() as conn:
            return int(
                conn.execute(
                    "SELECT total_score FROM users WHERE user_id = ?", (user_id,)
                ).fetchone()["total_score"]
            )

    def test_balance_one_sided_vote_penalizes_every_voter(self) -> None:
        for user_id in ("1", "2", "3"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        round_id = self.create_balance_round([("1", "A"), ("2", "A"), ("3", "A")])

        result = bot.close_round_record(round_id, None)

        self.assertIsNotNone(result)
        for user_id in ("1", "2", "3"):
            self.assertEqual(self.score_of(user_id), -2)
        self.assertIn("-2점", json.dumps(result[1].to_dict(), ensure_ascii=False))

    def test_balance_majority_still_gets_one_point(self) -> None:
        for user_id in ("1", "2", "3"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        round_id = self.create_balance_round([("1", "A"), ("2", "A"), ("3", "B")])

        bot.close_round_record(round_id, None)

        self.assertEqual(self.score_of("1"), 1)
        self.assertEqual(self.score_of("2"), 1)
        self.assertEqual(self.score_of("3"), 0)

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

    # --- 3선 폭탄 게임 ------------------------------------------------------

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

    # --- 상위 3명 -----------------------------------------------------------

    def create_top3_round(self, votes: list[tuple[str, str]]) -> int:
        candidates = [
            {"user_id": "1", "display_name": "1등", "score": 9},
            {"user_id": "2", "display_name": "2등", "score": 8},
            {"user_id": "3", "display_name": "3등", "score": 7},
        ]
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('top3', '견제', '견제', 'open', '10', ?, '2026-01-01 00:00:00')
                """,
                (json.dumps({"candidates": candidates}),),
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

    def test_top3_without_votes_is_void(self) -> None:
        for user_id in ("1", "2", "3"):
            self.add_user(user_id, f"후보{user_id}", 9)
        round_id = self.create_top3_round([])

        result = bot.close_round_record(round_id, None)

        for user_id in ("1", "2", "3"):
            self.assertEqual(self.score_of(user_id), 9)
        self.assertIn("무효", json.dumps(result[1].to_dict(), ensure_ascii=False))

    def test_top3_none_choice_rewards_only_its_voters(self) -> None:
        for user_id in ("1", "2", "3"):
            self.add_user(user_id, f"후보{user_id}", 9)
        for user_id in ("10", "11", "12", "13"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        # 견제 2표 vs 후보 1표 -> 견제 성공.
        # 후보에게 투표한 12번도, 아예 투표하지 않은 13번도 점수를 받지 못한다.
        round_id = self.create_top3_round(
            [("10", bot.TOP3_NONE_CHOICE), ("11", bot.TOP3_NONE_CHOICE), ("12", "1")]
        )

        bot.close_round_record(round_id, None)

        self.assertEqual(self.score_of("10"), 1)
        self.assertEqual(self.score_of("11"), 1)
        self.assertEqual(self.score_of("12"), 0)
        self.assertEqual(self.score_of("13"), 0)
        self.assertEqual(self.score_of("1"), 9)

    def test_top3_majority_hits_most_voted_candidate(self) -> None:
        for user_id in ("1", "2", "3"):
            self.add_user(user_id, f"후보{user_id}", 9)
        self.add_user("10", "참가자", 0)
        round_id = self.create_top3_round([("10", "2")])

        bot.close_round_record(round_id, None)

        self.assertEqual(self.score_of("2"), 8)
        self.assertEqual(self.score_of("1"), 9)

    # --- 유일 숫자 게임 ----------------------------------------------------

    def create_unique_number_round(self, votes: list[tuple[str, str]]) -> int:
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('grid', '유일 숫자', '유일 숫자', 'open', '10', ?,
                        '2026-01-01 00:00:00')
                """,
                (json.dumps({"mode": "unique_number"}),),
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

    def test_unique_number_example_rewards_nine_after_duplicate_elevens_are_removed(self) -> None:
        for user_id in ("1", "2", "3", "4", "5"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        round_id = self.create_unique_number_round(
            [("1", "4"), ("2", "6"), ("3", "9"), ("4", "11"), ("5", "11")]
        )

        result = bot.close_round_record(round_id, None)

        self.assertIsNotNone(result)
        self.assertEqual(self.score_of("1"), 0)
        self.assertEqual(self.score_of("2"), 0)
        self.assertEqual(self.score_of("3"), 2)
        self.assertEqual(self.score_of("4"), 0)
        self.assertEqual(self.score_of("5"), 0)
        rendered = json.dumps(result[1].to_dict(), ensure_ascii=False)
        self.assertIn("최고 유일 숫자 **9**", rendered)
        self.assertIn("**+2점**", rendered)
        self.assertIn("중복 제외", rendered)

    def test_unique_number_reward_tiers(self) -> None:
        expected = {
            1: 1,
            5: 1,
            6: 2,
            10: 2,
            11: 3,
            15: 3,
        }
        for number, points in expected.items():
            with self.subTest(number=number):
                self.assertEqual(bot.unique_number_reward(number), points)

    def test_unique_number_all_duplicates_score_nothing(self) -> None:
        for user_id in ("1", "2", "3", "4"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        round_id = self.create_unique_number_round(
            [("1", "15"), ("2", "15"), ("3", "4"), ("4", "4")]
        )

        result = bot.close_round_record(round_id, None)

        for user_id in ("1", "2", "3", "4"):
            self.assertEqual(self.score_of(user_id), 0)
        self.assertIn(
            "모든 선택 숫자가 중복",
            json.dumps(result[1].to_dict(), ensure_ascii=False),
        )

    def test_unique_number_view_uses_three_rows_of_five_buttons(self) -> None:
        buttons = bot.unique_number_vote_view(7).children

        self.assertEqual(len(buttons), 15)
        self.assertEqual([button.row for button in buttons], [0] * 5 + [1] * 5 + [2] * 5)
        self.assertEqual(
            {button.custom_id for button in buttons},
            {f"eventbot:vote:7:{number}" for number in range(1, 16)},
        )
        rendered = json.dumps(bot.build_unique_number_embed(7, "숫자").to_dict(), ensure_ascii=False)
        self.assertIn("중복되지 않은 숫자 중", rendered)
        self.assertIn("11~15", rendered)
        self.assertIn("+3점", rendered)

    def test_unique_number_round_uses_mode_to_distinguish_it_from_legacy_grid(self) -> None:
        round_id = self.create_unique_number_round([])
        with bot.db() as conn:
            row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()

        self.assertEqual(row["type"], "grid")
        self.assertEqual(bot.round_game_key(row), "unique_number")

    # --- 신뢰 거래 ----------------------------------------------------------

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
        # 랜덤 배정된 2번이 단독 배신자가 되어 +2점.
        self.assertEqual(self.score_of("2"), 2)

    # --- 모래성 -------------------------------------------------------------

    @staticmethod
    def sand_pair(number_a: int, number_b: int) -> dict:
        return {
            "user_a_id": "1",
            "user_b_id": "2",
            "number_a": number_a,
            "number_b": number_b,
            "phase": "active",
            "current_user_id": "1",
            "successful_removals": 0,
        }

    def test_sand_survives_difference_then_collapses(self) -> None:
        """9와 7이면 차이가 2 -> 2번까지 성공하고 3번째에 무너진다."""
        pair = self.sand_pair(9, 7)

        bot.apply_sand_action(pair, "1", "remove")
        self.assertEqual(pair["phase"], "active")
        bot.apply_sand_action(pair, "2", "remove")
        self.assertEqual(pair["phase"], "active")
        self.assertEqual(pair["successful_removals"], 2)

        bot.apply_sand_action(pair, "1", "remove")
        self.assertEqual(pair["phase"], "resolved")
        self.assertEqual(pair["outcome"], "plain_collapse")
        self.assertEqual(pair["result"]["collapsed_user"], "1")

    def test_sand_same_number_collapses_on_first_removal(self) -> None:
        pair = self.sand_pair(5, 5)

        bot.apply_sand_action(pair, "1", "remove")

        self.assertEqual(pair["phase"], "resolved")
        self.assertEqual(pair["outcome"], "plain_collapse")

    def test_sand_autocomplete_resolves_pair_without_numbers(self) -> None:
        pair = {
            "user_a_id": "1",
            "user_b_id": "2",
            "number_a": None,
            "number_b": None,
            "phase": "number_selection",
            "current_user_id": "1",
            "successful_removals": 0,
        }

        bot.autocomplete_sand_pair(pair)

        self.assertEqual(pair["phase"], "resolved")
        self.assertTrue(pair["auto_completed"])
        self.assertIn(pair["outcome"], bot.SAND_OUTCOME_LABELS)
        self.assertIsNotNone(pair["number_a"])
        self.assertIsNotNone(pair["number_b"])

    def test_sand_close_autocompletes_unfinished_pairs(self) -> None:
        for user_id in ("1", "2"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        pair = self.sand_pair(9, 7)
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('sand', '모래성', '모래성', 'open', '10', ?, '2026-01-01 00:00:00')
                """,
                (json.dumps({"sand_phase": "playing", "sand_pairs": [pair]}),),
            )
            round_id = int(cursor.lastrowid)

        result = bot.close_round_record(round_id, None)

        with bot.db() as conn:
            extra = json.loads(
                conn.execute(
                    "SELECT extra_json FROM rounds WHERE id = ?", (round_id,)
                ).fetchone()["extra_json"]
            )
        self.assertEqual(extra["sand_pairs"][0]["phase"], "resolved")
        self.assertTrue(extra["sand_pairs"][0]["auto_completed"])
        rendered = json.dumps(result[1].to_dict(), ensure_ascii=False)
        self.assertIn("자동 진행", rendered)
        self.assertNotIn("미완료", rendered)

    # --- 돌 세공 ------------------------------------------------------------

    def test_cutting_tables_match_requested_values(self) -> None:
        self.assertEqual(bot.CUTTING_SUCCESS_RATES[0], 0.9)
        self.assertEqual(bot.CUTTING_SUCCESS_RATES[1], 0.9)
        self.assertEqual(
            bot.CUTTING_REWARD_POINTS, {0: 3, 1: 3, 2: 1, 3: 1, 4: 1, 5: 2, 6: 4}
        )

    def test_cutting_embed_text_is_generated_from_constants(self) -> None:
        rendered = json.dumps(bot.build_cutting_embed(1, "세공").to_dict(), ensure_ascii=False)
        self.assertIn("0→1·1→2 90%", rendered)
        self.assertIn("0~1강화 +3점", rendered)
        self.assertIn("2~4강화 +1점", rendered)

        # 상수를 바꾸면 안내문도 따라 바뀌어야 한다 (하드코딩 재발 방지).
        with patch.object(bot, "CUTTING_REWARD_POINTS", {0: 5, 1: 5, 2: 5, 3: 5, 4: 5, 5: 5, 6: 5}):
            self.assertIn("0~6강화 +5점", bot.cutting_reward_text())

    # --- 익명 발언 ----------------------------------------------------------

    def test_anonymous_message_embed_uses_stable_number_color(self) -> None:
        first = bot.build_anonymous_message_embed(1, "첫 발언").to_dict()
        same_user_next_post = bot.build_anonymous_message_embed(1, "다음 발언").to_dict()
        second = bot.build_anonymous_message_embed(2, "다른 발언").to_dict()

        self.assertNotIn("title", first)
        self.assertNotIn("title", second)
        self.assertEqual(first["description"], "첫 발언")
        self.assertEqual(first["color"], same_user_next_post["color"])
        self.assertNotEqual(first["color"], second["color"])

    def test_anonymous_message_embed_reuses_palette_safely(self) -> None:
        palette_size = len(bot.ANONYMOUS_EMBED_COLORS)

        self.assertEqual(palette_size, 20)
        self.assertEqual(len(set(bot.ANONYMOUS_EMBED_COLORS)), palette_size)

        def rgb(color: int) -> tuple[int, int, int]:
            return ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)

        def distance(left: int, right: int) -> float:
            return sum(
                (left_channel - right_channel) ** 2
                for left_channel, right_channel in zip(rgb(left), rgb(right))
            ) ** 0.5

        adjacent_distances = [
            distance(left, right)
            for left, right in zip(
                bot.ANONYMOUS_EMBED_COLORS,
                bot.ANONYMOUS_EMBED_COLORS[1:],
            )
        ]
        self.assertGreater(min(adjacent_distances), 100)

        first = bot.build_anonymous_message_embed(1, "발언").to_dict()
        wrapped = bot.build_anonymous_message_embed(palette_size + 1, "발언").to_dict()

        self.assertEqual(first["color"], wrapped["color"])

    def test_anonymous_numbers_are_stable_per_user(self) -> None:
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, extra_json, created_at)
                VALUES ('balance', '밸런스', '선택', 'open', '{}', '2026-01-01 00:00:00')
                """
            )
            round_id = int(cursor.lastrowid)

            def post(user_id: str) -> int:
                number = bot.assign_anonymous_number(conn, round_id, user_id)
                conn.execute(
                    """
                    INSERT INTO discussion_messages
                        (round_id, user_id, anon_number, message_id, content, created_at)
                    VALUES (?, ?, ?, '1', '내용', '2026-01-01 00:00:00')
                    """,
                    (round_id, user_id, number),
                )
                return number

            # 같은 유저가 여러 번 발언해도 번호가 유지되고, 다른 유저는 다음 번호를 받는다.
            self.assertEqual(post("1"), 1)
            self.assertEqual(post("2"), 2)
            self.assertEqual(post("1"), 1)
            self.assertEqual(post("3"), 3)

            count = conn.execute(
                "SELECT COUNT(*) AS c FROM discussion_messages WHERE round_id = ? AND user_id = '1'",
                (round_id,),
            ).fetchone()["c"]
        self.assertEqual(count, 2)

    # --- 제거된 게임 --------------------------------------------------------

    def test_grid_game_is_removed(self) -> None:
        self.assertNotIn("grid", bot.MINI_GAME_TYPES)
        self.assertNotIn("grid", bot.GAME_NAMES)
        self.assertFalse(hasattr(bot, "build_grid_embed"))
        self.assertIn("unique_number", bot.MINI_GAME_TYPES)

    def test_legacy_grid_round_closes_without_scoring(self) -> None:
        """CHECK 제약에 남아 있는 옛 grid 라운드가 열려 있어도 마감이 터지지 않아야 한다."""
        self.add_user("1", "참가자", 5)
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('grid', '옛 타일', '옛 타일', 'open', '10', '{}', '2026-01-01 00:00:00')
                """
            )
            round_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                VALUES (?, '1', '5', '2026-01-01 00:00:00', '2026-01-01 00:00:00')
                """,
                (round_id,),
            )

        result = bot.close_round_record(round_id, None)

        self.assertIsNotNone(result)
        self.assertEqual(self.score_of("1"), 5)

    def test_thread_view_carries_vote_buttons_with_the_channel_custom_ids(self) -> None:
        """스레드의 시작 메시지 버튼은 눌리지 않는다. 스레드 안에도 같은 custom_id의 선택 버튼이 있어야 한다."""
        channel_ids = {
            button.custom_id for button in bot.vote_view(7, bot.BALANCE_VOTE_CHOICES).children
        }
        thread_ids = {button.custom_id for button in bot.thread_discussion_view(7, "balance").children}

        self.assertEqual(channel_ids, {"eventbot:vote:7:A", "eventbot:vote:7:B"})
        self.assertTrue(channel_ids < thread_ids)
        self.assertIn("eventbot:anonymous:7", thread_ids)

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

    def test_spending_afk_coins_without_any_row_fails(self) -> None:
        self.add_user("1", "참가자", 0)
        with bot.db() as conn:
            self.assertFalse(bot.spend_afk_coins(conn, "1", bot.SCORE_REVEAL_COST))
            self.assertEqual(bot.get_afk_coins(conn, "1"), 0)

    def test_odd_participants_leave_one_user_unmatched(self) -> None:
        pairs, unmatched_user_id = bot.build_random_trust_pairs(["1", "2", "3"], set())

        self.assertEqual(len(pairs), 1)
        self.assertIsNotNone(unmatched_user_id)
        paired = {user_id for pair in pairs for user_id in pair}
        self.assertNotIn(unmatched_user_id, paired)

    def test_emergency_join_pairs_waiting_user_and_extends_deadline(self) -> None:
        extra = {
            "sand_phase": "playing",
            "sand_pairs": [{"user_a_id": "1", "user_b_id": "2", "phase": "active"}],
            "unmatched_user_id": "3",
            "auto_close_at": 1000.0,
            "play_close_at": 1000.0,
        }

        applied = bot.apply_emergency_join(extra, "sand", "4")

        self.assertIsNotNone(applied)
        self.assertEqual(applied["user_a_id"], "3")
        self.assertEqual(applied["user_b_id"], "4")
        self.assertIsNone(extra["unmatched_user_id"])

        expected_close_at = 1000.0 + bot.EMERGENCY_EXTENSION_SECONDS
        self.assertEqual(applied["close_at"], expected_close_at)
        self.assertEqual(extra["auto_close_at"], expected_close_at)
        self.assertEqual(extra["play_close_at"], expected_close_at)

        new_pair = extra["sand_pairs"][-1]
        self.assertEqual(new_pair["phase"], "number_selection")
        self.assertEqual({new_pair["user_a_id"], new_pair["user_b_id"]}, {"3", "4"})
        self.assertIn(new_pair["current_user_id"], {"3", "4"})

    def test_emergency_join_for_trust_extends_decision_deadline_only(self) -> None:
        extra = {
            "trust_phase": "decision",
            "unmatched_user_id": "3",
            "auto_close_at": 500.0,
            "decision_close_at": 500.0,
        }

        applied = bot.apply_emergency_join(extra, "trust", "4")

        expected_close_at = 500.0 + bot.EMERGENCY_EXTENSION_SECONDS
        self.assertEqual(applied["close_at"], expected_close_at)
        self.assertEqual(extra["decision_close_at"], expected_close_at)
        # 딜레마의 섬은 짝을 trust_pairings에서만 읽는다. extra에 sand_pairs를 만들면 안 된다.
        self.assertNotIn("sand_pairs", extra)

    def test_emergency_join_is_rejected_when_seat_already_taken(self) -> None:
        """두 사람이 동시에 눌러도 한 명만 들어가야 한다. (자리를 비운 뒤에는 항상 None)"""
        extra = {"sand_phase": "playing", "unmatched_user_id": "3", "auto_close_at": 1000.0}

        self.assertIsNotNone(bot.apply_emergency_join(extra, "sand", "4"))
        self.assertIsNone(bot.apply_emergency_join(extra, "sand", "5"))
        self.assertEqual(extra["auto_close_at"], 1000.0 + bot.EMERGENCY_EXTENSION_SECONDS)

    def test_emergency_join_rejects_the_waiting_user_themselves(self) -> None:
        extra = {"sand_phase": "playing", "unmatched_user_id": "3", "auto_close_at": 1000.0}

        self.assertIsNone(bot.apply_emergency_join(extra, "sand", "3"))
        self.assertEqual(extra["unmatched_user_id"], "3")

    def test_emergency_join_name_is_shown_on_the_main_board_only(self) -> None:
        """투입자 이름은 메인 채널에만 뜬다. 각 방에는 시간 연장 사실만 알린다."""
        with bot.db() as conn:
            round_row = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('sand', '모래성', '모래성', 'open', '10', ?, '2026-01-01 00:00:00')
                RETURNING *
                """,
                (
                    json.dumps(
                        {
                            "sand_phase": "playing",
                            "emergency_user_id": "4",
                            "sand_pairs": [
                                {"user_a_id": "3", "user_b_id": "4", "phase": "number_selection"}
                            ],
                        }
                    ),
                ),
            ).fetchone()

        board = bot.build_sand_play_embed(round_row, bot.round_extra(round_row))
        self.assertIn("<@4>", json.dumps(board.to_dict(), ensure_ascii=False))

        self.assertNotIn("<@4>", bot.emergency_thread_notice(1000.0))
        self.assertNotIn("4", bot.emergency_thread_notice(1000.0).split("새 마감")[0])

    def test_sand_buttons_only_work_in_the_pair_thread(self) -> None:
        pair = {"user_a_id": "1", "user_b_id": "2", "thread_id": "777"}

        self.assertTrue(bot.sand_pair_thread_matches(pair, 777))
        self.assertFalse(bot.sand_pair_thread_matches(pair, 888))

    def test_sand_pair_without_thread_stays_playable_anywhere(self) -> None:
        """이 기능 이전에 시작된 라운드는 방이 없으므로 종전처럼 메인 채널에서 진행돼야 한다."""
        self.assertTrue(bot.sand_pair_thread_matches({"user_a_id": "1", "user_b_id": "2"}, 999))

    def test_sand_pair_embed_hides_other_pairs(self) -> None:
        extra = {
            "play_close_at": 1000.0,
            "sand_pairs": [
                {"user_a_id": "1", "user_b_id": "2", "phase": "active", "current_user_id": "1"},
                {"user_a_id": "3", "user_b_id": "4", "phase": "active", "current_user_id": "3"},
            ],
        }

        embed = bot.build_sand_pair_embed("모래성", extra["sand_pairs"][0], extra)
        rendered = json.dumps(embed.to_dict(), ensure_ascii=False)

        self.assertIn("<@1>", rendered)
        self.assertIn("<@2>", rendered)
        self.assertNotIn("<@3>", rendered)
        self.assertNotIn("<@4>", rendered)


if __name__ == "__main__":
    unittest.main()
