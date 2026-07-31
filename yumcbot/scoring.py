"""유저 재화: 점수(`users.total_score` + `score_logs`)와 잠수 코인, 룰렛 연승 스택.

룰렛 스택 함수가 러시안룰렛 모듈이 아니라 여기 있는 이유는 순환을 끊기 위해서다.
`add_score()`는 점수가 바뀌는 모든 길목이라 스택 무효화(`reset_roulette_stack_if_escaped`)를
매번 확인해야 하는데, 스택 함수를 roulette 쪽에 두면 scoring → roulette → scoring이 된다.
셋 다 users 테이블만 만지므로 여기 두면 의존이 한 방향(roulette → scoring)으로 정리된다.
"""

from __future__ import annotations

import sqlite3

import discord

from .activity import log_activity
from .config import ROULETTE_MIN_BET
from .db import now_text


def ensure_user(conn: sqlite3.Connection, user: discord.abc.User) -> None:
    display_name = getattr(user, "display_name", None) or user.name
    ts = now_text()
    conn.execute(
        """
        INSERT INTO users (user_id, display_name, total_score, created_at, updated_at)
        VALUES (?, ?, 0, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            display_name = excluded.display_name,
            updated_at = excluded.updated_at
        """,
        (str(user.id), display_name, ts, ts),
    )


class SeasonClosedError(RuntimeError):
    """최종 순위 확정 뒤 점수를 바꾸려는 내부 경로를 차단한다."""


def _season_is_closed(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT status FROM season_state WHERE singleton_id = 1"
    ).fetchone()
    return bool(row and row["status"] == "closed")


def add_score(
    conn: sqlite3.Connection,
    user_id: str,
    display_name: str | None,
    delta: int,
    reason: str,
    round_id: int | None = None,
) -> None:
    if _season_is_closed(conn):
        raise SeasonClosedError("시즌이 종료되어 점수를 변경할 수 없습니다.")
    ts = now_text()
    # display_name이 None(멤버 캐시 미스)이면 신규 행은 플레이스홀더로,
    # 기존 행은 이름을 보존한다. excluded는 VALUES에서 파생돼 이 분기를 못 하므로
    # 원본 :name 파라미터를 UPDATE에서 직접 참조한다 (issue #19).
    conn.execute(
        """
        INSERT INTO users (user_id, display_name, total_score, created_at, updated_at)
        VALUES (:uid, COALESCE(:name, 'User ' || :uid), 0, :ts, :ts)
        ON CONFLICT(user_id) DO UPDATE SET
            display_name = COALESCE(:name, users.display_name),
            updated_at = :ts
        """,
        {"uid": user_id, "name": display_name, "ts": ts},
    )
    conn.execute(
        "UPDATE users SET total_score = total_score + ?, updated_at = ? WHERE user_id = ?",
        (delta, ts, user_id),
    )
    conn.execute(
        """
        INSERT INTO score_logs (round_id, user_id, delta, reason, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (round_id, user_id, delta, reason, ts),
    )
    reset_roulette_stack_if_escaped(conn, user_id)


def get_roulette_stack(conn: sqlite3.Connection, user_id: str) -> int:
    row = conn.execute(
        "SELECT roulette_win_stack FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    return int(row["roulette_win_stack"]) if row else 0


def set_roulette_stack(
    conn: sqlite3.Connection,
    user_id: str,
    value: int,
    reason: str,
    before: int | None = None,
) -> None:
    """스택은 카운터라 덮어쓰면 이전 값이 사라진다. 덮기 직전에 활동 로그를 남긴다.

    reason: win | payout | loss | escaped
    """
    current = get_roulette_stack(conn, user_id) if before is None else before
    if current == value:
        return
    log_activity(
        conn,
        "roulette",
        "stack",
        actor_user_id=user_id,
        payload={"before": current, "after": value, "reason": reason},
    )
    conn.execute(
        "UPDATE users SET roulette_win_stack = ? WHERE user_id = ?", (value, user_id)
    )


def reset_roulette_stack_if_escaped(conn: sqlite3.Connection, user_id: str) -> None:
    """신용 상태를 벗어나면 러시안룰렛 연승 스택을 버린다.

    스택은 '신용 상태가 끊기지 않고 이어졌다'는 뜻이라, 중간에 한 번이라도 점수가
    올라오면 무효다. 그런데 탈출은 룰렛이 아니라 메인게임 정산으로도 일어나므로
    현재 점수만 보고는 '중간에 올라왔다 내려왔는지'를 알 수 없다. 그래서 점수가
    바뀌는 길목인 add_score에서 매번 확인한다.
    """
    row = conn.execute(
        "SELECT total_score, roulette_win_stack FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        return
    stack = int(row["roulette_win_stack"])
    if stack <= 0 or int(row["total_score"]) < ROULETTE_MIN_BET:
        return
    set_roulette_stack(conn, user_id, 0, "escaped", before=stack)


def get_afk_coins(conn: sqlite3.Connection, user_id: str) -> int:
    row = conn.execute("SELECT coins FROM afk_coins WHERE user_id = ?", (user_id,)).fetchone()
    return int(row["coins"]) if row else 0


def grant_afk_coins(conn: sqlite3.Connection, user_id: str, amount: int) -> None:
    # 매분 도는 백그라운드 루프는 종료 뒤에도 살아 있으므로 조용히 적립만 건너뛴다.
    if _season_is_closed(conn):
        return
    conn.execute(
        """
        INSERT INTO afk_coins (user_id, coins, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            coins = coins + excluded.coins,
            updated_at = excluded.updated_at
        """,
        (user_id, amount, now_text()),
    )


def spend_afk_coins(conn: sqlite3.Connection, user_id: str, cost: int) -> bool:
    """잔액이 충분할 때만 차감한다. 동시 호출에도 잔액이 음수로 내려가지 않도록 조건부 UPDATE로 처리한다."""
    cursor = conn.execute(
        "UPDATE afk_coins SET coins = coins - ?, updated_at = ? WHERE user_id = ? AND coins >= ?",
        (cost, now_text(), user_id, cost),
    )
    return cursor.rowcount == 1
