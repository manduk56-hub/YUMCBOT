from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from yumcbot import config


class BotRegressionTests(unittest.TestCase):
    """bot.py에 아직 직접 남아 있는 로직 — 이벤트 취소·자동 마감 카운트다운·긴급 투입."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        # patch 대상이 config인 이유는 test_bot.py setUp의 주석 참고.
        self.db_patch = patch.object(config, "DB_PATH", Path(self.temp_dir.name) / "test.sqlite3")
        self.db_patch.start()
        bot.init_db()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_closed_season_allows_only_score_slash_command(self) -> None:
        with bot.db() as conn:
            conn.execute(
                "UPDATE season_state SET status = 'closed', closed_at = '2026-01-02' WHERE singleton_id = 1"
            )

        score_interaction = SimpleNamespace(command=SimpleNamespace(name="점수"))
        game_interaction = SimpleNamespace(command=SimpleNamespace(name="러시안룰렛"))
        self.assertTrue(asyncio.run(bot.bot.tree.interaction_check(score_interaction)))
        with self.assertRaises(bot.app_commands.CheckFailure) as raised:
            asyncio.run(bot.bot.tree.interaction_check(game_interaction))
        self.assertEqual(str(raised.exception), bot.SEASON_CLOSED_MESSAGE)

    def test_closed_season_rejects_existing_component_buttons(self) -> None:
        with bot.db() as conn:
            conn.execute(
                "UPDATE season_state SET status = 'closed', closed_at = '2026-01-02' WHERE singleton_id = 1"
            )
        interaction = SimpleNamespace(
            type=bot.discord.InteractionType.component,
            data={"custom_id": "eventbot:eventjoin:1"},
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        asyncio.run(bot.bot.on_interaction(interaction))

        interaction.response.send_message.assert_awaited_once_with(
            bot.SEASON_CLOSED_MESSAGE,
            ephemeral=True,
        )

    def test_season_end_button_rejects_non_operator(self) -> None:
        interaction = SimpleNamespace(
            type=bot.discord.InteractionType.component,
            data={"custom_id": "eventbot:seasonend:confirm"},
            user=SimpleNamespace(
                guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
                roles=[],
            ),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        asyncio.run(bot.bot.on_interaction(interaction))

        interaction.response.send_message.assert_awaited_once_with(
            "이 버튼은 관리자 권한 또는 운영자 역할이 필요합니다.",
            ephemeral=True,
        )

    def test_season_end_cancel_button_dispatches_for_operator(self) -> None:
        interaction = SimpleNamespace(
            type=bot.discord.InteractionType.component,
            data={"custom_id": "eventbot:seasonend:cancel"},
            user=SimpleNamespace(
                guild_permissions=SimpleNamespace(administrator=True, manage_guild=False),
                roles=[],
            ),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        with patch.object(
            bot.ranking, "cancel_season_end", new=AsyncMock()
        ) as cancel_handler:
            asyncio.run(bot.bot.on_interaction(interaction))

        cancel_handler.assert_awaited_once_with(interaction)
        interaction.response.send_message.assert_not_awaited()

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


if __name__ == "__main__":
    unittest.main()
