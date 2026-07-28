"""야구 — `/준비`로 한 구씩 진행하는 반응속도 미니게임.

3구를 던지고 **3구 평균**으로 최종 순위를 매겨 한 번만 점수를 준다. 구별 순위는
표시용이라 점수가 붙지 않는다 — 매 구 지급하면 먼저 친 사람이 계속 앞서서
뒤 구의 의미가 사라진다.

재시작 안전성이 이 게임의 제약 대부분을 만든다:

- 활성화 시각·조작판 메시지 ID·무작위 버튼 위치를 전부 `extra_json`에 남긴다.
  부팅 시 `EventBot.restore_baseball_pitch_tasks()`가 그걸로 타이머를 되살린다.
- 버튼 클릭은 `baseball_swings`에 남는다. 미입력은 `BASEBALL_MISSED_SWING_SECONDS`
  (7초)로 계산해 최종 평균에 넣는다 — 안 치는 것이 이득이 되면 안 된다.
- 조작판은 매 구 새로 그리므로 **누른 조작판이 현재 것인지** 확인한다
  (`baseball_control_message_id`). 옛 조작판의 클릭을 받으면 이미 끝난 구에
  기록이 들어간다.

타이머는 `EventBot`이 들고 있고 이 모듈은 프로토콜로 스케줄만 요청한다.
인터랙션 없이(투구 타이머 콜백에서) 채널을 열어야 해서 클라이언트는
`runtime.get_client()`로 얻는다.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from typing import Any, Protocol, runtime_checkable

import discord

from .. import runtime
from ..config import (
    BASEBALL_BUTTON_GRID_SIZE,
    BASEBALL_BUTTON_SLOT_COUNT,
    BASEBALL_IMAGE_FILENAME,
    BASEBALL_MISSED_SWING_SECONDS,
    BASEBALL_RANK_POINTS,
    BASEBALL_RESULT_SECONDS,
    BASEBALL_SWING_SECONDS,
    BASEBALL_TOTAL_PITCHES,
    BASEBALL_WINDUP_SECONDS,
)
from ..db import (
    db,
    now_text,
)
from ..activity import (
    log_activity,
    user_display_name,
)
from ..scoring import (
    add_score,
    ensure_user,
)
from ..rounds import (
    can_join_round,
    get_open_round,
    round_extra,
    round_game_key,
)
from .registry import SettleContext, register_settler


@runtime_checkable
class BaseballTimerClient(Protocol):
    """투구 타이머를 걸 수 있는 클라이언트. 실체는 EventBot이다.

    구체 타입 대신 프로토콜로 확인하는 이유는 순환 import를 피하기 위해서다.
    타이머 태스크는 EventBot이 들고 있고(재시작 시 복구도 거기서 한다), 이 모듈은
    '언제 깨워달라'고 요청만 한다.
    """

    def schedule_baseball_pitch(
        self, round_id: int, result_at: float, pitch_number: int
    ) -> None: ...

    def schedule_baseball_activation(
        self, round_id: int, activate_at: float, pitch_number: int
    ) -> None: ...


def _timer_client() -> BaseballTimerClient:
    client = runtime.get_client()
    if not isinstance(client, BaseballTimerClient):
        raise RuntimeError("투구 타이머를 걸 수 없는 클라이언트입니다.")
    return client


async def delete_baseball_control_message(round_row: sqlite3.Row | dict[str, Any]) -> None:
    if round_game_key(round_row) != "baseball":
        return
    control_message_id = str(round_extra(round_row).get("baseball_control_message_id") or "")
    channel_id = str(round_row["channel_id"] or "")
    if not control_message_id or not channel_id:
        return
    channel = await runtime.resolve_text_channel(channel_id)
    if channel is None:
        return
    try:
        message = await channel.fetch_message(int(control_message_id))
        await message.delete()
    except (discord.HTTPException, ValueError):
        pass

def prepare_baseball_pitch(
    round_id: int,
    actor_user_id: str,
    *,
    activate_at: float | None = None,
) -> dict[str, Any]:
    activate_at = (
        time.time() + BASEBALL_WINDUP_SECONDS
        if activate_at is None
        else float(activate_at)
    )
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row or round_game_key(round_row) != "baseball":
            raise ValueError("진행 중인 야구 라운드를 찾을 수 없습니다.")

        extra = round_extra(round_row)
        phase = str(extra.get("baseball_phase") or "")
        if phase == "winding_up":
            raise ValueError("이미 투수가 와인드업 중입니다. 잠시 기다려주세요.")
        if phase != "waiting_prepare":
            raise ValueError("지금은 투구를 준비할 수 없습니다.")

        pitch_number = int(extra.get("baseball_pitch") or 1)
        log_activity(
            conn,
            "round",
            "baseball_pitch_prepared",
            event_session_id=round_row["event_session_id"],
            round_id=round_id,
            actor_user_id=actor_user_id,
            payload={
                "pitch_number": pitch_number,
                "previous_phase": phase,
                "activate_at": activate_at,
            },
        )
        extra.update(
            {
                "baseball_phase": "winding_up",
                "baseball_prepared_by": actor_user_id,
                "baseball_prepared_at": time.time(),
                "baseball_activate_at": activate_at,
            }
        )
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )
        return {"round": dict(round_row), "extra": extra, "pitch_number": pitch_number}

def set_baseball_activation_at(
    round_id: int,
    pitch_number: int,
    activate_at: float,
    control_message_id: str,
) -> None:
    """비활성 스트라이크존이 보인 시각을 기준으로 자동 활성화 시각을 확정한다."""
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row or round_game_key(round_row) != "baseball":
            raise ValueError("진행 중인 야구 라운드를 찾을 수 없습니다.")
        extra = round_extra(round_row)
        if (
            extra.get("baseball_phase") != "winding_up"
            or int(extra.get("baseball_pitch") or 1) != pitch_number
        ):
            raise ValueError("활성화를 기다리는 투구가 아닙니다.")
        log_activity(
            conn,
            "round",
            "baseball_windup_visible",
            event_session_id=round_row["event_session_id"],
            round_id=round_id,
            payload={
                "pitch_number": pitch_number,
                "previous_activate_at": extra.get("baseball_activate_at"),
                "activate_at": float(activate_at),
                "control_message_id": control_message_id,
            },
        )
        extra["baseball_activate_at"] = float(activate_at)
        extra["baseball_control_message_id"] = control_message_id
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )

def reset_baseball_prepare(round_id: int, pitch_number: int) -> None:
    """와인드업 판 게시나 활성화에 실패하면 관리자가 /준비를 다시 실행할 수 있게 한다."""
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row or round_game_key(round_row) != "baseball":
            return
        extra = round_extra(round_row)
        if (
            extra.get("baseball_phase") != "winding_up"
            or int(extra.get("baseball_pitch") or 1) != pitch_number
        ):
            return
        log_activity(
            conn,
            "round",
            "baseball_prepare_reset",
            event_session_id=round_row["event_session_id"],
            round_id=round_id,
            payload={
                "pitch_number": pitch_number,
                "activate_at": extra.get("baseball_activate_at"),
                "reason": "strike_zone_publish_or_activation_failed",
            },
        )
        extra["baseball_phase"] = "waiting_prepare"
        extra.pop("baseball_activate_at", None)
        extra.pop("baseball_prepared_by", None)
        extra.pop("baseball_prepared_at", None)
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )

def begin_baseball_pitch(
    round_id: int,
    actor_user_id: str,
    *,
    started_at: float | None = None,
) -> dict[str, Any]:
    started_at = time.time() if started_at is None else float(started_at)
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row or round_game_key(round_row) != "baseball":
            raise ValueError("진행 중인 야구 라운드를 찾을 수 없습니다.")

        extra = round_extra(round_row)
        if extra.get("baseball_phase") != "winding_up":
            raise ValueError("먼저 관리자가 `/준비` 명령어를 실행해주세요.")
        pitch_number = int(extra.get("baseball_pitch") or 1)
        result_at = started_at + BASEBALL_RESULT_SECONDS
        log_activity(
            conn,
            "round",
            "baseball_pitch_started",
            event_session_id=round_row["event_session_id"],
            round_id=round_id,
            actor_user_id=actor_user_id,
            payload={
                "pitch_number": pitch_number,
                "started_at": started_at,
                "result_at": result_at,
                "scheduled_activate_at": extra.get("baseball_activate_at"),
            },
        )
        extra.update(
            {
                "baseball_phase": "swinging",
                "baseball_started_at": started_at,
                "baseball_result_at": result_at,
                "baseball_started_by": actor_user_id,
            }
        )
        extra.pop("baseball_activate_at", None)
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )
        return {
            "round": dict(round_row),
            "extra": extra,
            "pitch_number": pitch_number,
            "started_at": started_at,
            "result_at": result_at,
        }

def set_baseball_pitch_visible_at(
    round_id: int,
    pitch_number: int,
    visible_at: float,
    control_message_id: str | None = None,
    button_position: int | None = None,
) -> dict[str, float]:
    """Discord가 버튼 편집을 완료한 시각으로 반응 시간의 0초를 다시 맞춘다."""
    visible_at = float(visible_at)
    result_at = visible_at + BASEBALL_RESULT_SECONDS
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row or round_game_key(round_row) != "baseball":
            raise ValueError("진행 중인 야구 라운드를 찾을 수 없습니다.")
        extra = round_extra(round_row)
        if (
            extra.get("baseball_phase") != "swinging"
            or int(extra.get("baseball_pitch") or 1) != pitch_number
        ):
            raise ValueError("활성화 중인 투구가 아닙니다.")
        log_activity(
            conn,
            "round",
            "baseball_pitch_visible",
            event_session_id=round_row["event_session_id"],
            round_id=round_id,
            payload={
                "pitch_number": pitch_number,
                "visible_at": visible_at,
                "result_at": result_at,
                "control_message_id": control_message_id,
                "button_position": button_position,
                "previous_started_at": extra.get("baseball_started_at"),
            },
        )
        extra.update(
            {
                "baseball_started_at": visible_at,
                "baseball_result_at": result_at,
            }
        )
        if control_message_id is not None:
            extra["baseball_control_message_id"] = control_message_id
        if button_position is not None:
            extra["baseball_button_position"] = button_position
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )
    return {"started_at": visible_at, "result_at": result_at}

def record_baseball_swing(
    round_id: int,
    pitch_number: int,
    user: discord.abc.User,
    *,
    swung_at: float | None = None,
    button_visible_at: float | None = None,
    control_message_id: str | None = None,
) -> dict[str, Any]:
    swung_at = time.time() if swung_at is None else float(swung_at)
    user_id = str(user.id)
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row or round_game_key(round_row) != "baseball":
            raise ValueError("이미 끝났거나 존재하지 않는 야구 라운드입니다.")
        if not can_join_round(conn, round_id, user_id):
            raise PermissionError("이번 이벤트에서 `참여하기`를 누른 유저만 타격할 수 있습니다.")

        extra = round_extra(round_row)
        expected_control_id = str(extra.get("baseball_control_message_id") or "")
        if control_message_id is not None and expected_control_id != control_message_id:
            raise ValueError("이미 교체된 야구 조작판입니다. 가장 아래의 최신 버튼을 사용해주세요.")
        current_pitch = int(extra.get("baseball_pitch") or 1)
        if extra.get("baseball_phase") != "swinging" or current_pitch != pitch_number:
            raise ValueError("지금은 이 투구의 치기 버튼을 사용할 수 없습니다.")

        existing = conn.execute(
            """
            SELECT elapsed_seconds, rank, score FROM baseball_swings
            WHERE round_id = ? AND pitch_number = ? AND user_id = ?
            """,
            (round_id, pitch_number, user_id),
        ).fetchone()
        if existing:
            return {
                "already": True,
                "elapsed_seconds": float(existing["elapsed_seconds"]),
                "rank": existing["rank"],
                "score": int(existing["score"]),
            }

        try:
            started_at = float(
                button_visible_at
                if button_visible_at is not None
                else extra["baseball_started_at"]
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("투구 시작 시각을 확인할 수 없습니다.") from error
        elapsed = max(0.0, swung_at - started_at)
        rank: int | None = None
        if elapsed < BASEBALL_SWING_SECONDS:
            rank = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM baseball_swings
                    WHERE round_id = ? AND pitch_number = ? AND elapsed_seconds < ?
                    """,
                    (round_id, pitch_number, BASEBALL_SWING_SECONDS),
                ).fetchone()["count"]
            ) + 1

        ensure_user(conn, user)
        display_name = user_display_name(user) or f"User {user_id}"
        conn.execute(
            """
            INSERT INTO baseball_swings
                (round_id, pitch_number, user_id, display_name, swung_at,
                 elapsed_seconds, rank, score, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                round_id,
                pitch_number,
                user_id,
                display_name,
                swung_at,
                elapsed,
                rank,
                now_text(),
            ),
        )
        log_activity(
            conn,
            "round",
            "baseball_swing",
            event_session_id=round_row["event_session_id"],
            round_id=round_id,
            actor_user_id=user_id,
            payload={
                "pitch_number": pitch_number,
                "elapsed_seconds": elapsed,
                "rank_at_click": rank,
                "valid": rank is not None,
            },
        )
        return {
            "already": False,
            "elapsed_seconds": elapsed,
            "rank": rank,
            "score": 0,
        }

def _baseball_pitch_result(
    conn: sqlite3.Connection,
    round_row: sqlite3.Row,
    pitch_number: int,
) -> dict[str, Any]:
    extra = round_extra(round_row)
    participants = [
        dict(row)
        for row in conn.execute(
            """
            SELECT event_participants.user_id, users.display_name
            FROM event_participants
            JOIN users ON users.user_id = event_participants.user_id
            WHERE event_participants.event_session_id = ?
            ORDER BY event_participants.joined_at ASC
            """,
            (round_row["event_session_id"],),
        ).fetchall()
    ]
    swings = [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM baseball_swings
            WHERE round_id = ? AND pitch_number = ?
            ORDER BY CASE WHEN rank IS NULL THEN 1 ELSE 0 END, rank ASC, id ASC
            """,
            (round_row["id"], pitch_number),
        ).fetchall()
    ]
    return {
        "round_id": int(round_row["id"]),
        "event_session_id": round_row["event_session_id"],
        "channel_id": str(round_row["channel_id"]),
        "message_id": str(round_row["message_id"] or ""),
        "control_message_id": str(extra.get("baseball_control_message_id") or ""),
        "button_position": max(
            0,
            min(
                BASEBALL_BUTTON_SLOT_COUNT - 1,
                int(extra.get("baseball_button_position") or 0),
            ),
        ),
        "title": str(round_row["title"]),
        "pitch_number": pitch_number,
        "participants": participants,
        "swings": swings,
    }

