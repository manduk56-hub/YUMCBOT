"""게임 레지스트리가 실제 저장·조회 경로와 어긋나지 않는지 고정한다.

여기서 잡으려는 실패는 전부 **조용한** 것들이다. 게임 이름과 rounds.type이 어긋나도
라운드는 정상으로 열리고 버튼도 동작하다가 정산에서만 빠지고, 분류를 잘못 달면
다음 미니게임 안내가 사라진다. 어느 쪽도 예외를 내지 않는다.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from yumcbot import config
from yumcbot.games import registry
from yumcbot.games.registry import MAIN, MINI, GameSpec


class GameSpecInvariantTests(unittest.TestCase):
    def test_derived_key_matches_round_game_key(self) -> None:
        """spec의 (round_type, mode)로 만든 행을 round_game_key가 그 키로 되읽어야 한다.

        이게 깨지면 라운드를 어느 게임으로 정산할지 알 수 없어진다. spec 정의 시점에도
        막고 있지만, 여기서는 실제 조회 함수를 태워 왕복을 확인한다.
        """
        for key, spec in registry.GAMES.items():
            with self.subTest(game=key):
                extra: dict[str, str] = {}
                if spec.mode:
                    extra["mode"] = spec.mode
                row = {"type": spec.round_type, "extra_json": json.dumps(extra)}
                self.assertEqual(bot.round_game_key(row), key)

    def test_spec_rejects_key_that_cannot_be_read_back(self) -> None:
        with self.assertRaises(ValueError):
            # mode가 없으면 round_game_key는 'grid'를 돌려주므로 이 키는 찾을 수 없다.
            GameSpec("nowhere", "못 찾는 게임", MINI, "grid")

    def test_spec_rejects_unknown_category(self) -> None:
        with self.assertRaises(ValueError):
            GameSpec("balance", "밸런스 게임", "bonus", "balance")

    def test_main_game_keys_double_as_round_types(self) -> None:
        """호출부가 `round_row["type"] in MAIN_GAME_TYPES`로도 검사한다.

        MAIN_GAME_TYPES는 게임 **키** 집합인데, 메인게임의 round_type이 마침 전부 그
        집합 안에 있어서 type으로 검사해도 통과한다(bail은 type='sand'). 새 메인게임을
        다른 저장 타입으로 추가하면 그 검사만 조용히 False가 되어 다음 미니게임 안내와
        참가자 수 검사가 사라진다.
        """
        for key in registry.MAIN_GAME_TYPES:
            with self.subTest(game=key):
                self.assertIn(registry.GAMES[key].round_type, registry.MAIN_GAME_TYPES)

    def test_registry_matches_known_game_list(self) -> None:
        """게임 목록이 조용히 늘거나 줄지 않게 값으로 고정한다."""
        self.assertEqual(
            {key: (spec.round_type, spec.mode, spec.category) for key, spec in registry.GAMES.items()},
            {
                "balance": ("balance", None, MINI),
                "minority_balance": ("balance", "minority_balance", MINI),
                "defuse4": ("bomb", "defuse4", MINI),
                "cutting": ("cutting", None, MINI),
                "top3": ("top3", None, MINI),
                "unique_number": ("grid", "unique_number", MINI),
                "baseball": ("grid", "baseball", MINI),
                "trust": ("trust", None, MAIN),
                "sand": ("sand", None, MAIN),
                "bail": ("sand", "bail", MAIN),
            },
        )

    def test_bot_reexports_registry_names(self) -> None:
        self.assertIs(bot.GAME_NAMES, registry.GAME_NAMES)
        self.assertIs(bot.MAIN_GAME_TYPES, registry.MAIN_GAME_TYPES)
        self.assertIs(bot.MINI_GAME_TYPES, registry.MINI_GAME_TYPES)


class RoundTypeConstraintTests(unittest.TestCase):
    """spec의 round_type이 rounds.type CHECK 제약을 실제로 통과하는지 본다.

    BASE_SCHEMA는 불변이라 제약을 넓힐 수 없다. 제약 밖의 타입을 단 게임은 라운드
    생성 시점에야 IntegrityError로 터지는데, 그때는 이미 운영 중이다.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            config, "DB_PATH", Path(self.temp_dir.name) / "test.sqlite3"
        )
        self.db_patch.start()
        bot.init_db()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_every_round_type_is_accepted_by_the_check_constraint(self) -> None:
        for key, spec in registry.GAMES.items():
            with self.subTest(game=key), bot.db() as conn:
                conn.execute(
                    """
                    INSERT INTO rounds (type, title, question, status, channel_id, created_at)
                    VALUES (?, ?, ?, 'closed', '1', '2026-01-01 00:00:00')
                    """,
                    (spec.round_type, key, key),
                )

    def test_constraint_still_rejects_an_unknown_type(self) -> None:
        """위 검사가 제약이 사라져서 통과한 것은 아닌지 확인한다."""
        with self.assertRaises(sqlite3.IntegrityError), bot.db() as conn:
            conn.execute(
                """
                INSERT INTO rounds (type, title, question, status, channel_id, created_at)
                VALUES ('nosuchtype', 'x', 'x', 'closed', '1', '2026-01-01 00:00:00')
                """
            )


class SettlerRegistrationTests(unittest.TestCase):
    def test_registered_settlers_are_known_games(self) -> None:
        for key in registry._settlers:
            self.assertIn(key, registry.GAMES)

    def test_registering_an_unknown_game_is_rejected(self) -> None:
        with self.assertRaises(KeyError):
            registry.register_settler("nosuchgame", lambda ctx: None)

    def test_baseball_settles_through_the_registry(self) -> None:
        """야구는 close_round_record의 if/elif가 아니라 훅으로 정산된다.

        3단계에서 새 경로로 옮긴 첫 게임이다. 이 등록이 빠지면 야구 라운드는
        '더 이상 지원하지 않는 게임'으로 점수 없이 닫힌다 — 예외는 나지 않는다.
        """
        self.assertIs(registry.settler_for("baseball"), bot.settle_baseball)


if __name__ == "__main__":
    unittest.main()
