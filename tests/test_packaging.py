"""패키지 분할이 진행되는 동안의 배선 회귀 테스트.

bot.py는 아직 yumcbot.config / yumcbot.db의 이름을 재노출한다. 재노출은 '사본'이라
원본과 조용히 갈라질 수 있는데, 갈라졌을 때의 증상이 최악이다 — 테스트는 임시 DB로
갈아끼웠다고 믿지만 실제 연결은 원래 경로로 간다. 즉 리팩터링 도중 실수가 나면
'테스트가 통과하면서 개발/운영 DB를 건드리는' 형태로 나타난다.

그래서 여기서 보는 것은 기능이 아니라 배선이다.
"""

from __future__ import annotations

import ast
import builtins
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from yumcbot import (
    activity,
    config,
    discussion,
    events,
    permissions,
    ranking,
    rounds,
    roulette,
    runtime,
    scoring,
    ui,
)
from yumcbot import db as schema
from yumcbot.games import (
    bail,
    balance,
    baseball,
    cutting,
    defuse,
    pairing,
    registry,
    sand,
    top3,
    trust,
    unique_number,
)

# bot.py가 재노출하는 모듈들. 새로 추출할 때마다 여기에 추가한다.
EXTRACTED = (
    config, schema, activity, scoring, roulette, registry, rounds, ui, pairing,
    permissions, discussion, events, ranking,
    bail, sand, trust, baseball, balance, cutting, defuse, top3, unique_number,
)


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


class BaseballWiringTest(unittest.TestCase):
    """야구는 게임 모듈로 옮긴 첫 게임이라 bot.py와 이어지는 지점이 여럿이다.

    어느 하나가 끊겨도 예외는 나지 않고 해당 경로만 조용히 죽는다.
    """

    def test_timer_protocol_is_satisfied_by_event_bot(self) -> None:
        """끊기면 투구 타이머가 안 걸려 와인드업에서 멈춘다."""
        self.assertIsInstance(bot.bot, baseball.BaseballTimerClient)

    def test_protocol_rejects_unrelated_objects(self) -> None:
        class Unrelated:
            pass

        self.assertNotIsInstance(Unrelated(), baseball.BaseballTimerClient)

    def test_round_services_are_registered(self) -> None:
        """3구를 다 던진 뒤 라운드를 닫는 통로. 빠지면 3구째 결과만 뜨고 정산이 없다."""
        self.assertIsInstance(runtime.round_services(), runtime.RoundServices)

    def test_pitch_timer_restore_is_registered_on_ready(self) -> None:
        """재시작 시 진행 중이던 구의 타이머를 되살린다 (라운드 타이머와 별개다)."""
        self.assertIn(
            "restore_baseball_pitch_tasks", bot.EventBot.on_ready.__code__.co_names
        )

    def test_prepare_command_is_registered_as_slash_command(self) -> None:
        """`/준비`가 야구를 진행시키는 유일한 입구다."""
        self.assertIsNotNone(bot.bot.tree.get_command("준비"))


class PairGameWiringTest(unittest.TestCase):
    """1:1 짝 게임(딜레마의 섬·모래성·가라앉는 배)이 EventBot과 이어지는 지점."""

    def test_board_protocol_is_satisfied_by_event_bot(self) -> None:
        """진행판 락과 자동 마감 타이머를 EventBot에서 빌려 쓴다.

        끊기면 두 사람이 동시에 눌렀을 때 진행판이 여러 개 남거나(락), 단계가
        제때 넘어가지 않는다(타이머). 둘 다 예외 없이 조용히 어긋난다.
        """
        self.assertIsInstance(bot.bot, pairing.PairBoardClient)

    def test_board_protocol_rejects_unrelated_objects(self) -> None:
        class Unrelated:
            pass

        self.assertNotIsInstance(Unrelated(), pairing.PairBoardClient)

    def test_every_game_settles_through_the_registry(self) -> None:
        """정산 훅이 없는 게임은 '더 이상 지원하지 않는 게임'으로 점수 없이 닫힌다.

        `close_round_record`에 게임별 분기가 더는 없으므로, 등록을 빠뜨리면 라운드가
        정상으로 진행되다가 마감에서만 조용히 0점이 된다.
        """
        for key in registry.GAMES:
            with self.subTest(game=key):
                self.assertIsNotNone(
                    registry.settler_for(key), f"'{key}'의 정산 훅이 등록되지 않았습니다."
                )

    def test_main_game_settlers_live_in_their_own_modules(self) -> None:
        for key, expected in (
            ("bail", bail.settle_bail),
            ("sand", sand.settle_sand),
            ("trust", trust.settle_trust),
            ("baseball", baseball.settle_baseball),
        ):
            with self.subTest(game=key):
                self.assertIs(registry.settler_for(key), expected)

    def test_every_game_creates_through_the_registry(self) -> None:
        """생성 훅이 없는 게임은 `/시작`·`/미니게임`에서 '아직 지원하지 않는 게임'으로 막힌다."""
        for key in registry.GAMES:
            with self.subTest(game=key):
                self.assertIsNotNone(
                    registry.creator_for(key), f"'{key}'의 생성 훅이 등록되지 않았습니다."
                )