def baseball_final_standings(
    conn: sqlite3.Connection,
    round_row: sqlite3.Row,
    completed_pitches: set[int],
) -> list[dict[str, Any]]:
    """미입력 투구를 7초로 채우고 세 투구의 평균 반응 시간으로 정렬한다."""
    rows = conn.execute(
        """
        SELECT event_participants.user_id, users.display_name,
               COUNT(baseball_swings.id) AS swing_count,
               COALESCE(SUM(
                   CASE WHEN baseball_swings.elapsed_seconds < ? THEN 1 ELSE 0 END
               ), 0) AS valid_swing_count,
               COALESCE(SUM(baseball_swings.elapsed_seconds), 0) AS recorded_seconds
        FROM event_participants
        JOIN users ON users.user_id = event_participants.user_id
        LEFT JOIN baseball_swings
          ON baseball_swings.round_id = ?
         AND baseball_swings.user_id = event_participants.user_id
        WHERE event_participants.event_session_id = ?
        GROUP BY event_participants.user_id, users.display_name
        """,
        (
            BASEBALL_SWING_SECONDS,
            round_row["id"],
            round_row["event_session_id"],
        ),
    ).fetchall()
    all_pitches_complete = completed_pitches.issuperset(
        range(1, BASEBALL_TOTAL_PITCHES + 1)
    )
    standings = []
    for row in rows:
        valid_swing_count = int(row["valid_swing_count"] or 0)
        swing_count = min(int(row["swing_count"] or 0), BASEBALL_TOTAL_PITCHES)
        missed_swing_count = BASEBALL_TOTAL_PITCHES - swing_count
        average_seconds = (
            float(row["recorded_seconds"])
            + (missed_swing_count * BASEBALL_MISSED_SWING_SECONDS)
        ) / BASEBALL_TOTAL_PITCHES
        standings.append(
            {
                "user_id": str(row["user_id"]),
                "display_name": str(row["display_name"]),
                "swing_count": swing_count,
                "missed_swing_count": missed_swing_count,
                "valid_swing_count": valid_swing_count,
                "average_seconds": average_seconds,
                "eligible": all_pitches_complete,
                "rank": None,
                "score": 0,
            }
        )

    standings.sort(
        key=lambda standing: (
            not standing["eligible"],
            (
                float(standing["average_seconds"])
                if standing["average_seconds"] is not None
                else float("inf")
            ),
            standing["display_name"],
            standing["user_id"],
        )
    )
    rank = 0
    for standing in standings:
        if not standing["eligible"]:
            continue
        rank += 1
        standing["rank"] = rank
        standing["score"] = BASEBALL_RANK_POINTS.get(rank, 0)
    return standings

