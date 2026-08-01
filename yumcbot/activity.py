"""활동 로그 (`activity_logs`).

사후 분석(SQL·에이전트)용 **append-only 이벤트 스트림**. `log_activity()` 하나로만 쓴다.
**절대 UPDATE/DELETE 하지 않는다.**

존재 이유가 곧 사용 기준이다. 기존 테이블 대부분은 *현재 상태*라 덮어쓰기·삭제가
일어나고(votes의 choice upsert, event_participants의 DELETE, 카운터 증가,
extra_json 통째 교체), 그래서 '무슨 일이 있었는지'는 그 테이블들만으로 복원할 수 없다.
**상태를 덮어쓰기 직전에 로그를 남긴다** — 새 기능이 상태를 덮어쓴다면
log_activity() 호출을 함께 넣어야 한다.

이미 append-only인 것(score_logs, discussion_messages)은 내용을 중복 저장하지 않고
참조만 남긴다. 자세한 배경은 AGENTS.md의 '활동 로그' 절.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import discord

from .config import (
    BAIL_FAILED_REWARD_POOL,
    BAIL_NEGOTIATION_MAX_OFFERS,
    BAIL_PROGRESS_UPDATE_INTERVAL,
    BAIL_REQUIRED_SCOOPS,
    BAIL_REWARD_POOL,
    BAIL_SCOOP_COST,
    BAIL_SINK_PENALTY_EVEN,
    BAIL_SINK_PENALTY_HIGH,
    BAIL_SINK_PENALTY_LOW,
    BASEBALL_BUTTON_GRID_SIZE,
    BASEBALL_BUTTON_SLOT_COUNT,
    BASEBALL_MISSED_SWING_SECONDS,
    BASEBALL_RANK_POINTS,
    BASEBALL_RESULT_SECONDS,
    BASEBALL_SWING_SECONDS,
    BASEBALL_TOTAL_PITCHES,
    BASEBALL_WINDUP_SECONDS,
    BOT_VERSION,
    CUTTING_REWARD_POINTS,
    CUTTING_SUCCESS_RATES,
    DEFUSE_COLORS,
    MATCH_HISTORY_ROUNDS,
    SAND_NUMBER_CHOICES,
    SAND_PLAY_SECONDS,
    TRUST_DECISION_SECONDS,
    UNIQUE_NUMBER_CHOICES,
)
from .db import now_text


def user_display_name(user: discord.abc.User) -> str | None:
    """로그에 남길 표시 이름. 로깅 때문에 게임이 죽으면 안 되므로 속성이 없어도 견딘다."""
    return getattr(user, "display_name", None) or getattr(user, "name", None)


def log_activity(
    conn: sqlite3.Connection,
    category: str,
    action: str,
    *,
    event_session_id: int | None = None,
    round_id: int | None = None,
    actor_user_id: str | None = None,
    target_user_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """활동 로그 한 줄. 사후 분석용 append-only 기록이라 절대 UPDATE/DELETE 하지 않는다.

    호출자가 이미 쥐고 있는 conn에 얹어서 같은 트랜잭션으로 커밋한다. 별도 커넥션을
    열지 않으므로 추가 fsync가 없고, 본 작업이 롤백되면 로그도 같이 롤백돼 어긋나지 않는다.

    로깅 실패가 게임 진행을 막으면 안 되므로 여기서 예외를 삼킨다. 다만 조용히 사라지면
    누락을 눈치챌 수 없으니 stderr에는 남긴다.

    category: event|round|match|participation|score|roulette|vote|thread
    payload: JSON 직렬화 가능한 세부. 게임마다 다른 값은 전부 여기에 넣어
             새 게임이 추가돼도 스키마 마이그레이션이 필요 없게 한다.
    """
    try:
        conn.execute(
            """
            INSERT INTO activity_logs
                (created_at, category, action, event_session_id, round_id,
                 actor_user_id, target_user_id, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_text(),
                category,
                action,
                event_session_id,
                round_id,
                actor_user_id,
                target_user_id,
                json.dumps(payload or {}, ensure_ascii=False, default=str),
            ),
        )
    except sqlite3.Error as error:
        print(f"활동 로그 기록 실패({category}:{action}): {error}", flush=True)


def game_settings_snapshot(game_key: str) -> dict[str, Any]:
    """라운드가 시작될 때의 규칙값. 규칙을 조정해도 과거 라운드를 당시 기준으로 해석하기 위한 것.

    점수 규칙 일부(밸런스 몰표 -2점, 소수파 +2점 등)는 상수가 아니라
    close_round_record 분기에 하드코딩돼 있어 여기서 값으로 뜰 수 없다.
    그래서 bot_version(배포 커밋 SHA)을 항상 함께 남긴다 — 그 커밋을 열면
    당시 정산 규칙을 그대로 확인할 수 있다. 튜닝 가능한 상수는 값으로도 남긴다.
    """
    settings: dict[str, Any] = {"bot_version": BOT_VERSION}

    if game_key == "cutting":
        settings["reward_points"] = CUTTING_REWARD_POINTS
        settings["success_rates"] = CUTTING_SUCCESS_RATES
    elif game_key == "trust":
        settings["decision_seconds"] = TRUST_DECISION_SECONDS
        settings["match_history_rounds"] = MATCH_HISTORY_ROUNDS
    elif game_key == "sand":
        settings["play_seconds"] = SAND_PLAY_SECONDS
        settings["number_choices"] = SAND_NUMBER_CHOICES
        settings["match_history_rounds"] = MATCH_HISTORY_ROUNDS
    elif game_key == "bail":
        settings["reward_pool"] = BAIL_REWARD_POOL
        settings["failed_reward_pool"] = BAIL_FAILED_REWARD_POOL
        settings["required_scoops"] = BAIL_REQUIRED_SCOOPS
        settings["scoop_cost"] = BAIL_SCOOP_COST
        settings["sink_penalty_high"] = BAIL_SINK_PENALTY_HIGH
        settings["sink_penalty_low"] = BAIL_SINK_PENALTY_LOW
        settings["sink_penalty_even"] = BAIL_SINK_PENALTY_EVEN
        settings["max_offers"] = BAIL_NEGOTIATION_MAX_OFFERS
        settings["progress_update_interval"] = BAIL_PROGRESS_UPDATE_INTERVAL
        settings["match_history_rounds"] = MATCH_HISTORY_ROUNDS
    elif game_key == "unique_number":
        settings["number_choices"] = UNIQUE_NUMBER_CHOICES
    elif game_key == "baseball":
        settings["total_pitches"] = BASEBALL_TOTAL_PITCHES
        settings["swing_seconds"] = BASEBALL_SWING_SECONDS
        settings["result_seconds"] = BASEBALL_RESULT_SECONDS
        settings["windup_seconds"] = BASEBALL_WINDUP_SECONDS
        settings["missed_swing_seconds"] = BASEBALL_MISSED_SWING_SECONDS
        settings["button_grid_size"] = BASEBALL_BUTTON_GRID_SIZE
        settings["button_slot_count"] = BASEBALL_BUTTON_SLOT_COUNT
        settings["rank_points"] = BASEBALL_RANK_POINTS
        settings["ranking_rule"] = "three_pitch_average"
    elif game_key == "defuse4":
        settings["colors"] = [color["id"] for color in DEFUSE_COLORS]

    return settings
