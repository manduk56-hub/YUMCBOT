from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import bot
from yumcbot.games import cutting


class CuttingGameTests(unittest.TestCase):
    """돌 세공(yumcbot/games/cutting.py) 회귀 테스트."""

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
        # 상수를 읽는 것은 cutting 모듈이다. bot의 재노출은 사본이라 갈아끼워도 안 통한다.
        with patch.object(
            cutting, "CUTTING_REWARD_POINTS", {0: 5, 1: 5, 2: 5, 3: 5, 4: 5, 5: 5, 6: 5}
        ):
            self.assertIn("0~6강화 +5점", bot.cutting_reward_text())


if __name__ == "__main__":
    unittest.main()