def settle_baseball_pitch(round_id: int, pitch_number: int) -> dict[str, Any] | None:
    """한 투구의 순위만 확정한다. 점수는 3구 평균 최종 순위에서 한 번 지급한다."""
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row or round_game_key(round_row) != "baseball":
            return None
        extra = round_extra(round_row)
        if int(extra.get("baseball_pitch") or 1) != pitch_number:
            return None
        if extra.get("baseball_phase") == "result_ready":
            return _baseball_pitch_result(conn, round_row, pitch_number)
        if extra.get("baseball_phase") != "swinging":
            return None

        swing_rows = conn.execute(
            """
            SELECT * FROM baseball_swings
            WHERE round_id = ? AND pitch_number = ?
            ORDER BY id ASC
            """,
            (round_id, pitch_number),
        ).fetchall()
        valid_rank = 0
        settlements: list[dict[str, Any]] = []
        for swing in swing_rows:
            rank: int | None = None
            if float(swing["elapsed_seconds"]) < BASEBALL_SWING_SECONDS:
                valid_rank += 1
                rank = valid_rank
            conn.execute(
                "UPDATE baseball_swings SET rank = ?, score = ? WHERE id = ?",
                (rank, 0, swing["id"]),
            )
            settlements.append(
                {
                    "user_id": str(swing["user_id"]),
                    "elapsed_seconds": float(swing["elapsed_seconds"]),
                    "rank": rank,
                }
            )

        log_activity(
            conn,
            "round",
            "baseball_pitch_recorded",
            event_session_id=round_row["event_session_id"],
            round_id=round_id,
            payload={"pitch_number": pitch_number, "results": settlements},
        )
        settled_pitches = [int(value) for value in extra.get("baseball_settled_pitches", [])]
        if pitch_number not in settled_pitches:
            settled_pitches.append(pitch_number)
        extra.update(
            {
                "baseball_phase": "result_ready",
                "baseball_settled_pitches": sorted(settled_pitches),
            }
        )
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )
        return _baseball_pitch_result(conn, round_row, pitch_number)

