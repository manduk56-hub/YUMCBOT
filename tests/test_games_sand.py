from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from yumcbot import config


class SandGameTests(unittest.TestCase):
    """모래성(yumcbot/games/sand.py) 회귀 테스트."""

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

    def score_of(self, user_id: str) -> int:
        with bot.db() as conn:
            return int(
                conn.execute(
                    "SELECT total_score FROM users WHERE user_id = ?", (user_id,)
                ).fetchone()["total_score"]
            )

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
