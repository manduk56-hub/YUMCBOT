from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from yumcbot import config


class RouletteTests(unittest.TestCase):
    """러시안룰렛(yumcbot/roulette.py) 회귀 테스트."""

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


if __name__ == "__main__":
    unittest.main()