def advance_baseball_pitch(
    round_id: int,
    pitch_number: int,
    result_message_id: str,
    next_control_message_id: str | None = None,
) -> dict[str, Any] | None:
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row or round_game_key(round_row) != "baseball":
            return None
        extra = round_extra(round_row)
        if (
            extra.get("baseball_phase") != "result_ready"
            or int(extra.get("baseball_pitch") or 1) != pitch_number
        ):
            return None

        next_pitch = pitch_number + 1
        next_phase = "waiting_prepare" if next_pitch <= BASEBALL_TOTAL_PITCHES else "complete"
        result_message_ids = dict(extra.get("baseball_result_message_ids") or {})
        result_message_ids[str(pitch_number)] = result_message_id
        log_activity(
            conn,
            "round",
            "baseball_result_published",
            event_session_id=round_row["event_session_id"],
            round_id=round_id,
            payload={
                "pitch_number": pitch_number,
                "result_message_id": result_message_id,
                "next_control_message_id": next_control_message_id,
                "next_phase": next_phase,
            },
        )
        extra.update(
            {
                "baseball_phase": next_phase,
                "baseball_pitch": min(next_pitch, BASEBALL_TOTAL_PITCHES),
                "baseball_result_message_ids": result_message_ids,
            }
        )
        if next_control_message_id is not None:
            extra["baseball_control_message_id"] = next_control_message_id
        elif next_phase == "complete":
            extra.pop("baseball_control_message_id", None)
        extra.pop("baseball_button_position", None)
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )
        return {"round": dict(round_row), "extra": extra, "complete": next_phase == "complete"}

