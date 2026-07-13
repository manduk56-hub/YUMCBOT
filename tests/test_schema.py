"""스키마 마이그레이션 안전장치 테스트.

배포 파이프라인 전체가 다음 불변식에 기대고 있다.

  1. BASE_SCHEMA(v1)는 절대 수정하지 않는다. 고치면 새로 만든 DB와 이미 굴러가는
     운영 DB의 스키마가 조용히 갈라진다.
  2. 스키마 변경은 전부 MIGRATIONS를 통한다.
  3. verify_schema()는 마이그레이션 누락을 부팅 시점에 잡아낸다.
     (못 잡으면 봇은 멀쩡히 로그인하고 게임 도중에 no such column으로 터진다.)
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot


# BASE_SCHEMA를 수정하면 이 테스트가 깨진다. 그게 의도다.
# 스키마를 바꾸고 싶다면 BASE_SCHEMA가 아니라 MIGRATIONS에 항목을 추가하라.
BASE_SCHEMA_SHA256 = "367c1164ae311917f87543b8f8c9afc1bde82ed537676a0dece20f6789d40c8a"


class BaselineImmutabilityTest(unittest.TestCase):
    def test_base_schema_is_frozen(self) -> None:
        actual = hashlib.sha256(bot.BASE_SCHEMA.encode()).hexdigest()
        self.assertEqual(
            actual,
            BASE_SCHEMA_SHA256,
            "BASE_SCHEMA가 수정되었습니다. 스키마 변경은 MIGRATIONS에 추가하세요. "
            "(정말 베이스라인을 다시 잡는 거라면 이 체크섬도 함께 갱신할 것)",
        )

    def test_migration_versions_are_contiguous(self) -> None:
        expected = list(range(bot.BASELINE_VERSION + 1, bot.SCHEMA_VERSION + 1))
        self.assertEqual(
            sorted(bot.MIGRATIONS), expected, "MIGRATIONS 버전 번호에 구멍이 있습니다."
        )


class FreshDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        patcher = patch.object(
            bot, "DB_PATH", Path(self.temp_dir.name) / "schema.sqlite3"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fresh_db_matches_canonical_schema(self) -> None:
        """새로 만든 DB가 '기존 DB + 마이그레이션' 결과와 동일해야 한다."""
        bot.init_db()
        with bot.db() as conn:
            actual = bot._schema_snapshot(conn)
        self.assertEqual(actual, bot.canonical_schema())

    def test_fresh_db_is_stamped_with_current_version(self) -> None:
        bot.init_db()
        with bot.db() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, bot.SCHEMA_VERSION)

    def test_verify_schema_passes(self) -> None:
        bot.init_db()
        bot.verify_schema()  # 예외가 나면 실패

    def test_init_db_is_idempotent(self) -> None:
        bot.init_db()
        bot.init_db()
        bot.verify_schema()

    def test_legacy_db_without_user_version_is_adopted_as_baseline(self) -> None:
        """마이그레이션 도입 이전부터 굴러온 운영 DB(user_version=0)를 v1로 인식해야 한다."""
        conn = sqlite3.connect(bot.DB_PATH)
        conn.executescript(bot.BASE_SCHEMA)
        conn.commit()
        conn.close()

        bot.init_db()
        with bot.db() as conn:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], bot.SCHEMA_VERSION
            )


class SchemaGuardTest(unittest.TestCase):
    """마이그레이션을 빠뜨린 배포를 부팅 시점에 막는지 확인한다."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        patcher = patch.object(
            bot, "DB_PATH", Path(self.temp_dir.name) / "guard.sqlite3"
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        bot.init_db()

    def test_missing_column_blocks_startup(self) -> None:
        pending = {bot.SCHEMA_VERSION + 1: ["ALTER TABLE rounds ADD COLUMN memo TEXT"]}
        with patch.object(bot, "MIGRATIONS", pending), patch.object(
            bot, "SCHEMA_VERSION", bot.SCHEMA_VERSION + 1
        ):
            # DB에는 아직 memo 컬럼이 없다 -> 부팅을 막아야 한다.
            with self.assertRaises(RuntimeError) as ctx:
                bot.verify_schema()
            self.assertIn("memo", str(ctx.exception))

            # init_db가 마이그레이션을 적용하면 통과해야 한다.
            bot.init_db()
            bot.verify_schema()

    def test_missing_table_blocks_startup(self) -> None:
        with bot.db() as conn:
            conn.execute("DROP TABLE score_logs")
        with self.assertRaises(RuntimeError) as ctx:
            bot.verify_schema()
        self.assertIn("score_logs", str(ctx.exception))

    def test_unknown_extra_column_is_tolerated(self) -> None:
        """구버전으로 롤백하면 DB에 모르는 컬럼이 남는다. 그건 막지 않아야 한다."""
        with bot.db() as conn:
            conn.execute("ALTER TABLE rounds ADD COLUMN from_the_future TEXT")
        bot.verify_schema()  # 예외가 나면 실패


if __name__ == "__main__":
    unittest.main()
