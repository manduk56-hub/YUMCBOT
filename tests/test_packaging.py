"""패키지 분할이 진행되는 동안의 배선 회귀 테스트.

bot.py는 아직 yumcbot.config / yumcbot.db의 이름을 재노출한다. 재노출은 '사본'이라
원본과 조용히 갈라질 수 있는데, 갈라졌을 때의 증상이 최악이다 — 테스트는 임시 DB로
갈아끼웠다고 믿지만 실제 연결은 원래 경로로 간다. 즉 리팩터링 도중 실수가 나면
'테스트가 통과하면서 개발/운영 DB를 건드리는' 형태로 나타난다.

그래서 여기서 보는 것은 기능이 아니라 배선이다.
"""

from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from yumcbot import activity, config, rounds, roulette, runtime, scoring
from yumcbot import db as schema
from yumcbot.games import registry

# bot.py가 재노출하는 모듈들. 새로 추출할 때마다 여기에 추가한다.
EXTRACTED = (config, schema, activity, scoring, roulette, registry, rounds)


def public_names(module: types.ModuleType) -> set[str]:
    """모듈이 제공하는 이름 중 다른 데서 가져온 것과 비공개를 뺀 것."""
    return {
        name
        for name, value in vars(module).items()
        if not name.startswith("_")
        and not isinstance(value, types.ModuleType)
        and getattr(value, "__module__", module.__name__) == module.__name__
    }


class ReexportWiringTest(unittest.TestCase):
    def test_bot_reexports_are_the_same_objects(self) -> None:
        """bot이 재노출한 이름은 원본과 같은 객체여야 한다 (사본이 아니라)."""
        mismatched = []
        for module in EXTRACTED:
            for name in public_names(module) | {"db", "init_db", "verify_schema"}:
                if not hasattr(module, name) or not hasattr(bot, name):
                    continue
                if getattr(bot, name) is not getattr(module, name):
                    mismatched.append(f"{module.__name__}.{name}")
        self.assertEqual(
            mismatched,
            [],
            "bot의 재노출이 원본과 갈라졌습니다. 이 이름들은 값을 복사하지 말고 "
            "import로 다시 노출하거나, 모듈을 통해 조회하세요.",
        )

    def test_bot_does_not_shadow_db_path(self) -> None:
        """경로는 config를 통해서만 읽는다.

        bot.DB_PATH 같은 사본이 생기면 patch.object(bot, "DB_PATH")가 통하는 것처럼
        보이지만 실제 연결은 config.DB_PATH를 따라간다. 사본 자체를 두지 않는다.
        """
        for name in ("DB_PATH", "DATA_DIR", "BASE_DIR"):
            self.assertFalse(
                hasattr(bot, name),
                f"bot.{name}이 다시 생겼습니다. config.{name}을 직접 조회하세요.",
            )

    def test_bot_does_not_shadow_resolve_text_channel(self) -> None:
        """채널 조회도 사본을 두지 않는다 — DB_PATH와 같은 계열의 함정이다.

        테스트는 `patch.object(runtime, "resolve_text_channel")`로 가짜 채널을 넣는다.
        bot이나 게임 모듈이 이름을 복사해 가지고 있으면 그 사본은 갈아끼워지지 않아,
        가짜를 넣었다고 믿는 테스트가 실제 클라이언트를 부른다. 항상 모듈을 거쳐 부른다.
        """
        self.assertFalse(
            hasattr(bot, "resolve_text_channel"),
            "bot.resolve_text_channel 사본이 생겼습니다. "
            "runtime.resolve_text_channel(...)처럼 모듈을 거쳐 부르세요.",
        )

    def test_client_is_registered_for_game_modules(self) -> None:
        """게임 모듈은 타이머 콜백에서 이 클라이언트로 채널을 연다.

        등록이 빠지면 사용자 클릭이 없는 경로(자동 마감, 야구 투구 타이머)만 조용히
        죽는다 — 버튼으로 하는 조작은 멀쩡해서 알아채기 어렵다.
        """
        self.assertIs(runtime.get_client(), bot.bot)

    def test_db_connection_follows_patched_config_path(self) -> None:
        """config.DB_PATH를 갈아끼우면 bot 경유로 부른 db()도 그 경로를 열어야 한다."""
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "wiring.sqlite3"
            with patch.object(config, "DB_PATH", target):
                with bot.db() as conn:
                    conn.execute("CREATE TABLE probe (id INTEGER)")
            self.assertTrue(target.exists(), "db()가 patch된 경로를 쓰지 않았습니다.")


class RouletteTimerProtocolTest(unittest.TestCase):
    """룰렛 모듈은 EventBot을 구체 타입으로 알지 못한다 (알면 순환 import).

    대신 프로토콜로 '타이머를 걸 수 있는 클라이언트인가'만 확인하는데, EventBot 쪽에서
    메서드 이름이 바뀌면 isinstance가 조용히 False가 된다. 그러면 차례 타이머가 걸리지
    않고 — 예외도 로그도 없이 — 예치금이 영구 동결되고 상대까지 묶인다.
    증상이 없는 실패라서 여기서 붙잡는다.
    """

    def test_event_bot_satisfies_timer_protocol(self) -> None:
        self.assertIsInstance(bot.bot, roulette.RouletteTimerClient)

    def test_protocol_rejects_unrelated_objects(self) -> None:
        """메서드 이름만 보는 프로토콜이라 아무 객체나 통과하지 않는지 확인한다."""

        class Unrelated:
            pass

        self.assertNotIsInstance(Unrelated(), roulette.RouletteTimerClient)

    def test_turn_timer_restore_is_registered_on_ready(self) -> None:
        """라운드 타이머(restore_auto_close_tasks)와 별개라 따로 등록해야 한다.

        빠뜨리면 재시작 순간 타이머가 사라져 위와 같은 동결이 재현된다.
        """
        self.assertIn(
            "restore_roulette_turn_tasks", bot.EventBot.on_ready.__code__.co_names
        )


class EntrypointTest(unittest.TestCase):
    """systemd ExecStart / CI / run.ps1이 저장소 루트의 bot.py를 경로로 물고 있다.

    yumcbot.service는 배포로 갱신되지 않으므로(AGENTS.md), 이 파일이 없어지거나
    엔트리포인트가 사라지면 서버에서 bootstrap.sh를 다시 돌려야 한다.
    """

    def test_bot_module_exposes_main(self) -> None:
        self.assertTrue(callable(bot.main))


if __name__ == "__main__":
    unittest.main()