async def handle_baseball_swing(
    interaction: discord.Interaction,
    round_id: int,
    pitch_number: int,
) -> None:
    if interaction.user.bot:
        await interaction.response.send_message("봇 계정은 타격할 수 없습니다.", ephemeral=True)
        return
    try:
        visible_datetime = (
            getattr(interaction.message, "edited_at", None)
            or getattr(interaction.message, "created_at", None)
        )
        clicked_datetime = getattr(interaction, "created_at", None)
        result = record_baseball_swing(
            round_id,
            pitch_number,
            interaction.user,
            swung_at=(
                clicked_datetime.timestamp()
                if clicked_datetime is not None
                else None
            ),
            button_visible_at=(
                visible_datetime.timestamp() if visible_datetime is not None else None
            ),
            control_message_id=str(interaction.message.id),
        )
    except (ValueError, PermissionError) as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return

    elapsed = float(result["elapsed_seconds"])
    if result["already"]:
        message = f"이미 이번 투구에 스윙했습니다. 기록: **{elapsed:.2f}초**"
    elif result["rank"] is None:
        message = f"💨 헛스윙! **{elapsed:.2f}초** · 이 시간이 최종 평균에 반영됩니다."
    else:
        message = f"⚾ 스윙 기록 완료! **{elapsed:.2f}초** · 결과는 7초 후 공개됩니다."
    await interaction.response.send_message(message, ephemeral=True)

async def activate_baseball_pitch(
    round_id: int,
    pitch_number: int,
    expected_activate_at: float | None = None,
) -> bool:
    """와인드업 3초 뒤 같은 3×3 판의 무작위 한 칸을 활성화한다."""
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row or round_game_key(round_row) != "baseball":
            return False
        extra = round_extra(round_row)
        if (
            extra.get("baseball_phase") != "winding_up"
            or int(extra.get("baseball_pitch") or 1) != pitch_number
        ):
            return False
        try:
            activate_at = float(extra.get("baseball_activate_at") or 0)
        except (TypeError, ValueError):
            return False
        if expected_activate_at is not None and abs(activate_at - expected_activate_at) > 0.05:
            return False
        channel_id = str(round_row["channel_id"])
        main_message_id = str(round_row["message_id"] or "")
        control_message_id = str(extra.get("baseball_control_message_id") or "")
        prepared_by = str(extra.get("baseball_prepared_by") or "system")
        title = str(round_row["title"])

    channel = await runtime.resolve_text_channel(channel_id)
    if channel is None or not control_message_id:
        reset_baseball_prepare(round_id, pitch_number)
        return False
    try:
        control_message = await channel.fetch_message(int(control_message_id))
    except (discord.HTTPException, ValueError):
        reset_baseball_prepare(round_id, pitch_number)
        return False

    button_position = random.randrange(BASEBALL_BUTTON_SLOT_COUNT)
    try:
        activated_message = await control_message.edit(
            content=baseball_control_content(pitch_number, "swinging"),
            view=baseball_swing_view(
                round_id,
                pitch_number,
                button_position=button_position,
            ),
        )
    except discord.HTTPException:
        reset_baseball_prepare(round_id, pitch_number)
        return False

    edited_at = getattr(activated_message, "edited_at", None)
    visible_at = edited_at.timestamp() if edited_at is not None else time.time()
    try:
        begin_baseball_pitch(round_id, prepared_by, started_at=visible_at)
        timing = set_baseball_pitch_visible_at(
            round_id,
            pitch_number,
            visible_at,
            control_message_id,
            button_position,
        )
    except ValueError:
        try:
            await control_message.edit(
                content=baseball_control_content(pitch_number, "winding_up"),
                view=baseball_swing_view(
                    round_id,
                    pitch_number,
                    disabled=True,
                    button_position=None,
                ),
            )
        except discord.HTTPException:
            pass
        return False

    if main_message_id:
        try:
            main_message = await channel.fetch_message(int(main_message_id))
            await main_message.edit(
                embed=build_baseball_embed(
                    round_id,
                    title,
                    pitch_number,
                    "swinging",
                ),
                view=None,
            )
        except (discord.HTTPException, ValueError):
            pass

    _timer_client().schedule_baseball_pitch(
        round_id, float(timing["result_at"]), pitch_number
    )
    return True

