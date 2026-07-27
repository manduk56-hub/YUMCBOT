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


class BotRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        # db()가 호출 시점에 config.DB_PATH를 읽으므로 patch 대상도 config다.
        # bot.DB_PATH는 재노출된 사본이라 갈아끼워도 연결 경로가 바뀌지 않는다.
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

    def new_betting_game(self, conn, created_at: str | None = None) -> int:
        cursor = conn.execute(
            """
            INSERT INTO russian_roulette_games (
                guild_id, channel_id, challenger_id, target_id,
                challenger_name, target_name, status, created_at, expires_at
            )
            VALUES ('10', '20', '1', '2', '도전자', '상대', 'betting', ?, ?)
            """,
            (created_at or bot.now_text(), 9_999_999_999),
        )
        return int(cursor.lastrowid)

    def scores_of(self, conn, *user_ids: str) -> dict[str, int]:
        placeholders = ", ".join("?" for _ in user_ids)
        return {
            row["user_id"]: row["total_score"]
            for row in conn.execute(
                f"SELECT user_id, total_score FROM users WHERE user_id IN ({placeholders})",
                user_ids,
            )
        }

    def test_roulette_unifies_bets_to_the_lower_stake_and_awards_the_pot(self) -> None:
        """서로 다른 금액을 걸면 낮은 쪽으로 맞춰진다.

        금액이 다른 채로 두면 적게 건 쪽이 항상 이득이라 '무조건 최소액'이 유일한 정답이 된다.
        """
        self.add_user("1", "도전자", 10)
        self.add_user("2", "상대", 8)
        with bot.db() as conn:
            game_id = self.new_betting_game(conn)

            state, _ = bot.submit_roulette_bet(conn, game_id, "1", 3)
            self.assertEqual(state, "waiting")
            with patch.object(bot.secrets, "randbelow", side_effect=[0, 2]):
                state, game = bot.submit_roulette_bet(conn, game_id, "2", 2)

            self.assertEqual(state, "started")
            self.assertEqual(game["challenger_bet"], 2)
            self.assertEqual(game["target_bet"], 2)
            self.assertEqual(game["current_player_id"], "1")
            self.assertEqual(game["live_chamber"], 2)
            self.assertIsNotNone(game["expires_at"])
            self.assertEqual(self.scores_of(conn, "1", "2"), {"1": 8, "2": 6})

            self.assertEqual(bot.pull_roulette_trigger(conn, game_id, "1")[0], "blank")
            self.assertEqual(bot.pull_roulette_trigger(conn, game_id, "2")[0], "blank")
            result, finished = bot.pull_roulette_trigger(conn, game_id, "1")

            self.assertEqual(result, "fired")
            self.assertEqual(finished["winner_id"], "2")
            self.assertEqual(finished["finish_cause"], "fired")
            rendered = json.dumps(bot.build_roulette_embed(finished).to_dict(), ensure_ascii=False)
            self.assertIn("<@2>님이 <@1>님의 **2점**을 빼앗았습니다", rendered)

            final = self.scores_of(conn, "1", "2")
            self.assertEqual(final, {"1": 8, "2": 10})
            self.assertEqual(sum(final.values()), 18)

    def test_roulette_rejects_a_bet_over_the_current_balance(self) -> None:
        self.add_user("1", "도전자", 2)
        self.add_user("2", "상대", 3)
        with bot.db() as conn:
            game_id = self.new_betting_game(conn)
            with self.assertRaisesRegex(ValueError, "가진 점수보다 많이"):
                bot.submit_roulette_bet(conn, game_id, "1", 3)

    def test_roulette_rejects_a_bet_over_the_maximum(self) -> None:
        self.add_user("1", "도전자", 100)
        self.add_user("2", "상대", 100)
        with bot.db() as conn:
            game_id = self.new_betting_game(conn)
            with self.assertRaisesRegex(ValueError, "사이여야"):
                bot.submit_roulette_bet(conn, game_id, "1", bot.ROULETTE_MAX_BET + 1)

    def test_roulette_credit_user_bet_is_clamped_and_winning_pays_nothing(self) -> None:
        """신용 유저는 강제로 최소액을 걸고, 이겨도 예치금만 돌려받는다.

        진행판에는 최소액을 건 다른 플레이어와 똑같이 보여야 하므로 금액도 같아야 한다.
        """
        self.add_user("1", "도전자", 5)
        self.add_user("2", "빚쟁이", -1)
        with bot.db() as conn:
            game_id = self.new_betting_game(conn)

            bot.submit_roulette_bet(conn, game_id, "1", bot.ROULETTE_MAX_BET)
            # 3을 넣어도 신용 유저라 최소액으로 눌린다.
            with patch.object(bot.secrets, "randbelow", side_effect=[0, 0]):
                state, game = bot.submit_roulette_bet(conn, game_id, "2", bot.ROULETTE_MAX_BET)

            self.assertEqual(state, "started")
            self.assertEqual(game["target_bet"], bot.ROULETTE_MIN_BET)
            self.assertEqual(game["challenger_bet"], bot.ROULETTE_MIN_BET)
            self.assertEqual(game["target_negative"], 1)
            self.assertEqual(game["challenger_negative"], 0)
            self.assertEqual(self.scores_of(conn, "1", "2"), {"1": 4, "2": -2})

            result, finished = bot.pull_roulette_trigger(conn, game_id, "1")

            self.assertEqual(result, "fired")
            self.assertEqual(finished["winner_id"], "2")
            # 이긴 신용 유저는 순변동 0, 진 쪽만 1점을 잃는다.
            self.assertEqual(self.scores_of(conn, "1", "2"), {"1": 4, "2": -1})
            self.assertEqual(finished["winner_stack_after"], 1)
            self.assertEqual(bot.get_roulette_stack(conn, "2"), 1)

            # 공개 결과는 일반 유저가 이겼을 때와 글자 하나 다르지 않아야 한다.
            rendered = json.dumps(bot.build_roulette_embed(finished).to_dict(), ensure_ascii=False)
            self.assertIn("<@2>님이 <@1>님의 **1점**을 빼앗았습니다", rendered)
            self.assertNotIn("신용", rendered)
            self.assertNotIn("스택", rendered)

    def test_roulette_credit_user_stack_pays_out_on_the_second_win(self) -> None:
        self.add_user("1", "도전자", 10)
        self.add_user("2", "빚쟁이", -1)
        for expected_stack in (1, 0):
            with bot.db() as conn:
                game_id = self.new_betting_game(conn)
                bot.submit_roulette_bet(conn, game_id, "1", 1)
                with patch.object(bot.secrets, "randbelow", side_effect=[0, 0]):
                    bot.submit_roulette_bet(conn, game_id, "2", 1)
                _, finished = bot.pull_roulette_trigger(conn, game_id, "1")
                self.assertEqual(finished["winner_id"], "2")
                self.assertEqual(finished["winner_stack_after"], expected_stack)

        with bot.db() as conn:
            # 2연승 보상 +2점을 받아 신용 상태를 벗어났고, 스택은 0으로 초기화됐다.
            self.assertEqual(self.scores_of(conn, "2")["2"], 1)
            self.assertEqual(bot.get_roulette_stack(conn, "2"), 0)

    def test_roulette_stack_resets_when_leaving_credit_status(self) -> None:
        """메인게임 정산으로 신용 상태를 벗어나도 스택은 무효가 된다."""
        self.add_user("2", "빚쟁이", -3)
        with bot.db() as conn:
            bot.set_roulette_stack(conn, "2", 1, "win")
            bot.add_score(conn, "2", "빚쟁이", 5, "라운드 보상")

            self.assertEqual(bot.get_roulette_stack(conn, "2"), 0)
            reasons = [
                json.loads(row["payload_json"])["reason"]
                for row in conn.execute(
                    "SELECT payload_json FROM activity_logs "
                    "WHERE category = 'roulette' AND action = 'stack' ORDER BY id"
                )
            ]
            self.assertEqual(reasons, ["win", "escaped"])

    def _display_name(self, conn, user_id: str) -> str:
        row = conn.execute(
            "SELECT display_name FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["display_name"]

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

    def test_member_name_returns_none_on_cache_miss(self) -> None:
        """캐시 미스(멤버 없음/길드 없음)면 플레이스홀더가 아니라 None을 반환한다 (issue #19)."""
        self.assertIsNone(bot.member_name(None, "123"))
        guild = SimpleNamespace(get_member=lambda _uid: None)
        self.assertIsNone(bot.member_name(guild, "123"))
        member = SimpleNamespace(display_name="캐시된이름")
        guild_hit = SimpleNamespace(get_member=lambda _uid: member)
        self.assertEqual(bot.member_name(guild_hit, "123"), "캐시된이름")

    def test_roulette_timeout_makes_the_idle_player_lose(self) -> None:
        """자기 차례에 안 누르면 자동 패배. 무승부 환불이면 잠수가 지배 전략이 된다."""
        self.add_user("1", "도전자", 5)
        self.add_user("2", "상대", 5)
        with bot.db() as conn:
            game_id = self.new_betting_game(conn)
            bot.submit_roulette_bet(conn, game_id, "1", 2)
            with patch.object(bot.secrets, "randbelow", side_effect=[0, 5]):
                _, game = bot.submit_roulette_bet(conn, game_id, "2", 2)

            self.assertIsNotNone(game["expires_at"])
            finished = bot.timeout_roulette_turn(conn, game_id, 0)

            self.assertIsNotNone(finished)
            self.assertEqual(finished["loser_id"], "1")
            self.assertEqual(finished["winner_id"], "2")
            self.assertEqual(finished["finish_cause"], "timeout")
            self.assertEqual(finished["shots_fired"], 0)
            self.assertEqual(self.scores_of(conn, "1", "2"), {"1": 3, "2": 7})

    def test_roulette_timeout_is_ignored_when_the_turn_already_moved_on(self) -> None:
        """격발로 차례가 넘어간 뒤 도착한 낡은 타이머는 아무것도 하지 않아야 한다."""
        self.add_user("1", "도전자", 5)
        self.add_user("2", "상대", 5)
        with bot.db() as conn:
            game_id = self.new_betting_game(conn)
            bot.submit_roulette_bet(conn, game_id, "1", 1)
            with patch.object(bot.secrets, "randbelow", side_effect=[0, 5]):
                _, game = bot.submit_roulette_bet(conn, game_id, "2", 1)

            self.assertEqual(game["shots_fired"], 0)
            bot.pull_roulette_trigger(conn, game_id, "1")

            # 0번 차례를 감시하던 타이머. 이미 1번 차례로 넘어갔으므로 무시돼야 한다.
            self.assertIsNone(bot.timeout_roulette_turn(conn, game_id, 0))
            still_playing = conn.execute(
                "SELECT status FROM russian_roulette_games WHERE id = ?", (game_id,)
            ).fetchone()
            self.assertEqual(still_playing["status"], "playing")

    def test_roulette_timeout_win_does_not_advance_the_stack(self) -> None:
        """짜고 잠수해주면 확률 없이 스택을 채울 수 있으므로 시간 초과 승리는 세지 않는다."""
        self.add_user("1", "도전자", 5)
        self.add_user("2", "빚쟁이", -1)
        with bot.db() as conn:
            game_id = self.new_betting_game(conn)
            bot.submit_roulette_bet(conn, game_id, "1", 1)
            with patch.object(bot.secrets, "randbelow", side_effect=[0, 5]):
                bot.submit_roulette_bet(conn, game_id, "2", 1)

            finished = bot.timeout_roulette_turn(conn, game_id, 0)

            self.assertEqual(finished["winner_id"], "2")
            self.assertIsNone(finished["winner_stack_after"])
            self.assertEqual(bot.get_roulette_stack(conn, "2"), 0)

    def test_roulette_daily_count_charges_only_the_player_who_skipped_betting(self) -> None:
        """베팅을 넣은 쪽은 상대가 방치해도 횟수를 잃지 않는다."""
        self.add_user("1", "도전자", 5)
        self.add_user("2", "상대", 5)
        today = bot.roulette_day_key()
        with bot.db() as conn:
            conn.execute(
                """
                INSERT INTO russian_roulette_games (
                    guild_id, channel_id, challenger_id, target_id,
                    challenger_name, target_name, status, created_at,
                    accepted_at, challenger_bet, target_bet
                )
                VALUES ('10', '20', '1', '2', '도전자', '상대', 'expired', ?, ?, NULL, 2)
                """,
                (f"{today} 12:00:00", f"{today} 12:01:00"),
            )

            self.assertEqual(bot.count_roulette_games_today(conn, "1"), 1)
            self.assertEqual(bot.count_roulette_games_today(conn, "2"), 0)
            # 성립하지 않은 판이라 '같은 상대 하루 1회'는 태우지 않는다.
            self.assertEqual(
                bot.count_roulette_games_with_opponent_today(conn, "1", "2"), 0
            )

    def test_roulette_daily_count_ignores_declines_and_pending_expiry(self) -> None:
        self.add_user("1", "도전자", 5)
        self.add_user("2", "상대", 5)
        today = bot.roulette_day_key()
        with bot.db() as conn:
            for status, accepted in (("declined", None), ("expired", None)):
                conn.execute(
                    """
                    INSERT INTO russian_roulette_games (
                        guild_id, channel_id, challenger_id, target_id,
                        challenger_name, target_name, status, created_at, accepted_at
                    )
                    VALUES ('10', '20', '1', '2', '도전자', '상대', ?, ?, ?)
                    """,
                    (status, f"{today} 12:00:00", accepted),
                )

            self.assertEqual(bot.count_roulette_games_today(conn, "1"), 0)
            self.assertEqual(bot.count_roulette_games_today(conn, "2"), 0)

    def test_roulette_daily_count_is_scoped_to_the_day(self) -> None:
        self.add_user("1", "도전자", 5)
        self.add_user("2", "상대", 5)
        with bot.db() as conn:
            for day in ("2026-07-21", "2026-07-22"):
                conn.execute(
                    """
                    INSERT INTO russian_roulette_games (
                        guild_id, channel_id, challenger_id, target_id,
                        challenger_name, target_name, status, created_at
                    )
                    VALUES ('10', '20', '1', '2', '도전자', '상대', 'finished', ?)
                    """,
                    (f"{day} 12:00:00",),
                )

            self.assertEqual(
                bot.count_roulette_games_today(conn, "1", day="2026-07-22"), 1
            )
            self.assertEqual(
                bot.count_roulette_games_with_opponent_today(
                    conn, "1", "2", day="2026-07-22"
                ),
                1,
            )

    def test_roulette_pending_locks_only_the_challenger(self) -> None:
        """받는 쪽까지 묶으면 신청만 걸어두는 것으로 상대를 공짜로 배제할 수 있다."""
        self.add_user("1", "도전자", 5)
        self.add_user("2", "상대", 5)
        with bot.db() as conn:
            conn.execute(
                """
                INSERT INTO russian_roulette_games (
                    guild_id, channel_id, challenger_id, target_id,
                    challenger_name, target_name, status, created_at, expires_at
                )
                VALUES ('10', '20', '1', '2', '도전자', '상대', 'pending', ?, ?)
                """,
                (bot.now_text(), 9_999_999_999),
            )

            self.assertIsNotNone(bot.active_roulette_game_for_user(conn, "1"))
            self.assertIsNone(bot.active_roulette_game_for_user(conn, "2"))

    def test_roulette_betting_embed_hides_amounts_until_both_are_in(self) -> None:
        """먼저 넣은 금액이 보이면 나중에 넣는 쪽이 그걸 보고 정할 수 있다."""
        game = {
            "id": 7,
            "challenger_id": "1",
            "target_id": "2",
            "status": "betting",
            "challenger_bet": 3,
            "target_bet": None,
            "expires_at": 9_999_999_999,
            "finish_cause": None,
        }

        rendered = json.dumps(bot.build_roulette_embed(game).to_dict(), ensure_ascii=False)

        self.assertIn("입력 완료", rendered)
        self.assertIn("입력 대기 중", rendered)
        self.assertNotIn("**3점**", rendered)

    def test_roulette_finished_view_keeps_a_personal_settlement_button(self) -> None:
        view = bot.roulette_view({"id": 7, "status": "finished"})

        self.assertEqual(
            [item.custom_id for item in view.children], ["eventbot:roulettesettle:7"]
        )

    def test_roulette_settlement_message_is_private_about_credit_results(self) -> None:
        self.add_user("2", "빚쟁이", -1)
        game = {
            "id": 7,
            "challenger_id": "1",
            "target_id": "2",
            "challenger_bet": 1,
            "target_bet": 1,
            "challenger_negative": 0,
            "target_negative": 1,
            "winner_id": "2",
            "loser_id": "1",
            "finish_cause": "fired",
            "winner_stack_after": 1,
        }

        with bot.db() as conn:
            message = bot.roulette_settlement_message(conn, game, "2")

        self.assertIn("점수 변동 **없음**", message)
        self.assertIn(f"연승 스택 **1/{bot.ROULETTE_STACK_GOAL}**", message)

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

    def test_sand_rules_match_requested_settlements(self) -> None:
        embed = bot.build_sand_registration_embed(1, "모래성", 2)
        rendered = json.dumps(embed.to_dict(), ensure_ascii=False)
        for expected in (
            "둘 다 포기 선택 시: **서로 -3점**",
            "덜기 성공: A는 **-3점**, B는 **+5점**",
            "무너짐: B는 **-3점**, A는 **+2점**",
            "서로 덜기를 하다 무너진 경우: 그 턴의 사람이 **-3점**",
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

    @staticmethod
    def insert_bail_round(extra: dict) -> int:
        """가라앉는 배 라운드를 넣는다. CHECK 제약 때문에 type은 sand고 mode로 갈린다."""
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('sand', '가라앉는 배', '가라앉는 배', 'open', '10', ?, '2026-01-01 00:00:00')
                """,
                (json.dumps({"mode": "bail", **extra}),),
            )
            return int(cursor.lastrowid)

    def test_balance_one_sided_vote_penalizes_every_voter(self) -> None:
        for user_id in ("1", "2", "3"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        round_id = self.create_balance_round([("1", "A"), ("2", "A"), ("3", "A")])

        result = bot.close_round_record(round_id, None)

        self.assertIsNotNone(result)
        for user_id in ("1", "2", "3"):
            self.assertEqual(self.score_of(user_id), -2)
        self.assertIn("-2점", json.dumps(result[1].to_dict(), ensure_ascii=False))

    def test_balance_only_lone_minority_gets_two_points(self) -> None:
        for user_id in ("1", "2", "3"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        round_id = self.create_balance_round([("1", "A"), ("2", "A"), ("3", "B")])

        result = bot.close_round_record(round_id, None)

        self.assertEqual(self.score_of("1"), 0)
        self.assertEqual(self.score_of("2"), 0)
        self.assertEqual(self.score_of("3"), 2)
        self.assertIn(
            "소수파 `B` 단독 선택자만 +2점",
            json.dumps(result[1].to_dict(), ensure_ascii=False),
        )
        self.assertIn(
            "다수파 `A` 선택자는 점수 없음",
            json.dumps(result[1].to_dict(), ensure_ascii=False),
        )

    def test_balance_minority_with_multiple_voters_gets_no_bonus(self) -> None:
        for user_id in ("1", "2", "3", "4", "5"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        round_id = self.create_balance_round(
            [("1", "A"), ("2", "A"), ("3", "A"), ("4", "B"), ("5", "B")]
        )

        bot.close_round_record(round_id, None)

        self.assertEqual(self.score_of("1"), 1)
        self.assertEqual(self.score_of("2"), 1)
        self.assertEqual(self.score_of("3"), 1)
        self.assertEqual(self.score_of("4"), 0)
        self.assertEqual(self.score_of("5"), 0)

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
        self.assertIn("중복 숫자 선택", rendered)

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
        # 랜덤 배정된 2번이 단독 배신자가 되어 +5점.
        self.assertEqual(self.score_of("2"), 5)

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

    def test_sand_scoring_matches_trust_game_score_range(self) -> None:
        for user_id in ("1", "2", "3", "4", "5", "6", "7", "8"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        pairs = [
            {
                **self.sand_pair(9, 7),
                "user_a_id": "1",
                "user_b_id": "2",
                "phase": "resolved",
                "outcome": "mutual_quit",
                "result": {"losers": ["1", "2"]},
            },
            {
                **self.sand_pair(9, 7),
                "user_a_id": "3",
                "user_b_id": "4",
                "phase": "resolved",
                "outcome": "quit_success",
                "result": {"winner": "3", "quitter": "4"},
            },
            {
                **self.sand_pair(9, 7),
                "user_a_id": "5",
                "user_b_id": "6",
                "phase": "resolved",
                "outcome": "quit_collapse",
                "result": {"winner": "5", "collapsed_user": "6"},
            },
            {
                **self.sand_pair(9, 7),
                "user_a_id": "7",
                "user_b_id": "8",
                "phase": "resolved",
                "outcome": "plain_collapse",
                "result": {"collapsed_user": "7"},
            },
        ]
        with bot.db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('sand', '점수표 테스트', '점수표 테스트', 'open', '10', ?, '2026-01-01 00:00:00')
                """,
                (json.dumps({"sand_phase": "playing", "sand_pairs": pairs}),),
            )
            round_id = int(cursor.lastrowid)

        bot.close_round_record(round_id, None)

        self.assertEqual(
            {user_id: self.score_of(user_id) for user_id in ("1", "2", "3", "4", "5", "6", "7", "8")},
            {"1": -3, "2": -3, "3": 5, "4": -3, "5": 2, "6": -3, "7": -3, "8": 0},
        )

    # --- 가라앉는 배 --------------------------------------------------------

    @staticmethod
    def bail_pair(**overrides) -> dict:
        pair = bot.bail_new_pair("1", "2")
        pair["proposer_id"] = "1"  # 랜덤이라 테스트에서는 고정한다
        pair.update(overrides)
        return pair

    @staticmethod
    def bail_submitted_pair(
        scoop_a: int,
        scoop_b: int,
        reward_split: dict | None = None,
    ) -> dict:
        pair = bot.bail_new_pair("1", "2")
        pair.update(
            {
                "phase": "submitting",
                "reward_split": reward_split,
                "scoop_a": scoop_a,
                "scoop_b": scoop_b,
            }
        )
        bot.resolve_bail_pair(pair)
        return pair

    @staticmethod
    def bail_even_split() -> dict:
        half = bot.BAIL_REWARD_POOL // 2
        return {"user_a": half, "user_b": bot.BAIL_REWARD_POOL - half}

    def test_bail_accepted_offer_confirms_reward_split(self) -> None:
        pair = self.bail_pair()

        bot.apply_bail_offer(pair, "1", 6)
        self.assertEqual(pair["offer"], {"proposer_id": "1", "reward": 6})
        self.assertEqual(pair["offer_count"], 1)

        bot.apply_bail_response(pair, "2", True)

        # 제안자가 6점이면 상대는 나머지를 받는다.
        self.assertEqual(pair["reward_split"], {"user_a": 6, "user_b": bot.BAIL_REWARD_POOL - 6})
        self.assertEqual(pair["phase"], "submitting")
        self.assertIsNone(pair["offer"])

    def test_bail_rejection_hands_proposal_turn_to_responder(self) -> None:
        pair = self.bail_pair()

        bot.apply_bail_offer(pair, "1", 7)
        bot.apply_bail_response(pair, "2", False)

        self.assertEqual(pair["phase"], "negotiating")
        self.assertEqual(pair["proposer_id"], "2")
        self.assertIsNone(pair["offer"])
        self.assertIsNone(pair["reward_split"])

    def test_bail_exhausted_offers_end_negotiation_in_failure(self) -> None:
        """제안 횟수를 모두 소진하면 결렬 — 감액된 보상금을 균등 분배한다."""
        pair = self.bail_pair()

        proposer, responder = "1", "2"
        for _ in range(bot.BAIL_NEGOTIATION_MAX_OFFERS):
            bot.apply_bail_offer(pair, proposer, bot.BAIL_REWARD_POOL)
            bot.apply_bail_response(pair, responder, False)
            proposer, responder = responder, proposer

        self.assertEqual(pair["offer_count"], bot.BAIL_NEGOTIATION_MAX_OFFERS)
        self.assertEqual(pair["phase"], "submitting")
        self.assertIsNone(pair["reward_split"])
        self.assertEqual(
            bot.bail_reward_share(pair, "user_a"), bot.BAIL_FAILED_REWARD_POOL // 2
        )

    def test_bail_survives_when_submissions_reach_requirement(self) -> None:
        pair = self.bail_submitted_pair(3, 3, reward_split=self.bail_even_split())

        self.assertEqual(pair["outcome"], "survived")
        self.assertEqual(pair["result"]["total_scoops"], bot.BAIL_REQUIRED_SCOOPS)
        # 공평하게 나누고 공평하게 일하면 낮은 이득: 보상 몫에서 퍼낸 만큼을 뺀다.
        half = bot.BAIL_REWARD_POOL // 2
        self.assertEqual(pair["result"]["score_a"], half - 3)
        self.assertEqual(pair["result"]["score_b"], half - 3)

    def test_bail_sink_penalty_scales_with_reward_share(self) -> None:
        """많이 받기로 한 쪽이 침몰에서 더 크게 잃는다 — 이게 배신 스위치다."""
        greedy = self.bail_submitted_pair(3, 2, reward_split={"user_a": 7, "user_b": 3})

        self.assertEqual(greedy["outcome"], "sunk")
        self.assertEqual(greedy["result"]["score_a"], -bot.BAIL_SINK_PENALTY_HIGH)
        self.assertEqual(greedy["result"]["score_b"], -bot.BAIL_SINK_PENALTY_LOW)

        even = self.bail_submitted_pair(3, 2, reward_split=self.bail_even_split())
        self.assertEqual(even["result"]["score_a"], -bot.BAIL_SINK_PENALTY_EVEN)
        self.assertEqual(even["result"]["score_b"], -bot.BAIL_SINK_PENALTY_EVEN)

    def test_bail_underpaid_side_gains_by_shirking(self) -> None:
        """보상을 적게 받은 쪽은 노동을 깎는 편이 낫다. 상대는 침몰이 더 아파서 떠안게 된다."""
        split = {"user_a": 7, "user_b": 3}
        # B가 합의대로 절반씩 일하면
        fair = self.bail_submitted_pair(3, 3, reward_split=split)
        # B가 깎고 A가 그만큼 더 떠안으면
        shirked = self.bail_submitted_pair(4, 2, reward_split=split)

        self.assertGreater(shirked["result"]["score_b"], fair["result"]["score_b"])
        # A는 침몰보다 4번 떠안는 쪽이 낫다.
        self.assertGreater(shirked["result"]["score_a"], -bot.BAIL_SINK_PENALTY_HIGH)

    def test_bail_free_rider_takes_full_share(self) -> None:
        """한 명이 전부 떠안으면 무임승차한 쪽이 몫을 그대로 챙긴다."""
        split = self.bail_even_split()
        pair = self.bail_submitted_pair(0, bot.BAIL_REQUIRED_SCOOPS, reward_split=split)

        self.assertEqual(pair["outcome"], "survived")
        self.assertEqual(pair["result"]["score_a"], split["user_a"])
        self.assertEqual(
            pair["result"]["score_b"],
            split["user_b"] - bot.BAIL_REQUIRED_SCOOPS,
        )
        # 떠안는 선택이 침몰보다 나아야 성립한다.
        self.assertGreater(pair["result"]["score_b"], -bot.BAIL_SINK_PENALTY_EVEN)

    def test_bail_failed_negotiation_is_worse_than_agreeing(self) -> None:
        """결렬은 확실히 손해다. 그래서 '합의 안 해준다'가 자기 살을 깎는 위협이 된다."""
        agreed = self.bail_submitted_pair(3, 3, reward_split=self.bail_even_split())
        failed = self.bail_submitted_pair(3, 3)

        self.assertLess(failed["result"]["score_a"], agreed["result"]["score_a"])
        self.assertEqual(
            failed["result"]["score_a"], bot.BAIL_FAILED_REWARD_POOL // 2 - 3
        )

    def test_bail_labor_is_never_bound_by_negotiation(self) -> None:
        """노동은 협상 대상이 아니다. 같은 배분이라도 제출값에 따라 점수가 달라진다."""
        split = self.bail_even_split()
        worked = self.bail_submitted_pair(4, 2, reward_split=split)
        shirked = self.bail_submitted_pair(2, 4, reward_split=split)

        self.assertNotEqual(worked["result"]["score_a"], shirked["result"]["score_a"])
        # 두 경우 모두 배는 뜬다 — 합계가 같기 때문이다.
        self.assertEqual(worked["outcome"], shirked["outcome"])

    def test_bail_autocomplete_fills_missing_submissions(self) -> None:
        pair = self.bail_pair(
            phase="submitting",
            reward_split=self.bail_even_split(),
            scoop_a=None,
            scoop_b=1,
        )

        bot.autocomplete_bail_pair(pair)

        # 미제출자만 절반으로 채우고, 이미 낸 사람은 낸 값 그대로.
        self.assertEqual(pair["scoop_a"], bot.BAIL_FALLBACK_SCOOPS)
        self.assertEqual(pair["scoop_b"], 1)
        self.assertEqual(pair["phase"], "resolved")
        self.assertTrue(pair["auto_completed"])

    def test_bail_autocomplete_during_negotiation_counts_as_failure(self) -> None:
        pair = self.bail_pair(phase="negotiating", offer={"proposer_id": "1", "reward": 8})

        bot.autocomplete_bail_pair(pair)

        self.assertEqual(pair["scoop_a"], bot.BAIL_FALLBACK_SCOOPS)
        self.assertEqual(pair["scoop_b"], bot.BAIL_FALLBACK_SCOOPS)
        self.assertIsNone(pair["reward_split"])
        self.assertEqual(pair["result"]["reward_a"], bot.BAIL_FAILED_REWARD_POOL // 2)
        self.assertIsNone(pair["offer"])
        # 절반씩이면 배가 뜬다 — 잠수 한 명이 상대까지 침몰시키지 않는다.
        self.assertEqual(pair["outcome"], "survived")

    def test_bail_negotiations_complete_flag(self) -> None:
        negotiating = {"bail_pairs": [self.bail_pair()]}
        submitting = {"bail_pairs": [self.bail_pair(phase="submitting")]}

        self.assertFalse(bot.bail_negotiations_complete(negotiating))
        self.assertTrue(bot.bail_negotiations_complete(submitting))
        # 짝이 없으면 완료가 아니다 (라운드가 조기 전이되지 않게).
        self.assertFalse(bot.bail_negotiations_complete({"bail_pairs": []}))

    def test_bail_scoop_can_be_resubmitted_until_close(self) -> None:
        """마감까지 정산이 없으므로 제출은 몇 번이든 덮어쓴다. 직전 값은 로그용으로 돌려준다."""
        pair = self.bail_pair(phase="submitting")

        self.assertIsNone(bot.apply_bail_scoop(pair, "1", 2))
        self.assertEqual(pair["scoop_a"], 2)

        self.assertEqual(bot.apply_bail_scoop(pair, "1", 5), 2)
        self.assertEqual(pair["scoop_a"], 5)
        # 번복은 본인 쪽만 건드린다.
        self.assertIsNone(pair["scoop_b"])

        self.assertIsNone(bot.apply_bail_scoop(pair, "2", 1))
        self.assertEqual(pair["scoop_b"], 1)

    def test_bail_execution_status_hides_submission_progress(self) -> None:
        """제출 인원수가 보이면 상대의 제출 여부가 새고, 번복 불가인 비공개 제출이 무너진다."""
        extra = {"bail_phase": "execution"}
        none_submitted = self.bail_pair(phase="submitting")
        one_submitted = self.bail_pair(phase="submitting", scoop_a=2)
        both_submitted = self.bail_pair(phase="submitting", scoop_a=2, scoop_b=4)

        texts = {
            bot.bail_pair_status_text(pair, extra)
            for pair in (none_submitted, one_submitted, both_submitted)
        }

        self.assertEqual(len(texts), 1)
        self.assertNotIn("/2", texts.pop())

    def test_bail_autocomplete_does_not_flag_fully_submitted_pairs(self) -> None:
        """실행 단계는 조기 정산이 없어 정상 제출한 짝도 마감 때 이 함수를 거친다."""
        pair = self.bail_pair(
            phase="submitting",
            reward_split=self.bail_even_split(),
            scoop_a=3,
            scoop_b=3,
        )

        self.assertFalse(bot.autocomplete_bail_pair(pair))

        self.assertEqual(pair["phase"], "resolved")
        self.assertNotIn("auto_completed", pair)

    def test_bail_registration_transitions_to_negotiation(self) -> None:
        self.add_user("1", "참가자1", 0)
        self.add_user("2", "참가자2", 0)
        round_id = self.insert_bail_round({"bail_phase": "registration"})
        with bot.db() as conn:
            conn.executemany(
                "INSERT INTO trust_registrations (round_id, user_id, registered_at) VALUES (?, ?, ?)",
                [(round_id, "1", "2026-01-01 00:00:00"), (round_id, "2", "2026-01-01 00:00:01")],
            )

        transition = bot.start_bail_negotiation(round_id)

        self.assertIsNotNone(transition)
        self.assertEqual(transition["state"], "negotiation")
        self.assertEqual({tuple(sorted(pair)) for pair in transition["pairs"]}, {("1", "2")})
        with bot.db() as conn:
            extra = json.loads(
                conn.execute(
                    "SELECT extra_json FROM rounds WHERE id = ?", (round_id,)
                ).fetchone()["extra_json"]
            )
        self.assertEqual(extra["bail_phase"], "negotiation")
        self.assertEqual(extra["bail_pairs"][0]["phase"], "negotiating")

    def test_bail_execution_transition_drops_pending_offers(self) -> None:
        """협상 시간이 끝나면 대기 중인 제안은 결렬로 사라진다."""
        pending = self.bail_pair(offer={"proposer_id": "1", "reward": 8}, offer_count=1)
        settled = self.bail_pair(phase="submitting", reward_split={"user_a": 7, "user_b": 3})
        round_id = self.insert_bail_round(
            {"bail_phase": "negotiation", "bail_pairs": [pending, settled]}
        )

        transition = bot.start_bail_execution(round_id)

        self.assertEqual(transition["state"], "execution")
        with bot.db() as conn:
            extra = json.loads(
                conn.execute(
                    "SELECT extra_json FROM rounds WHERE id = ?", (round_id,)
                ).fetchone()["extra_json"]
            )
        self.assertEqual(extra["bail_phase"], "execution")
        self.assertEqual(extra["bail_pairs"][0]["phase"], "submitting")
        self.assertIsNone(extra["bail_pairs"][0]["offer"])
        self.assertIsNone(extra["bail_pairs"][0]["reward_split"])
        # 이미 배분을 확정한 짝은 그대로 유지된다.
        self.assertEqual(extra["bail_pairs"][1]["reward_split"], {"user_a": 7, "user_b": 3})

    def test_bail_execution_transition_requires_negotiation_phase(self) -> None:
        round_id = self.insert_bail_round({"bail_phase": "execution", "bail_pairs": []})

        self.assertIsNone(bot.start_bail_execution(round_id))

    def test_bail_close_autocompletes_and_scores_pairs(self) -> None:
        for user_id in ("1", "2", "3", "4", "5", "6"):
            self.add_user(user_id, f"참가자{user_id}", 0)
        split = {"user_a": 7, "user_b": 3}
        survived = self.bail_submitted_pair(2, 4, reward_split=split)
        sunk = {
            **self.bail_submitted_pair(3, 2, reward_split=split),
            "user_a_id": "3",
            "user_b_id": "4",
        }
        unfinished = self.bail_pair(
            user_a_id="5",
            user_b_id="6",
            phase="submitting",
            reward_split=self.bail_even_split(),
            scoop_a=3,
        )
        round_id = self.insert_bail_round(
            {"bail_phase": "execution", "bail_pairs": [survived, sunk, unfinished]}
        )

        result = bot.close_round_record(round_id, None)

        half = bot.BAIL_REWARD_POOL // 2
        self.assertEqual(
            {user_id: self.score_of(user_id) for user_id in ("1", "2", "3", "4", "5", "6")},
            {
                # 7점 받고 2번, 3점 받고 4번 → 배는 떴다.
                "1": 7 - 2,
                "2": 3 - 4,
                # 침몰: 많이 받기로 한 3번이 더 크게 잃는다.
                "3": -bot.BAIL_SINK_PENALTY_HIGH,
                "4": -bot.BAIL_SINK_PENALTY_LOW,
                # 미제출자는 절반(3번)으로 자동 제출되어 배가 뜬다.
                "5": half - 3,
                "6": half - 3,
            },
        )
        with bot.db() as conn:
            extra = json.loads(
                conn.execute(
                    "SELECT extra_json FROM rounds WHERE id = ?", (round_id,)
                ).fetchone()["extra_json"]
            )
        self.assertEqual(extra["bail_phase"], "scored")
        self.assertTrue(extra["bail_pairs"][2]["auto_completed"])
        rendered = json.dumps(result[1].to_dict(), ensure_ascii=False)
        self.assertIn("자동 제출", rendered)
        self.assertIn("침몰", rendered)

    def test_bail_result_flags_the_free_rider(self) -> None:
        """돈은 더 받고 일은 덜 한 사람이 결과에 드러나야 한다 — 이게 평판 장치다."""
        pair = self.bail_submitted_pair(2, 4, reward_split={"user_a": 7, "user_b": 3})

        rendered = bot.bail_split_text(pair)

        self.assertIn("2번 ⚠️", rendered)  # 7점 받고 2번만 퍼냈다
        self.assertIn("4번", rendered)
        self.assertNotIn("4번 ⚠️", rendered)

    def test_bail_split_text_marks_failed_negotiation(self) -> None:
        pair = self.bail_submitted_pair(3, 3)

        rendered = bot.bail_split_text(pair)

        self.assertIn("결렬", rendered)
        self.assertNotIn("⚠️", rendered)

    def test_bail_emergency_join_starts_a_new_negotiation(self) -> None:
        extra = {"bail_phase": "negotiation", "unmatched_user_id": "3", "auto_close_at": 100.0}

        applied = bot.apply_emergency_join(extra, "bail", "4")

        self.assertIsNotNone(applied)
        self.assertEqual(extra["bail_pairs"][0]["phase"], "negotiating")
        self.assertEqual(
            {extra["bail_pairs"][0]["user_a_id"], extra["bail_pairs"][0]["user_b_id"]},
            {"3", "4"},
        )
        self.assertIsNone(extra["unmatched_user_id"])
        # 협상 단계에서는 협상 마감이 연장된다.
        self.assertEqual(extra["negotiation_close_at"], extra["auto_close_at"])
        self.assertNotIn("play_close_at", extra)

    def test_bail_emergency_join_during_execution_skips_negotiation(self) -> None:
        """실행 단계에 투입되면 협상 기회가 없으므로 제출 단계에서 시작한다."""
        extra = {"bail_phase": "execution", "unmatched_user_id": "3", "auto_close_at": 100.0}

        bot.apply_emergency_join(extra, "bail", "4")

        self.assertEqual(extra["bail_pairs"][0]["phase"], "submitting")
        self.assertIsNone(extra["bail_pairs"][0]["reward_split"])
        self.assertEqual(extra["execution_close_at"], extra["auto_close_at"])

    def test_bail_rules_text_is_generated_from_constants(self) -> None:
        with patch.object(bot, "BAIL_SINK_PENALTY_HIGH", 9):
            self.assertIn("-9점", bot.build_bail_registration_embed(1, "배", 2).fields[1].value)

    def test_bail_pair_view_fits_discord_button_limits(self) -> None:
        """버튼은 한 줄에 5개가 상한이다. 넘기면 View 생성 자체가 터진다."""
        cases = [
            ({"bail_phase": "negotiation"}, self.bail_pair()),
            (
                {"bail_phase": "negotiation"},
                self.bail_pair(offer={"proposer_id": "1", "reward": 5}, offer_count=1),
            ),
            ({"bail_phase": "execution"}, self.bail_pair(phase="submitting")),
        ]
        for extra, pair in cases:
            view = bot.bail_pair_view(1, pair, extra)
            per_row: dict[int, int] = {}
            for item in view.children:
                per_row[item.row] = per_row.get(item.row, 0) + 1
            self.assertLessEqual(max(per_row.values()), 5, extra)
            self.assertLessEqual(len(view.children), 25, extra)
            self.assertTrue(
                all(item.custom_id.startswith("eventbot:bail") for item in view.children),
                extra,
            )

    def test_bail_pair_view_locks_proposals_while_offer_pending(self) -> None:
        extra = {"bail_phase": "negotiation"}
        pending = bot.bail_pair_view(
            1, self.bail_pair(offer={"proposer_id": "1", "reward": 5}), extra
        )

        disabled = {
            item.custom_id.rsplit(":", 1)[0]: item.disabled for item in pending.children
        }
        self.assertTrue(disabled["eventbot:bailoffer:1"])
        self.assertFalse(disabled["eventbot:bailrespond:1"])

    def test_bail_resolved_pair_has_no_buttons(self) -> None:
        view = bot.bail_pair_view(1, self.bail_submitted_pair(3, 3), {"bail_phase": "execution"})

        self.assertEqual(view.children, [])

    def test_bail_embeds_render_in_every_phase(self) -> None:
        """embed 필드는 1024자 제한이 있어 단계별로 한 번씩 만들어봐야 한다."""
        for phase, pair in (
            ("negotiation", self.bail_pair()),
            ("execution", self.bail_pair(phase="submitting", scoop_a=2)),
            ("scored", self.bail_submitted_pair(2, 4, reward_split={"user_a": 6, "user_b": 2})),
        ):
            extra = {
                "bail_phase": phase,
                "bail_pairs": [pair],
                "unmatched_user_id": "9",
                "negotiation_close_at": 100.0,
                "execution_close_at": 200.0,
            }
            round_row = {"title": "가라앉는 배", "type": "sand"}
            for embed in (
                bot.build_bail_pair_embed("가라앉는 배", pair, extra),
                bot.build_bail_play_embed(round_row, extra),
                bot.build_bail_pair_result_embed("가라앉는 배", pair),
            ):
                for field in embed.fields:
                    self.assertLessEqual(len(field.value), 1024, (phase, field.name))

    def test_bail_choices_cover_full_range(self) -> None:
        # 협상은 보상금 전부를, 실행은 노동 전부를 요구할 수 있어야 폭이 나온다.
        self.assertEqual(
            bot.BAIL_REWARD_CHOICES,
            [str(index) for index in range(bot.BAIL_REWARD_POOL + 1)],
        )
        self.assertEqual(
            bot.BAIL_SCOOP_CHOICES,
            [str(index) for index in range(bot.BAIL_REQUIRED_SCOOPS + 1)],
        )

    def test_bail_greedy_split_cannot_beat_a_moderate_one(self) -> None:
        """8:0으로 이겨도 6:2보다 나을 게 없다 — 상대가 노동할 이유를 없애면 자기도 못 챈다."""
        # 상대(B)는 몫이 0이면 퍼낼수록 손해라 0번을 낸다. A가 전부 떠안아야 배가 뜬다.
        greedy = self.bail_submitted_pair(
            bot.BAIL_REQUIRED_SCOOPS, 0, reward_split={"user_a": 8, "user_b": 0}
        )
        moderate = self.bail_submitted_pair(4, 2, reward_split={"user_a": 6, "user_b": 2})

        self.assertEqual(greedy["outcome"], "survived")
        self.assertLessEqual(greedy["result"]["score_a"], moderate["result"]["score_a"])

    # --- 돌 세공 ------------------------------------------------------------

    def test_cutting_tables_match_requested_values(self) -> None:
        self.assertEqual(bot.CUTTING_SUCCESS_RATES[0], 0.9)
        self.assertEqual(bot.CUTTING_SUCCESS_RATES[1], 0.9)
        self.assertEqual(bot.CUTTING_SUCCESS_RATES[5], 0.15)
        self.assertEqual(
            bot.CUTTING_REWARD_POINTS, {0: 3, 1: 3, 2: 1, 3: 1, 4: 1, 5: 2, 6: 6}
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

    def create_baseball_round(self, participant_count: int = 5) -> int:
        with bot.db() as conn:
            event_id = int(
                conn.execute(
                    """
                    INSERT INTO event_sessions (status, channel_id, created_at)
                    VALUES ('active', '10', '2026-01-01 00:00:00')
                    """
                ).lastrowid
            )
            for index in range(1, participant_count + 1):
                user_id = str(index)
                conn.execute(
                    """
                    INSERT INTO users
                        (user_id, display_name, total_score, created_at, updated_at)
                    VALUES (?, ?, 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00')
                    """,
                    (user_id, f"타자{index}"),
                )
                conn.execute(
                    """
                    INSERT INTO event_participants (event_session_id, user_id, joined_at)
                    VALUES (?, ?, '2026-01-01 00:00:00')
                    """,
                    (event_id, user_id),
                )
            return int(
                conn.execute(
                    """
                    INSERT INTO rounds
                        (type, title, question, status, channel_id, message_id,
                         extra_json, created_at, event_session_id)
                    VALUES ('grid', '야구', '야구', 'open', '10', '20', ?,
                            '2026-01-01 00:00:00', ?)
                    """,
                    (
                        json.dumps(
                            {
                                "mode": "baseball",
                                "baseball_pitch": 1,
                                "baseball_phase": "waiting_prepare",
                                "baseball_control_message_id": "99",
                                "baseball_result_message_ids": {},
                            },
                            ensure_ascii=False,
                        ),
                        event_id,
                    ),
                ).lastrowid
            )

    @staticmethod
    def baseball_user(user_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            id=user_id,
            name=f"타자{user_id}",
            display_name=f"타자{user_id}",
            bot=False,
        )

    def test_baseball_embed_and_commands_match_three_pitch_flow(self) -> None:
        embed = bot.build_baseball_embed(7, "야구", 1)
        rendered = embed.to_dict()
        self.assertIn("<1구/3구>", rendered["description"])
        self.assertEqual(
            rendered["image"]["url"],
            f"attachment://{bot.BASEBALL_IMAGE_FILENAME}",
        )
        self.assertIn("3구 평균", json.dumps(rendered, ensure_ascii=False))
        self.assertIn("7.00초", json.dumps(rendered, ensure_ascii=False))
        score_fields = rendered["fields"][2:5]
        self.assertEqual(
            [(field["name"], field["value"], field["inline"]) for field in score_fields],
            [
                ("🥇 1위", "**+3점**", True),
                ("🥈 2위", "**+2점**", True),
                ("🥉 3위", "**+1점**", True),
            ],
        )
        self.assertIn("투수가 준비중입니다.", json.dumps(rendered, ensure_ascii=False))
        self.assertNotIn("`/준비`", json.dumps(rendered, ensure_ascii=False))

        prepare_command = bot.bot.tree.get_command("준비")
        self.assertIsNotNone(prepare_command)
        self.assertEqual(prepare_command.default_permissions.value, 0)
        self.assertEqual(len(prepare_command.checks), 1)
        self.assertIsNone(bot.bot.tree.get_command("투구"))
        waiting_control = bot.baseball_control_content(1, "waiting_prepare")
        self.assertEqual(waiting_control, "⚾ **야구 조작판 · <1구/3구>**")
        self.assertNotIn("관리자", waiting_control)
        self.assertEqual(
            bot.baseball_control_content(1, "winding_up"),
            "⚾ **투수 와인드업 · <1구/3구>**",
        )

        windup = bot.baseball_swing_view(7, 2, disabled=True, button_position=None)
        self.assertEqual(len(windup.children), bot.BASEBALL_BUTTON_SLOT_COUNT)
        self.assertTrue(all(button.disabled for button in windup.children))
        self.assertTrue(
            all(button.style == bot.discord.ButtonStyle.secondary for button in windup.children)
        )

        swing = bot.baseball_swing_view(7, 2, button_position=7)
        self.assertEqual(len(swing.children), bot.BASEBALL_BUTTON_SLOT_COUNT)
        active_buttons = [button for button in swing.children if not button.disabled]
        self.assertEqual(len(active_buttons), 1)
        self.assertIs(active_buttons[0], swing.children[7])
        self.assertEqual(active_buttons[0].label, "치기")
        self.assertEqual(active_buttons[0].custom_id, "eventbot:baseballswing:7:2")
        self.assertTrue(
            all(button.disabled for index, button in enumerate(swing.children) if index != 7)
        )
        self.assertEqual(
            [button.row for button in swing.children],
            [0, 0, 0, 1, 1, 1, 2, 2, 2],
        )
        self.assertIn("야구 조작판", bot.baseball_control_content(2, "swinging"))
        self.assertIn("<2구/3구>", bot.baseball_control_content(2, "swinging"))

    def test_baseball_pitch_ranks_clicks_without_awarding_points(self) -> None:
        round_id = self.create_baseball_round()
        bot.prepare_baseball_pitch(round_id, "999")
        transition = bot.begin_baseball_pitch(round_id, "999", started_at=100.0)
        self.assertEqual(transition["result_at"], 100.0 + bot.BASEBALL_RESULT_SECONDS)

        click_times = [100.10, 100.40, 100.90, 101.20, 103.00]
        for user_id, clicked_at in enumerate(click_times, start=1):
            bot.record_baseball_swing(
                round_id,
                1,
                self.baseball_user(user_id),
                swung_at=clicked_at,
            )

        result = bot.settle_baseball_pitch(round_id, 1)
        self.assertIsNotNone(result)
        self.assertEqual(
            [(row["rank"], row["score"]) for row in result["swings"]],
            [(1, 0), (2, 0), (3, 0), (4, 0), (None, 0)],
        )
        with bot.db() as conn:
            scores = {
                row["user_id"]: row["total_score"]
                for row in conn.execute("SELECT user_id, total_score FROM users").fetchall()
            }
            extra = bot.round_extra(
                conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
            )
        self.assertEqual(scores, {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0})
        self.assertEqual(extra["baseball_phase"], "result_ready")

        # 개별 투구에서는 순위만 확정하고 점수를 지급하지 않는다.
        bot.settle_baseball_pitch(round_id, 1)
        with bot.db() as conn:
            score_log_count = conn.execute(
                "SELECT COUNT(*) AS count FROM score_logs WHERE round_id = ?",
                (round_id,),
            ).fetchone()["count"]
        self.assertEqual(score_log_count, 0)

        advanced = bot.advance_baseball_pitch(round_id, 1, "30", "40")
        self.assertFalse(advanced["complete"])
        self.assertEqual(advanced["extra"]["baseball_pitch"], 2)
        self.assertEqual(advanced["extra"]["baseball_phase"], "waiting_prepare")
        self.assertEqual(advanced["extra"]["baseball_control_message_id"], "40")

    def test_baseball_pitch_result_shows_times_without_settlement(self) -> None:
        lines = bot.baseball_result_lines(
            [
                {"user_id": "1", "display_name": "타자1"},
                {"user_id": "2", "display_name": "타자2"},
            ],
            [
                {
                    "user_id": "1",
                    "display_name": "타자1",
                    "elapsed_seconds": 0.25,
                    "rank": 1,
                    "score": 0,
                }
            ],
        )

        self.assertEqual(
            lines,
            ["🏏 타자1 · **0.25초**", "💨 타자2 · **7.00초** (미입력)"],
        )
        self.assertNotIn("위", "".join(lines))
        self.assertNotIn("점", "".join(lines))

    def test_baseball_rejects_replaced_swing_control_board(self) -> None:
        round_id = self.create_baseball_round(1)
        bot.prepare_baseball_pitch(round_id, "999")
        bot.begin_baseball_pitch(round_id, "999", started_at=100.0)
        bot.set_baseball_pitch_visible_at(round_id, 1, 100.0, "100")
        with self.assertRaisesRegex(ValueError, "최신 버튼"):
            bot.record_baseball_swing(
                round_id,
                1,
                self.baseball_user(1),
                swung_at=100.1,
                control_message_id="99",
            )

    def test_baseball_repeated_click_keeps_first_time(self) -> None:
        round_id = self.create_baseball_round(1)
        bot.prepare_baseball_pitch(round_id, "999")
        bot.begin_baseball_pitch(round_id, "999", started_at=100.0)

        first = bot.record_baseball_swing(
            round_id, 1, self.baseball_user(1), swung_at=100.25
        )
        repeated = bot.record_baseball_swing(
            round_id, 1, self.baseball_user(1), swung_at=100.10
        )

        self.assertFalse(first["already"])
        self.assertTrue(repeated["already"])
        self.assertAlmostEqual(repeated["elapsed_seconds"], 0.25)
        with bot.db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM baseball_swings WHERE round_id = ?",
                (round_id,),
            ).fetchone()["count"]
        self.assertEqual(count, 1)

    def test_baseball_timing_starts_when_button_edit_is_visible(self) -> None:
        round_id = self.create_baseball_round(1)
        bot.prepare_baseball_pitch(round_id, "999")
        bot.begin_baseball_pitch(round_id, "999", started_at=100.0)

        timing = bot.set_baseball_pitch_visible_at(
            round_id,
            1,
            101.25,
            "100",
            8,
        )
        swing = bot.record_baseball_swing(
            round_id, 1, self.baseball_user(1), swung_at=101.75
        )

        self.assertEqual(timing["result_at"], 101.25 + bot.BASEBALL_RESULT_SECONDS)
        self.assertAlmostEqual(swing["elapsed_seconds"], 0.50)
        with bot.db() as conn:
            extra = bot.round_extra(
                conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
            )
        self.assertEqual(extra["baseball_button_position"], 8)

    def test_baseball_windup_activates_one_random_grid_button(self) -> None:
        round_id = self.create_baseball_round(1)
        bot.prepare_baseball_pitch(round_id, "999", activate_at=103.0)

        activated_message = SimpleNamespace(
            id=99,
            edited_at=SimpleNamespace(timestamp=lambda: 103.0),
        )
        control_message = SimpleNamespace(
            edit=AsyncMock(return_value=activated_message),
        )
        main_message = SimpleNamespace(edit=AsyncMock())

        async def fetch_message(message_id: int) -> SimpleNamespace:
            return control_message if message_id == 99 else main_message

        channel = SimpleNamespace(fetch_message=AsyncMock(side_effect=fetch_message))
        with (
            patch.object(
                bot,
                "resolve_text_channel",
                AsyncMock(return_value=channel),
            ),
            patch.object(bot.random, "randrange", return_value=7),
            patch.object(bot.bot, "schedule_baseball_pitch") as schedule_result,
        ):
            activated = asyncio.run(
                bot.activate_baseball_pitch(round_id, 1, expected_activate_at=103.0)
            )

        self.assertTrue(activated)
        active_view = control_message.edit.await_args.kwargs["view"]
        active_buttons = [button for button in active_view.children if not button.disabled]
        self.assertEqual(len(active_buttons), 1)
        self.assertIs(active_buttons[0], active_view.children[7])
        schedule_result.assert_called_once_with(
            round_id,
            103.0 + bot.BASEBALL_RESULT_SECONDS,
            1,
        )
        with bot.db() as conn:
            extra = bot.round_extra(
                conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
            )
        self.assertEqual(extra["baseball_phase"], "swinging")
        self.assertEqual(extra["baseball_button_position"], 7)
        self.assertNotIn("baseball_activate_at", extra)

    def test_baseball_prepare_posts_disabled_grid_and_schedules_three_seconds(self) -> None:
        round_id = self.create_baseball_round(1)
        control_message = SimpleNamespace(
            id=99,
            edited_at=SimpleNamespace(timestamp=lambda: 100.0),
            created_at=SimpleNamespace(timestamp=lambda: 99.0),
        )
        control_message.edit = AsyncMock(return_value=control_message)
        main_message = SimpleNamespace(edit=AsyncMock())

        async def fetch_message(message_id: int) -> SimpleNamespace:
            return control_message if message_id == 99 else main_message

        channel = SimpleNamespace(
            fetch_message=AsyncMock(side_effect=fetch_message),
            send=AsyncMock(),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=999),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with (
            patch.object(
                bot,
                "resolve_text_channel",
                AsyncMock(return_value=channel),
            ),
            patch.object(bot.bot, "schedule_baseball_activation") as schedule_activation,
        ):
            asyncio.run(bot.baseball_prepare.callback(interaction))

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        channel.send.assert_not_awaited()
        windup_view = control_message.edit.await_args.kwargs["view"]
        self.assertTrue(all(button.disabled for button in windup_view.children))
        self.assertIn("투수 와인드업", control_message.edit.await_args.kwargs["content"])
        schedule_activation.assert_called_once_with(round_id, 103.0, 1)
        with bot.db() as conn:
            extra = bot.round_extra(
                conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
            )
        self.assertEqual(extra["baseball_phase"], "winding_up")
        self.assertEqual(extra["baseball_activate_at"], 103.0)

    def test_baseball_windup_timer_is_restored_after_restart(self) -> None:
        round_id = self.create_baseball_round(1)
        bot.prepare_baseball_pitch(round_id, "999", activate_at=123.0)

        with (
            patch.object(bot.bot, "schedule_baseball_activation") as schedule_activation,
            patch.object(bot.bot, "schedule_baseball_pitch") as schedule_result,
        ):
            bot.bot.restore_baseball_pitch_tasks()

        schedule_activation.assert_called_once_with(round_id, 123.0, 1)
        schedule_result.assert_not_called()

    def test_baseball_swing_success_uses_private_confirmation_message(self) -> None:
        round_id = self.create_baseball_round(1)
        bot.prepare_baseball_pitch(round_id, "999")
        bot.begin_baseball_pitch(round_id, "999", started_at=100.0)
        interaction = SimpleNamespace(
            user=self.baseball_user(1),
            created_at=SimpleNamespace(timestamp=lambda: 100.25),
            message=SimpleNamespace(
                id=99,
                edited_at=None,
                created_at=SimpleNamespace(timestamp=lambda: 100.0),
            ),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        )

        # 봇 서버 시계가 Discord보다 느려도 두 Discord 타임스탬프의 차이를 써야 한다.
        with patch.object(bot.time, "time", return_value=99.0):
            asyncio.run(bot.handle_baseball_swing(interaction, round_id, 1))

        interaction.response.defer.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()
        args, kwargs = interaction.response.send_message.await_args
        self.assertIn("0.25초", args[0])
        self.assertTrue(kwargs["ephemeral"])

    def test_baseball_result_reposts_next_control_board_below_result(self) -> None:
        round_id = self.create_baseball_round(1)
        bot.prepare_baseball_pitch(round_id, "999")
        bot.begin_baseball_pitch(round_id, "999", started_at=100.0)
        bot.record_baseball_swing(
            round_id, 1, self.baseball_user(1), swung_at=100.25
        )

        old_control = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        main_message = SimpleNamespace(edit=AsyncMock())
        result_message = SimpleNamespace(id=30)
        next_control = SimpleNamespace(id=40, delete=AsyncMock())

        async def fetch_message(message_id: int) -> SimpleNamespace:
            return old_control if message_id == 99 else main_message

        channel = SimpleNamespace(
            guild=None,
            fetch_message=AsyncMock(side_effect=fetch_message),
            send=AsyncMock(side_effect=[result_message, next_control]),
        )
        with patch.object(
            bot,
            "resolve_text_channel",
            AsyncMock(return_value=channel),
        ):
            published = asyncio.run(bot.publish_baseball_pitch_result(round_id, 1))

        self.assertTrue(published)
        self.assertEqual(channel.send.await_count, 2)
        self.assertIsNotNone(channel.send.await_args_list[0].kwargs.get("embed"))
        self.assertEqual(
            channel.send.await_args_list[1].kwargs["content"],
            bot.baseball_control_content(2, "waiting_prepare"),
        )
        self.assertIsNone(channel.send.await_args_list[1].kwargs["view"])
        old_control.delete.assert_awaited_once_with()
        with bot.db() as conn:
            extra = bot.round_extra(
                conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
            )
        self.assertEqual(extra["baseball_control_message_id"], "40")
        self.assertEqual(extra["baseball_pitch"], 2)

    def test_baseball_third_pitch_result_is_followed_by_final_settlement(self) -> None:
        round_id = self.create_baseball_round(1)
        for pitch_number in (1, 2):
            started_at = 100.0 * pitch_number
            bot.prepare_baseball_pitch(round_id, "999")
            bot.begin_baseball_pitch(round_id, "999", started_at=started_at)
            bot.record_baseball_swing(
                round_id,
                pitch_number,
                self.baseball_user(1),
                swung_at=started_at + 0.2,
            )
            bot.settle_baseball_pitch(round_id, pitch_number)
            bot.advance_baseball_pitch(
                round_id,
                pitch_number,
                str(30 + pitch_number),
                str(99 + pitch_number),
            )

        bot.prepare_baseball_pitch(round_id, "999")
        bot.begin_baseball_pitch(round_id, "999", started_at=300.0)
        bot.record_baseball_swing(
            round_id,
            3,
            self.baseball_user(1),
            swung_at=300.2,
        )

        old_control = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        main_message = SimpleNamespace(edit=AsyncMock())
        pitch_result_message = SimpleNamespace(id=33)
        final_result_message = SimpleNamespace(id=34)

        async def fetch_message(message_id: int) -> SimpleNamespace:
            return old_control if message_id == 101 else main_message

        channel = SimpleNamespace(
            guild=None,
            fetch_message=AsyncMock(side_effect=fetch_message),
            send=AsyncMock(side_effect=[pitch_result_message, final_result_message]),
        )
        with (
            patch.object(
                bot,
                "resolve_text_channel",
                AsyncMock(return_value=channel),
            ),
            patch.object(
                bot,
                "publish_event_summary_if_complete",
                AsyncMock(),
            ),
        ):
            published = asyncio.run(bot.publish_baseball_pitch_result(round_id, 3))

        self.assertTrue(published)
        self.assertEqual(channel.send.await_count, 2)
        self.assertIn("3구 결과", channel.send.await_args_list[0].kwargs["embed"].title)
        self.assertIn("최종 결과", channel.send.await_args_list[1].kwargs["embed"].title)
        with bot.db() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT total_score FROM users WHERE user_id = '1'"
                ).fetchone()["total_score"],
                3,
            )

    def test_baseball_rejects_non_participant(self) -> None:
        round_id = self.create_baseball_round(1)
        bot.prepare_baseball_pitch(round_id, "999")
        bot.begin_baseball_pitch(round_id, "999", started_at=100.0)

        with self.assertRaisesRegex(PermissionError, "참여하기"):
            bot.record_baseball_swing(
                round_id, 1, self.baseball_user(2), swung_at=100.10
            )

    def test_baseball_closes_after_exactly_three_pitches_without_double_scoring(self) -> None:
        round_id = self.create_baseball_round(1)
        for pitch_number in range(1, bot.BASEBALL_TOTAL_PITCHES + 1):
            bot.prepare_baseball_pitch(round_id, "999")
            bot.begin_baseball_pitch(round_id, "999", started_at=100.0 * pitch_number)
            bot.record_baseball_swing(
                round_id,
                pitch_number,
                self.baseball_user(1),
                swung_at=(100.0 * pitch_number) + 0.2,
            )
            bot.settle_baseball_pitch(round_id, pitch_number)
            advanced = bot.advance_baseball_pitch(
                round_id, pitch_number, str(30 + pitch_number)
            )

        self.assertTrue(advanced["complete"])
        result = bot.close_round_record(round_id, None, closed_by="3구 완료")
        self.assertIsNotNone(result)
        rendered = json.dumps(result[1].to_dict(), ensure_ascii=False)
        self.assertIn("평균 **0.20초**", rendered)
        self.assertNotIn("💰 정산", rendered)
        with bot.db() as conn:
            user_score = conn.execute(
                "SELECT total_score FROM users WHERE user_id = '1'"
            ).fetchone()["total_score"]
            round_status = conn.execute(
                "SELECT status FROM rounds WHERE id = ?", (round_id,)
            ).fetchone()["status"]
            score_logs = conn.execute(
                "SELECT COUNT(*) AS count FROM score_logs WHERE round_id = ?",
                (round_id,),
            ).fetchone()["count"]
        self.assertEqual(user_score, 3)
        self.assertEqual(round_status, "closed")
        self.assertEqual(score_logs, 1)

    def test_baseball_final_ranking_counts_missing_pitch_as_seven_seconds(self) -> None:
        round_id = self.create_baseball_round(5)
        elapsed_by_pitch = {
            1: {1: 0.30, 2: 0.40, 3: 0.50, 4: 0.10},
            2: {1: 0.30, 2: 0.20, 3: 0.50, 4: 0.10},
            3: {1: 0.30, 2: 0.60, 3: 0.80},
        }
        for pitch_number, elapsed_by_user in elapsed_by_pitch.items():
            started_at = 100.0 * pitch_number
            bot.prepare_baseball_pitch(round_id, "999")
            bot.begin_baseball_pitch(round_id, "999", started_at=started_at)
            for user_id, elapsed in elapsed_by_user.items():
                bot.record_baseball_swing(
                    round_id,
                    pitch_number,
                    self.baseball_user(user_id),
                    swung_at=started_at + elapsed,
                )
            bot.settle_baseball_pitch(round_id, pitch_number)
            bot.advance_baseball_pitch(round_id, pitch_number, str(40 + pitch_number))

        result = bot.close_round_record(round_id, None, closed_by="3구 완료")
        self.assertIsNotNone(result)
        rendered = json.dumps(result[1].to_dict(), ensure_ascii=False)
        self.assertIn("타자1 · 평균 **0.30초** · +3점", rendered)
        self.assertIn("타자2 · 평균 **0.40초** · +2점", rendered)
        self.assertIn("타자3 · 평균 **0.60초** · +1점", rendered)
        self.assertIn("타자4 · 평균 **2.40초** · +0점 · 미입력 1구는 7.00초 반영", rendered)
        self.assertIn("타자5 · 평균 **7.00초** · +0점 · 미입력 3구는 7.00초 반영", rendered)
        with bot.db() as conn:
            scores = {
                row["user_id"]: row["total_score"]
                for row in conn.execute(
                    "SELECT user_id, total_score FROM users ORDER BY user_id"
                ).fetchall()
            }
            score_log_count = conn.execute(
                "SELECT COUNT(*) AS count FROM score_logs WHERE round_id = ?",
                (round_id,),
            ).fetchone()["count"]
        self.assertEqual(scores, {"1": 3, "2": 2, "3": 1, "4": 0, "5": 0})
        self.assertEqual(score_log_count, 3)


if __name__ == "__main__":
    unittest.main()
