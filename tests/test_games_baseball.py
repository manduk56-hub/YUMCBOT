from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from yumcbot import config, runtime


class BaseballGameTests(unittest.TestCase):
    """야구(yumcbot/games/baseball.py) 회귀 테스트."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        # patch 대상이 config인 이유는 test_bot.py setUp의 주석 참고.
        self.db_patch = patch.object(config, "DB_PATH", Path(self.temp_dir.name) / "test.sqlite3")
        self.db_patch.start()
        bot.init_db()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

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
                runtime,
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
                runtime,
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
            runtime,
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
                runtime,
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