async def publish_baseball_pitch_result(round_id: int, pitch_number: int) -> bool:
    result = settle_baseball_pitch(round_id, pitch_number)
    if not result:
        return False
    channel = await runtime.resolve_text_channel(str(result["channel_id"]))
    if channel is None:
        return False

    old_control_message = None
    if result["control_message_id"]:
        try:
            old_control_message = await channel.fetch_message(
                int(result["control_message_id"])
            )
            await old_control_message.edit(
                content=baseball_control_content(pitch_number, "result_ready"),
                view=baseball_swing_view(
                    round_id,
                    pitch_number,
                    disabled=True,
                    button_position=int(result["button_position"]),
                ),
            )
        except (discord.HTTPException, ValueError):
            old_control_message = None

    if result["message_id"]:
        try:
            message = await channel.fetch_message(int(result["message_id"]))
            await message.edit(
                embed=build_baseball_embed(
                    round_id,
                    str(result["title"]),
                    pitch_number,
                    "result_ready",
                ),
                view=None,
            )
        except (discord.HTTPException, ValueError):
            pass

    result_message = await channel.send(embed=build_baseball_pitch_result_embed(result))
    next_pitch = pitch_number + 1
    next_control_message = None
    if next_pitch <= BASEBALL_TOTAL_PITCHES:
        next_control_message = await channel.send(
            content=baseball_control_content(next_pitch, "waiting_prepare"),
            view=None,
        )

    advanced = advance_baseball_pitch(
        round_id,
        pitch_number,
        str(result_message.id),
        str(next_control_message.id) if next_control_message is not None else None,
    )
    if not advanced:
        if next_control_message is not None:
            try:
                await next_control_message.delete()
            except discord.HTTPException:
                pass
        return False

    if old_control_message is not None:
        try:
            await old_control_message.delete()
        except discord.HTTPException:
            pass

    extra = advanced["extra"]
    if result["message_id"]:
        try:
            message = await channel.fetch_message(int(result["message_id"]))
            await message.edit(
                embed=build_baseball_embed(
                    round_id,
                    str(result["title"]),
                    int(extra["baseball_pitch"]),
                    str(extra["baseball_phase"]),
                ),
                view=None,
            )
        except (discord.HTTPException, ValueError):
            pass

    if advanced["complete"]:
        await runtime.finish_round(round_id, channel, "3구 완료")
    return True

def settle_baseball(ctx: SettleContext) -> discord.Embed:
    """3구 평균 순위로 한 번만 지급한다. 구별 순위(baseball_swings.score)는 표시용이다."""
    completed_pitches = {
        int(value) for value in ctx.extra.get("baseball_settled_pitches", [])
    }
    standings = baseball_final_standings(ctx.conn, ctx.round_row, completed_pitches)
    for standing in standings:
        score = int(standing["score"])
        if not score:
            continue
        add_score(
            ctx.conn,
            str(standing["user_id"]),
            str(standing["display_name"]),
            score,
            (
                f"라운드 #{ctx.round_row['id']} 야구 3구 평균 "
                f"{float(standing['average_seconds']):.2f}초 "
                f"{standing['rank']}위"
            ),
            ctx.round_row["id"],
        )
    return build_baseball_final_result_embed(str(ctx.round_row["title"]), standings)

