from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

import bot


class UiTests(unittest.TestCase):
    """임베드/뷰 표시 유틸(yumcbot/ui.py) 회귀 테스트."""

    def test_member_name_returns_none_on_cache_miss(self) -> None:
        """캐시 미스(멤버 없음/길드 없음)면 플레이스홀더가 아니라 None을 반환한다 (issue #19)."""
        self.assertIsNone(bot.member_name(None, "123"))
        guild = SimpleNamespace(get_member=lambda _uid: None)
        self.assertIsNone(bot.member_name(guild, "123"))
        member = SimpleNamespace(display_name="캐시된이름")
        guild_hit = SimpleNamespace(get_member=lambda _uid: member)
        self.assertEqual(bot.member_name(guild_hit, "123"), "캐시된이름")

    def test_close_time_text_shows_exact_discord_time_without_relative_time(self) -> None:
        rendered = bot.close_time_text(1_800_000_000.9)

        self.assertEqual(rendered, "<t:1800000000:F>")
        self.assertNotIn(":R>", rendered)
        self.assertEqual(bot.close_time_text(None), "수동 마감")

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


if __name__ == "__main__":
    unittest.main()
