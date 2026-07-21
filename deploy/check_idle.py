#!/usr/bin/env python3
"""배포 게이트: 이벤트나 1:1 게임이 진행 중이면 배포를 막는다.

봇 재시작은 3~5초짜리 다운타임이고 상태는 DB에서 복구되지만, 그 사이 버튼을 누른
참가자는 '상호작용 실패'를 본다. 게임이 도는 중에는 배포하지 않는 게 맞다.

  exit 0 : 유휴 상태. 배포해도 됨
  exit 1 : 진행 중인 라운드/이벤트/1:1 게임 있음. 배포 보류
  exit 2 : DB를 읽을 수 없음

사용법:
    python3 check_idle.py [--db /var/lib/yumcbot/data/event_game.sqlite3]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path


def default_db_path() -> Path:
    data_dir = os.getenv("YUMCBOT_DATA_DIR") or "/var/lib/yumcbot/data"
    db_name = os.getenv("YUMCBOT_DB_NAME") or "event_game.sqlite3"
    return Path(data_dir) / db_name


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args()

    db_path = args.db or default_db_path()

    if not db_path.exists():
        # 첫 배포. DB가 아직 없으니 당연히 유휴 상태다.
        print(f"DB 없음({db_path}). 첫 배포로 간주하고 진행합니다.")
        return 0

    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        print(f"DB 열기 실패: {exc}", file=sys.stderr)
        return 2

    try:
        open_rounds = conn.execute(
            "SELECT id, type, title FROM rounds WHERE status = 'open' ORDER BY id"
        ).fetchall()
        live_sessions = conn.execute(
            "SELECT id, status FROM event_sessions "
            "WHERE status IN ('recruiting', 'active') ORDER BY id"
        ).fetchall()
        roulette_table = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'russian_roulette_games'"
        ).fetchone()
        live_roulette = (
            conn.execute(
                """
                SELECT id, status FROM russian_roulette_games
                WHERE status = 'playing'
                   OR (
                        status IN ('pending', 'betting')
                        AND expires_at > CAST(strftime('%s', 'now') AS INTEGER)
                   )
                ORDER BY id
                """
            ).fetchall()
            if roulette_table
            else []
        )
    except sqlite3.Error as exc:
        print(f"DB 조회 실패: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    if not open_rounds and not live_sessions and not live_roulette:
        print("유휴 상태입니다. 배포를 진행합니다.")
        return 0

    print("진행 중인 이벤트가 있어 배포를 보류합니다.", file=sys.stderr)
    for row in live_sessions:
        print(f"  - 이벤트 세션 #{row['id']} ({row['status']})", file=sys.stderr)
    for row in open_rounds:
        print(f"  - 라운드 #{row['id']} [{row['type']}] {row['title']}", file=sys.stderr)
    for row in live_roulette:
        print(f"  - 러시안룰렛 #{row['id']} ({row['status']})", file=sys.stderr)
    print(
        "\n진행 중인 게임이 끝난 뒤 다시 배포하거나, 이벤트는 운영자가 /이벤트종료 로 정리하세요.\n"
        "정말 지금 배포해야 한다면 Actions에서 force=true 로 재실행하세요.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