def build_baseball_embed(
    round_id: int,
    title: str,
    pitch_number: int,
    phase: str = "waiting_prepare",
) -> discord.Embed:
    pitch_number = max(1, min(BASEBALL_TOTAL_PITCHES, int(pitch_number)))
    status_by_phase = {
        "waiting_prepare": "투수가 준비중입니다.",
        "winding_up": "투수가 와인드업 중입니다. 3초 후 치기 버튼이 활성화됩니다.",
        "swinging": "지금 `치기` 버튼을 누르세요! 3.00초부터는 헛스윙입니다.",
        "result_ready": "이번 투구 결과를 집계하고 있습니다.",
        "complete": "3구가 모두 끝났습니다.",
    }
    embed = discord.Embed(
        title=f"⚾ {title}",
        description=f"## <{pitch_number}구/{BASEBALL_TOTAL_PITCHES}구>",
        color=discord.Color.blue(),
    )
    embed.set_image(url=f"attachment://{BASEBALL_IMAGE_FILENAME}")
    embed.add_field(
        name="현재 상태",
        value=status_by_phase.get(phase, status_by_phase["waiting_prepare"]),
        inline=False,
    )
    embed.add_field(
        name="최종 배점",
        value="3구 평균 기록 기준",
        inline=False,
    )
    for medal, rank, score in (("🥇", 1, 3), ("🥈", 2, 2), ("🥉", 3, 1)):
        embed.add_field(
            name=f"{medal} {rank}위",
            value=f"**+{score}점**",
            inline=True,
        )
    embed.add_field(
        name="미입력 처리",
        value="누르지 못한 투구는 평균 계산에서 **7.00초**로 반영됩니다.",
        inline=False,
    )
    embed.set_footer(text=f"라운드 #{round_id} · 치기 버튼이 열린 뒤 7초 후 결과 공개")
    return embed

def baseball_control_content(pitch_number: int, phase: str) -> str:
    pitch_number = max(1, min(BASEBALL_TOTAL_PITCHES, int(pitch_number)))
    guide_by_phase = {
        "waiting_prepare": "",
        "winding_up": "투수 와인드업",
        "swinging": "지금 `치기`를 누르세요!",
        "result_ready": "이번 투구 결과를 집계하고 있습니다.",
        "complete": "3구가 모두 끝났습니다.",
    }
    guide = guide_by_phase.get(phase, guide_by_phase["waiting_prepare"])
    title = (
        f"⚾ **투수 와인드업 · <{pitch_number}구/{BASEBALL_TOTAL_PITCHES}구>**"
        if phase == "winding_up"
        else f"⚾ **야구 조작판 · <{pitch_number}구/{BASEBALL_TOTAL_PITCHES}구>**"
    )
    if phase == "winding_up":
        return title
    return f"{title}\n{guide}" if guide else title

def baseball_swing_view(
    round_id: int,
    pitch_number: int,
    disabled: bool = False,
    button_position: int | None = 0,
) -> discord.ui.View:
    if button_position is not None:
        button_position = max(
            0,
            min(BASEBALL_BUTTON_SLOT_COUNT - 1, int(button_position)),
        )
    view = discord.ui.View(timeout=None)
    for slot in range(BASEBALL_BUTTON_SLOT_COUNT):
        is_swing_button = button_position is not None and slot == button_position
        view.add_item(
            discord.ui.Button(
                label="치기",
                emoji="🏏",
                style=(
                    discord.ButtonStyle.success
                    if is_swing_button
                    else discord.ButtonStyle.secondary
                ),
                custom_id=(
                    f"eventbot:baseballswing:{round_id}:{pitch_number}"
                    if is_swing_button
                    else f"eventbot:baseballslot:{round_id}:{pitch_number}:{slot}"
                ),
                disabled=disabled or not is_swing_button,
                row=slot // BASEBALL_BUTTON_GRID_SIZE,
            )
        )
    return view

def baseball_result_lines(
    participants: list[dict[str, Any]],
    swings: list[dict[str, Any]],
) -> list[str]:
    """개별 투구는 정산 없이 반응 시간만 빠른 순서로 보여준다."""
    swing_by_user = {str(row["user_id"]): row for row in swings}
    ordered = sorted(
        participants,
        key=lambda participant: (
            float(
                swing_by_user.get(str(participant["user_id"]), {}).get(
                    "elapsed_seconds",
                    BASEBALL_MISSED_SWING_SECONDS,
                )
            ),
            str(participant["display_name"]),
        ),
    )
    lines: list[str] = []
    for participant in ordered:
        user_id = str(participant["user_id"])
        display_name = discord.utils.escape_markdown(str(participant["display_name"]))
        swing = swing_by_user.get(user_id)
        if swing and swing.get("rank") is not None:
            elapsed = float(swing["elapsed_seconds"])
            lines.append(f"🏏 {display_name} · **{elapsed:.2f}초**")
        elif swing:
            lines.append(
                f"💨 {display_name} · **{float(swing['elapsed_seconds']):.2f}초** (헛스윙)"
            )
        else:
            lines.append(
                f"💨 {display_name} · **{BASEBALL_MISSED_SWING_SECONDS:.2f}초** (미입력)"
            )
    return lines

