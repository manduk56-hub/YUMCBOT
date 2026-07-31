"""스키마 마이그레이션 안전장치 테스트.

배포 파이프라인 전체가 다음 불변식에 기대고 있다.

  1. BASE_SCHEMA(v1)는 절대 수정하지 않는다. 고치면 새로 만든 DB와 이미 굴러가는
     운영 DB의 스키마가 조용히 갈라진다.
  2. 스키마 변경은 전부 MIGRATIONS를 통한다.
  3. verify_schema()는 마이그레이션 누락을 부팅 시점에 잡아낸다.
     (못 잡으면 봇은 멀쩡히 로그인하고 게임 도중에 no such column으로 터진다.)

대상은 yumcbot.db다. bot.py도 같은 이름들을 재노출하지만 그건 사본이라
patch.object로 갈아끼워도 실제 동작이 바뀌지 않는다.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yumcbot import config
from yumcbot import db as schema


# BASE_SCHEMA를 수정하면 이 테스트가 깨진다. 그게 의도다.
# 스키마를 바꾸고 싶다면 BASE_SCHEMA가 아니라 MIGRATIONS에 항목을 추가하라.
BASE_SCHEMA_SHA256 = "367c1164ae311917f87543b8f8c9afc1bde82ed537676a0dece20f6789d40c8a"


class BaselineImmutabilityTest(unittest.TestCase):
    def test_base_schema_is_frozen(self) -> None:
        actual = hashlib.sha256(schema.BASE_SCHEMA.encode()).hexdigest()
        self.assertEqual(
            actual,
            BASE_SCHEMA_SHA256,
            "BASE_SCHEMA가 수정되었습니다. 스키마 변경은 MIGRATIONS에 추가하세요. "
            "(정말 베이스라인을 다시 잡는 거라면 이 체크섬도 함께 갱신할 것)",
        )

    def test_migration_versions_are_contiguous(self) -> None:
        expected = list(range(schema.BASELINE_VERSION + 1, schema.SCHEMA_VERSION + 1))
        self.assertEqual(
            sorted(schema.MIGRATIONS), expected, "MIGRATIONS 버전 번호에 구멍이 있습니다."
        )


class FreshDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        patcher = patch.object(
            config, "DB_PATH", Path(self.temp_dir.name) / "schema.sqlite3"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fresh_db_matches_canonical_schema(self) -> None:
        """새로 만든 DB가 '기존 DB + 마이그레이션' 결과와 동일해야 한다."""
        schema.init_db()
        with schema.db() as conn:
            actual = schema._schema_snapshot(conn)
        self.assertEqual(actual, schema.canonical_schema())

    def test_fresh_db_is_stamped_with_current_version(self) -> None:
        schema.init_db()
        with schema.db() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, schema.SCHEMA_VERSION)

    def test_verify_schema_passes(self) -> None:
        schema.init_db()
        schema.verify_schema()  # 예외가 나면 실패

    def test_discussion_messages_table_is_created(self) -> None:
        """v2 마이그레이션. 익명 발언을 유저당 여러 건 저장하기 위한 테이블."""
        schema.init_db()
        with schema.db() as conn:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(discussion_messages)").fetchall()
            }
        self.assertIn("anon_number", columns)
        self.assertIn("content", columns)

    def test_russian_roulette_table_is_created(self) -> None:
        """v4 마이그레이션. 재시작 뒤에도 1:1 게임 상태와 예치금을 복구할 수 있어야 한다."""
        schema.init_db()
        with schema.db() as conn:
            columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(russian_roulette_games)"
                ).fetchall()
            }
        self.assertIn("challenger_bet", columns)
        self.assertIn("live_chamber", columns)
        self.assertIn("winner_id", columns)

    def test_baseball_swings_table_is_created(self) -> None:
        """v7 마이그레이션. 3구의 클릭 순서와 반응 시간을 각각 보존한다."""
        schema.init_db()
        with schema.db() as conn:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(baseball_swings)").fetchall()
            }
        self.assertEqual(
            columns,
            {
                "id",
                "round_id",
                "pitch_number",
                "user_id",
                "display_name",
                "swung_at",
                "elapsed_seconds",
                "rank",
                "score",
                "created_at",
            },
        )

    def test_season_finalization_tables_are_created_open(self) -> None:
        """v8 마이그레이션. 종료 상태와 추첨까지 끝난 고유 순위를 재시작 뒤에도 보존한다."""
        schema.init_db()
        with schema.db() as conn:
            state = conn.execute(
                "SELECT singleton_id, status FROM season_state"
            ).fetchone()
            ranking_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(final_rankings)").fetchall()
            }
        self.assertEqual((state["singleton_id"], state["status"]), (1, "open"))
        self.assertEqual(
            ranking_columns,
            {
                "user_id",
                "final_rank",
                "display_name",
                "total_score",
                "participation_count",
                "win_count",
                "draw_order",
                "finalized_at",
            },
        )

    def test_init_db_is_idempotent(self) -> None:
        schema.init_db()
        schema.init_db()
        schema.verify_schema()

    def test_legacy_db_without_user_version_is_adopted_as_baseline(self) -> None:
        """마이그레이션 도입 이전부터 굴러온 운영 DB(user_version=0)를 v1로 인식해야 한다."""
        conn = sqlite3.connect(config.DB_PATH)
        conn.executescript(schema.BASE_SCHEMA)
        conn.commit()
        conn.close()

        schema.init_db()
        with schema.db() as conn:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], schema.SCHEMA_VERSION
            )


class SchemaGuardTest(unittest.TestCase):
    """마이그레이션을 빠뜨린 배포를 부팅 시점에 막는지 확인한다."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        patcher = patch.object(
            config, "DB_PATH", Path(self.temp_dir.name) / "guard.sqlite3"
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        schema.init_db()

    def test_missing_column_blocks_startup(self) -> None:
        # 기존 마이그레이션은 그대로 두고 아직 적용되지 않은 버전을 하나 얹는다.
        # (canonical_schema가 v2부터 순서대로 실행하므로 구멍이 있으면 안 된다.)
        pending = {
            **schema.MIGRATIONS,
            schema.SCHEMA_VERSION + 1: ["ALTER TABLE rounds ADD COLUMN memo TEXT"],
        }
        with patch.object(schema, "MIGRATIONS", pending), patch.object(
            schema, "SCHEMA_VERSION", schema.SCHEMA_VERSION + 1
        ):
            # DB에는 아직 memo 컬럼이 없다 -> 부팅을 막아야 한다.
            with self.assertRaises(RuntimeError) as ctx:
                schema.verify_schema()
            self.assertIn("memo", str(ctx.exception))

            # init_db가 마이그레이션을 적용하면 통과해야 한다.
            schema.init_db()
            schema.verify_schema()

    def test_missing_table_blocks_startup(self) -> None:
        with schema.db() as conn:
            conn.execute("DROP TABLE score_logs")
        with self.assertRaises(RuntimeError) as ctx:
            schema.verify_schema()
        self.assertIn("score_logs", str(ctx.exception))

    def test_unknown_extra_column_is_tolerated(self) -> None:
        """구버전으로 롤백하면 DB에 모르는 컬럼이 남는다. 그건 막지 않아야 한다."""
        with schema.db() as conn:
            conn.execute("ALTER TABLE rounds ADD COLUMN from_the_future TEXT")
        schema.verify_schema()  # 예외가 나면 실패


if __name__ == "__main__":
    unittest.main()