class AutoCloseWiringTest(unittest.TestCase):
    """자동 마감 타이머의 게임별 단계 전이 훅이 EventBot과 이어지는 지점."""

    def test_scheduler_protocol_is_satisfied_by_event_bot(self) -> None:
        """1:1 방이 없는 게임(소수파 밸런스)도 다음 마감을 다시 걸 수 있어야 한다."""
        self.assertIsInstance(bot.bot, runtime.AutoCloseScheduler)

    def test_scheduler_protocol_rejects_unrelated_objects(self) -> None:
        class Unrelated:
            pass

        self.assertNotIsInstance(Unrelated(), runtime.AutoCloseScheduler)

    def test_baseball_timer_client_exposes_cancel(self) -> None:
        """라운드마감·이벤트종료가 야구 타이머 두 개를 한 번에 버리는 통로."""
        self.assertIsInstance(bot.bot, baseball.BaseballTimerClient)
        self.assertTrue(hasattr(bot.bot, "cancel_baseball_timers"))

    def test_registration_phase_handlers_are_registered(self) -> None:
        """등록 마감 단계가 있는 메인게임·소수파 밸런스는 자동 마감 훅이 반드시 있어야 한다.

        빠뜨리면 등록 마감 타이머가 울려도 다음 단계로 안 넘어가고 조용히 정산행이 된다.
        """
        for key in ("trust", "sand", "bail", "minority_balance"):
            with self.subTest(game=key):
                self.assertIsNotNone(
                    registry.auto_close_handler_for(key),
                    f"'{key}'의 자동 마감 전이 훅이 등록되지 않았습니다.",
                )


def unresolved_names(path: Path) -> list[str]:
    """모듈이 정의하지도 import 하지도 않은 채 읽는 이름을 모은다."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    defined = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            if not isinstance(node, ast.Lambda):
                defined.add(node.name)
            args = getattr(node, "args", None)
            if args:
                defined |= {
                    a.arg for a in args.args + args.kwonlyargs + args.posonlyargs
                }
                if args.vararg:
                    defined.add(args.vararg.arg)
                if args.kwarg:
                    defined.add(args.kwarg.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                defined.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, ast.Global):
            defined |= set(node.names)
    used = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    return sorted(used - defined)


class ImportCompletenessTest(unittest.TestCase):
    """모듈을 옮기다 import를 빠뜨리면 그 함수가 불릴 때까지 아무 일도 없다.

    파일은 정상으로 import 되고 테스트도 통과한다. 실제로는 이벤트 진행 중
    NameError로 터진다 — 실제로 `pairing.GAME_NAMES`가 이렇게 빠져서, `/start`로
    메인게임을 시작하는 경로만 죽어 있었다.
    """

    def test_no_module_reads_an_undefined_name(self) -> None:
        root = Path(bot.__file__).resolve().parent
        offenders = {}
        for path in sorted(root.glob("yumcbot/**/*.py")) + [root / "bot.py"]:
            missing = unresolved_names(path)
            if missing:
                offenders[path.relative_to(root).as_posix()] = missing
        self.assertEqual(offenders, {}, "import 되지 않은 이름을 읽는 모듈이 있습니다.")


class EntrypointTest(unittest.TestCase):
    """systemd ExecStart / CI / run.ps1이 저장소 루트의 bot.py를 경로로 물고 있다.

    yumcbot.service는 배포로 갱신되지 않으므로(AGENTS.md), 이 파일이 없어지거나
    엔트리포인트가 사라지면 서버에서 bootstrap.sh를 다시 돌려야 한다.
    """

    def test_bot_module_exposes_main(self) -> None:
        self.assertTrue(callable(bot.main))


if __name__ == "__main__":
    unittest.main()
