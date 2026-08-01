"""라운드 행을 읽는 공통 함수들. 게임 모듈이 공유하는 최소한의 어휘다.

여기 있는 것은 전부 순수 조회다 — 상태를 바꾸지 않고, 디스코드를 부르지 않는다.
게임별 상태 전이는 각 게임 모듈이 갖는다.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def get_open_round(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM rounds WHERE status = 'open' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def round_extra(row: sqlite3.Row) -> dict[str, Any]:
    try:
        data = json.loads(row["extra_json"])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def round_game_key(round_row: sqlite3.Row) -> str:
    """코드상 게임 이름. 3선 폭탄 게임은 레거시 키 defuse4, DB type bomb을 유지한다.

    `rounds.type`만으로는 게임을 가릴 수 없다 — 불변인 CHECK 제약 때문에 여러 게임이
    같은 타입을 나눠 쓴다(grid: 유일 숫자·야구, sand: 모래성·가라앉는 배).
    돌아온 값은 yumcbot.games.registry의 GAMES 키다.
    """
    return str(round_extra(round_row).get("mode") or round_row["type"])


def main_game_phase(round_row: sqlite3.Row | dict[str, Any]) -> str | None:
    """메인게임의 진행 단계. 게임마다 extra_json 키가 달라서 한 곳에 모아둔다.

    세 게임 모두 마지막 단계 이름은 'scored', 취소는 'cancelled'로 맞춘다.
    """
    extra = round_extra(round_row)  # type: ignore[arg-type]
    game_key = round_game_key(round_row)  # type: ignore[arg-type]
    if game_key == "trust":
        return extra.get("trust_phase")
    if game_key == "bail":
        return extra.get("bail_phase")
    return extra.get("sand_phase")


def is_event_participant(
    conn: sqlite3.Connection,
    event_session_id: int,
    user_id: str,
) -> bool:
    return conn.execute(
        """
        SELECT 1 FROM event_participants
        WHERE event_session_id = ? AND user_id = ?
        """,
        (event_session_id, user_id),
    ).fetchone() is not None


def can_join_round(conn: sqlite3.Connection, round_id: int, user_id: str) -> bool:
    round_row = conn.execute(
        "SELECT event_session_id FROM rounds WHERE id = ?",
        (round_id,),
    ).fetchone()
    if not round_row:
        return False
    event_session_id = round_row["event_session_id"]
    # 기존 DB에서 기능 추가 전에 생성된 라운드는 종전 동작을 유지합니다.
    if event_session_id is None:
        return True
    return is_event_participant(conn, int(event_session_id), user_id)