def build_baseball_pitch_result_embed(result: dict[str, Any]) -> discord.Embed:
    lines = baseball_result_lines(result["participants"], result["swings"])
    embed = discord.Embed(
        title=f"⚾ {result['pitch_number']}구 결과 · {result['title']}",
        description="\n".join(lines) if lines else "참가자가 없어 기록이 없습니다.",
        color=discord.Color.gold(),
    )
    embed.set_footer(
        text=(
            f"3.00초부터 헛스윙 · {result['pitch_number']}구/{BASEBALL_TOTAL_PITCHES}구 · "
            "점수는 3구 평균 최종 순위에서 지급"
        )
    )
    return embed

def build_baseball_final_result_embed(
    title: str,
    standings: list[dict[str, Any]],
) -> discord.Embed:
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines: list[str] = []
    for standing in standings:
        display_name = discord.utils.escape_markdown(str(standing["display_name"]))
        rank = standing.get("rank")
        if rank is not None:
            average_seconds = float(standing["average_seconds"])
            score = int(standing["score"])
            missed_swing_count = int(standing["missed_swing_count"])
            missed_text = (
                f" · 미입력 {missed_swing_count}구는 7.00초 반영"
                if missed_swing_count
                else ""
            )
            lines.append(
                f"{medals.get(int(rank), '▫️')} **{rank}위** · {display_name} · "
                f"평균 **{average_seconds:.2f}초** · +{score}점{missed_text}"
            )
        else:
            lines.append(
                f"▫️ **라운드 미완료** · {display_name} · 0점"
            )

    embed = discord.Embed(
        title=f"⚾ 최종 결과 · {title}",
        description="\n".join(lines) if lines else "참가자가 없어 기록이 없습니다.",
        color=discord.Color.gold(),
    )
    return embed

async def prepare_command(interaction: discord.Interaction) -> None:
    """`/준비` 본체. 슬래시 등록 껍데기는 bot.py에 있다 (6단계에서 commands/로 간다)."""
    await interaction.response.defer(ephemeral=True)
    with db() as conn:
        round_row = get_open_round(conn)
        if not round_row or round_game_key(round_row) != "baseball":
            await interaction.followup.send("진행 중인 야구 라운드가 없습니다.", ephemeral=True)
            return
        round_id = int(round_row["id"])
        channel_id = str(round_row["channel_id"])
        main_message_id = str(round_row["message_id"] or "")
        old_control_message_id = str(
            round_extra(round_row).get("baseball_control_message_id") or ""
        )

    channel = await runtime.resolve_text_channel(channel_id)
    if channel is None:
        await interaction.followup.send("야구 채널을 찾을 수 없습니다.", ephemeral=True)
        return

    try:
        transition = prepare_baseball_pitch(round_id, str(interaction.user.id))
    except ValueError as error:
        await interaction.followup.send(str(error), ephemeral=True)
        return

    pitch_number = int(transition["pitch_number"])
    windup_view = baseball_swing_view(
        round_id,
        pitch_number,
        disabled=True,
        button_position=None,
    )
    control_message = None
    if old_control_message_id:
        try:
            control_message = await channel.fetch_message(int(old_control_message_id))
            control_message = await control_message.edit(
                content=baseball_control_content(pitch_number, "winding_up"),
                view=windup_view,
            )
        except (discord.HTTPException, ValueError):
            control_message = None

    if control_message is None:
        try:
            control_message = await channel.send(
                content=baseball_control_content(pitch_number, "winding_up"),
                view=windup_view,
            )
        except discord.HTTPException:
            control_message = None

    if control_message is None:
        reset_baseball_prepare(round_id, pitch_number)
        await interaction.followup.send(
            "비활성 스트라이크존을 게시하지 못했습니다. 다시 시도해주세요.",
            ephemeral=True,
        )
        return

    visible_datetime = (
        getattr(control_message, "edited_at", None)
        or getattr(control_message, "created_at", None)
    )
    visible_at = (
        visible_datetime.timestamp() if visible_datetime is not None else time.time()
    )
    activate_at = visible_at + BASEBALL_WINDUP_SECONDS
    try:
        set_baseball_activation_at(
            round_id,
            pitch_number,
            activate_at,
            str(control_message.id),
        )
    except ValueError as error:
        reset_baseball_prepare(round_id, pitch_number)
        await interaction.followup.send(str(error), ephemeral=True)
        return

    _timer_client().schedule_baseball_activation(round_id, activate_at, pitch_number)

    if main_message_id:
        try:
            main_message = await channel.fetch_message(int(main_message_id))
            await main_message.edit(
                embed=build_baseball_embed(
                    round_id,
                    str(transition["round"]["title"]),
                    pitch_number,
                    "winding_up",
                ),
                view=None,
            )
        except (discord.HTTPException, ValueError):
            pass

    await interaction.followup.send(
        f"{pitch_number}구 와인드업을 시작했습니다. 3초 후 치기 버튼이 자동으로 활성화됩니다.",
        ephemeral=True,
    )

register_settler("baseball", settle_baseball)
