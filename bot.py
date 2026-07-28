from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import random
import secrets
import signal
import socket
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import discord
from discord import app_commands


from yumcbot import config, runtime

# 경로·상수는 yumcbot.config로, 스키마·연결은 yumcbot.db로 옮겼다.
# 여기서 다시 노출하는 이유: bot.py의 함수들이 아직 이 이름들을 모듈 전역으로 읽고,
# 테스트도 patch.object(bot, ...)로 일부를 갈아끼운다. 게임 모듈 이관이 끝나면 사라진다.
from yumcbot.config import (
    AFK_COIN_PER_TICK,
    AFK_TICK_SECONDS,
    ANONYMOUS_EMBED_COLORS,
    ANONYMOUS_GUIDE_TEXT,
    AUTO_CLOSE_COUNTDOWN_SECONDS,
    AUTO_CLOSE_WARNING_SECONDS,
    BAIL_DEFAULT_EXECUTION_MINUTES,
    BAIL_DEFAULT_NEGOTIATION_MINUTES,
    BAIL_FAILED_REWARD_POOL,
    BAIL_FALLBACK_SCOOPS,
    BAIL_NEGOTIATION_MAX_OFFERS,
    BAIL_OUTCOME_LABELS,
    BAIL_REQUIRED_SCOOPS,
    BAIL_REWARD_CHOICES,
    BAIL_REWARD_POOL,
    BAIL_SCOOP_CHOICES,
    BAIL_SCOOP_COST,
    BAIL_SINK_PENALTY_EVEN,
    BAIL_SINK_PENALTY_HIGH,
    BAIL_SINK_PENALTY_LOW,
    BALANCE_VOTE_CHOICES,
    BASEBALL_BUTTON_GRID_SIZE,
    BASEBALL_BUTTON_SLOT_COUNT,
    BASEBALL_IMAGE_FILENAME,
    BASEBALL_IMAGE_PATH,
    BASEBALL_MISSED_SWING_SECONDS,
    BASEBALL_RANK_POINTS,
    BASEBALL_RESULT_SECONDS,
    BASEBALL_SWING_SECONDS,
    BASEBALL_TOTAL_PITCHES,
    BASEBALL_WINDUP_SECONDS,
    BOT_VERSION,
    CUTTING_LEVEL_CHOICES,
    CUTTING_REWARD_POINTS,
    CUTTING_SUCCESS_RATES,
    DEFUSE_COLORS,
    DEFUSE_COLOR_BY_ID,
    EMERGENCY_EXTENSION_SECONDS,
    EVENT_STATUS_LABELS,
    KST_LABEL,
    MATCH_HISTORY_ROUNDS,
    ROULETTE_BETTING_SECONDS,
    ROULETTE_CHALLENGE_SECONDS,
    ROULETTE_DAILY_LIMIT,
    ROULETTE_MAX_BET,
    ROULETTE_MIN_BET,
    ROULETTE_PAIR_DAILY_LIMIT,
    ROULETTE_STACK_GOAL,
    ROULETTE_STACK_REWARD,
    ROULETTE_TURN_SECONDS,
    SAND_NUMBER_CHOICES,
    SAND_OUTCOME_LABELS,
    SAND_PLAY_SECONDS,
    SAND_RULES_TEXT,
    SCORE_REVEAL_COST,
    TOP3_NONE_CHOICE,
    TRUST_DECISION_SECONDS,
    TRUST_OUTCOME_LABELS,
    TRUST_REGISTRATION_DEFAULT_MINUTES,
    UNIQUE_NUMBER_CHOICES,
)
from yumcbot.db import (
    now_text,
    BASELINE_VERSION,
    BASE_SCHEMA,
    MIGRATIONS,
    SCHEMA_VERSION,
    apply_baseline_schema,
    canonical_schema,
    db,
    ensure_event_session_schema,
    ensure_trust_pairing_schema,
    init_db,
    migrate_round_type_check,
    verify_schema,
)
from yumcbot.activity import (
    game_settings_snapshot,
    log_activity,
    user_display_name,
)
from yumcbot.games.registry import (
    GAME_NAMES,
    MAIN_GAME_TYPES,
    MINI_GAME_TYPES,
    SettleContext,
    register_settler,
    settler_for,
)
from yumcbot.ui import (
    add_field_divider,
    choice_display,
    close_time_text,
    field_text,
    member_name,
    mention_list,
)
from yumcbot.games.pairing import (
    build_random_trust_pairs,
    create_pair_thread,
    emergency_join_notice,
    emergency_join_view,
    ensure_pair_thread_permissions,
    recent_match_pairs,
    save_pair_thread_id,
    warn_failed_pair_threads,
)
from yumcbot.games import bail
from yumcbot.games.bail import (
    advance_or_finish_bail_round,
    apply_bail_offer,
    apply_bail_response,
    apply_bail_scoop,
    autocomplete_bail_pair,
    bail_negotiations_complete,
    bail_new_pair,
    bail_pair_intro,
    bail_pair_settlement_text,
    bail_pair_status_text,
    bail_pair_thread_matches,
    bail_pair_threads,
    bail_pair_view,
    bail_reward_share,
    bail_rules_text,
    bail_score_text,
    bail_side_key,
    bail_split_text,
    build_bail_pair_embed,
    build_bail_pair_result_embed,
    build_bail_play_embed,
    build_bail_registration_embed,
    create_bail_pair_threads,
    find_bail_pair,
    handle_bail_offer,
    handle_bail_response,
    handle_bail_scoop,
    load_open_bail_pair,
    publish_bail_pair_results,
    redraw_bail_pair_board,
    refresh_all_bail_pair_boards,
    refresh_bail_pair_board,
    refresh_main_bail_board,
    resolve_bail_pair,
    save_bail_pair_message_id,
    settle_bail,
    start_bail_execution,
    start_bail_negotiation,
    store_bail_thread_ids,
)
from yumcbot.games import baseball
from yumcbot.games.baseball import (
    _baseball_pitch_result,
    activate_baseball_pitch,
    advance_baseball_pitch,
    baseball_control_content,
    baseball_final_standings,
    baseball_result_lines,
    baseball_swing_view,
    begin_baseball_pitch,
    build_baseball_embed,
    build_baseball_final_result_embed,
    build_baseball_pitch_result_embed,
    delete_baseball_control_message,
    handle_baseball_swing,
    prepare_baseball_pitch,
    publish_baseball_pitch_result,
    record_baseball_swing,
    reset_baseball_prepare,
    set_baseball_activation_at,
    set_baseball_pitch_visible_at,
    settle_baseball,
    settle_baseball_pitch,
)
from yumcbot.rounds import (
    can_join_round,
    get_open_round,
    is_event_participant,
    main_game_phase,
    round_extra,
    round_game_key,
)
from yumcbot.scoring import (
    add_score,
    ensure_user,
    get_afk_coins,
    get_roulette_stack,
    grant_afk_coins,
    reset_roulette_stack_if_escaped,
    set_roulette_stack,
    spend_afk_coins,
)
from yumcbot.roulette import (
    active_roulette_game_for_user,
    build_roulette_embed,
    count_roulette_games_today,
    count_roulette_games_with_opponent_today,
    expire_stale_roulette_games,
    handle_roulette_accept,
    handle_roulette_bet,
    handle_roulette_decline,
    handle_roulette_score,
    handle_roulette_settle,
    handle_roulette_trigger,
    is_roulette_credit_user,
    pull_roulette_trigger,
    refresh_roulette_message,
    roulette_betting_view,
    roulette_challenge_view,
    roulette_day_key,
    roulette_play_view,
    roulette_player_score_message,
    roulette_settlement_message,
    roulette_view,
    submit_roulette_bet,
    timeout_roulette_turn,
)


def cutting_success_rate(current_level: int) -> float:
    return CUTTING_SUCCESS_RATES[current_level]


def _group_consecutive(values: dict[int, Any]) -> list[tuple[list[int], Any]]:
    """연속하면서 값이 같은 단계들을 한 묶음으로 만든다. 안내문을 상수에서 생성하기 위한 것."""
    groups: list[tuple[list[int], Any]] = []
    for level in sorted(values):
        if groups and groups[-1][1] == values[level] and groups[-1][0][-1] == level - 1:
            groups[-1][0].append(level)
        else:
            groups.append(([level], values[level]))
    return groups


def cutting_success_rate_text() -> str:
    """코드블록에 넣을 한 줄 한 항목 표. 예: '0→1·1→2 90%\\n2→3·3→4 60%\\n4→5     50%'

    가장 긴 라벨은 공백 하나만 붙는다. 라벨 폭을 len으로 맞춰도 열이 어긋나지 않는 이유는
    항목마다 한글 글자 수가 같아서다(강화/없음). 그 전제가 깨지면 표시 폭으로 계산해야 한다.
    """
    rows = []
    for levels, rate in _group_consecutive(CUTTING_SUCCESS_RATES):
        steps = "·".join(f"{level}→{level + 1}" for level in levels)
        rows.append((steps, f"{rate * 100:.0f}%"))
    width = max(len(steps) for steps, _ in rows)
    return "\n".join(f"{steps:<{width}} {value}" for steps, value in rows)


def cutting_reward_text() -> str:
    """코드블록에 넣을 한 줄 한 항목 표. 예: '0~1강화 +3점\\n2~4강화 +1점\\n5강화   +2점'"""
    rows = []
    for levels, points in _group_consecutive(CUTTING_REWARD_POINTS):
        span = f"{levels[0]}강화" if len(levels) == 1 else f"{levels[0]}~{levels[-1]}강화"
        rows.append((span, f"+{points}점"))
    width = max(len(span) for span, _ in rows)
    return "\n".join(f"{span:<{width}} {value}" for span, value in rows)


def assign_anonymous_number(conn: sqlite3.Connection, round_id: int, user_id: str) -> int:
    """라운드 안에서 유저에게 고정으로 붙는 익명 번호. 첫 발언 때 1부터 순서대로 발급한다."""
    existing = conn.execute(
        "SELECT anon_number FROM discussion_messages WHERE round_id = ? AND user_id = ? LIMIT 1",
        (round_id, user_id),
    ).fetchone()
    if existing:
        return int(existing["anon_number"])

    row = conn.execute(
        "SELECT COALESCE(MAX(anon_number), 0) + 1 AS next FROM discussion_messages WHERE round_id = ?",
        (round_id,),
    ).fetchone()
    return int(row["next"])


def build_anonymous_message_embed(anon_number: int, content: str) -> discord.Embed:
    """작성자 이름 없이 내부 익명 번호마다 고정 색상만 붙이는 토론 발언 카드."""
    color = ANONYMOUS_EMBED_COLORS[(anon_number - 1) % len(ANONYMOUS_EMBED_COLORS)]
    return discord.Embed(
        description=content,
        color=color,
    )


# TODO(activity-log): 운영자용 조회/내보내기 수단이 아직 없다.
# 당분간은 서버에서 직접 조회한다:
#   sqlite3 /var/lib/yumcbot/data/event_game.sqlite3 \
#     "SELECT created_at, category, action, actor_user_id, payload_json
#      FROM activity_logs WHERE event_session_id = 12 ORDER BY created_at"
# 필요해지면 /로그 슬래시 명령이나 CSV 내보내기를 붙인다.


def get_current_event(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM event_sessions
        WHERE status IN ('recruiting', 'active')
        ORDER BY id DESC LIMIT 1
        """
    ).fetchone()


def force_close_current_event(
    conn: sqlite3.Connection,
) -> tuple[sqlite3.Row, list[sqlite3.Row]] | None:
    event_row = get_current_event(conn)
    if not event_row:
        return None

    round_rows = conn.execute(
        """
        SELECT * FROM rounds
        WHERE event_session_id = ?
        ORDER BY id ASC
        """,
        (event_row["id"],),
    ).fetchall()
    closed_at = now_text()

    for round_row in round_rows:
        extra = round_extra(round_row)
        is_open = round_row["status"] == "open"
        is_unresolved_cutting = (
            round_row["type"] == "cutting" and "cutting_result_level" not in extra
        )
        if not is_open and not is_unresolved_cutting:
            continue
        extra.update(
            {
                "force_closed": True,
                "force_closed_reason": "development_command",
            }
        )
        if is_open and round_row["type"] == "trust":
            extra["trust_phase"] = "cancelled"
        elif is_open and round_row["type"] == "sand":
            extra["sand_phase"] = "cancelled"
        conn.execute(
            """
            UPDATE rounds
            SET status = 'closed', closed_at = COALESCE(closed_at, ?), extra_json = ?
            WHERE id = ?
            """,
            (closed_at, json.dumps(extra, ensure_ascii=False), round_row["id"]),
        )

    conn.execute(
        """
        UPDATE event_sessions
        SET status = 'closed', closed_at = ?
        WHERE id = ?
        """,
        (closed_at, event_row["id"]),
    )
    # 정상 완료와 강제 마감은 event_sessions에서 똑같이 'closed'로만 보인다.
    # 어느 쪽으로 끝났는지는 이 로그에만 남는다.
    log_activity(
        conn,
        "event",
        "close",
        event_session_id=int(event_row["id"]),
        payload={
            "reason": "force_closed",
            "round_count": len(round_rows),
            "round_ids": [int(row["id"]) for row in round_rows],
        },
    )
    return event_row, round_rows


def event_rounds_are_complete(round_rows: list[sqlite3.Row]) -> bool:
    """이벤트에 예정된 게임이 모두 정산됐는지 확인한다."""
    if not round_rows or any(row["status"] != "closed" for row in round_rows):
        return False

    for index, round_row in enumerate(round_rows):
        extra = round_extra(round_row)
        if extra.get("force_closed"):
            return False
        if round_row["type"] == "cutting" and "cutting_result_level" not in extra:
            return False
        if round_row["type"] not in MAIN_GAME_TYPES:
            continue

        planned_mini_game = str(extra.get("planned_mini_game") or "")
        if planned_mini_game not in MINI_GAME_TYPES:
            continue
        if not any(
            round_game_key(candidate) == planned_mini_game
            and candidate["status"] == "closed"
            and not round_extra(candidate).get("force_closed")
            for candidate in round_rows[index + 1 :]
        ):
            return False
    return True


def build_event_summary_embed(
    conn: sqlite3.Connection,
    event_row: sqlite3.Row | dict[str, Any],
    round_rows: list[sqlite3.Row],
    *,
    force_closed: bool = False,
) -> discord.Embed:
    """종료 시점에 메인 채널에 남길 이벤트 종료 알림. 세부 정산은 각 게임 메시지에 이미 있다."""
    if force_closed:
        return discord.Embed(
            title="⛔ 이벤트 강제 종료",
            description=(
                "남은 게임을 정산하지 않고 이벤트를 종료했습니다.\n"
                "진행 기록과 스레드는 그대로 보관됩니다.\n"
                "수고하셨습니다!"
            ),
            color=discord.Color.orange(),
        )
    return discord.Embed(
        title="🏁 이벤트 종료",
        description=(
            "예정된 게임이 모두 종료되었습니다.\n"
            "세부 결과는 각 게임 메시지와 보관된 스레드에서 확인할 수 있습니다.\n"
            "수고하셨습니다!"
        ),
        color=discord.Color.gold(),
    )


def close_completed_event(event_session_id: int) -> tuple[str, discord.Embed] | None:
    """모든 게임이 끝난 이벤트를 한 번만 종료 상태로 전환하고 요약을 만든다."""
    with db() as conn:
        event_row = conn.execute(
            "SELECT * FROM event_sessions WHERE id = ? AND status != 'closed'",
            (event_session_id,),
        ).fetchone()
        if not event_row:
            return None
        round_rows = conn.execute(
            "SELECT * FROM rounds WHERE event_session_id = ? ORDER BY id ASC",
            (event_session_id,),
        ).fetchall()
        if not event_rounds_are_complete(round_rows):
            return None

        closed_at = now_text()
        cursor = conn.execute(
            """
            UPDATE event_sessions SET status = 'closed', closed_at = ?
            WHERE id = ? AND status != 'closed'
            """,
            (closed_at, event_session_id),
        )
        if cursor.rowcount != 1:
            return None

        participant_count = int(
            conn.execute(
                "SELECT COUNT(*) AS count FROM event_participants WHERE event_session_id = ?",
                (event_session_id,),
            ).fetchone()["count"]
        )
        log_activity(
            conn,
            "event",
            "close",
            event_session_id=event_session_id,
            payload={
                "reason": "completed",
                "participant_count": participant_count,
                # 이벤트 구성(메인->미니 / 미니 단독)을 사후에 한 줄로 알 수 있게 남긴다.
                "rounds": [
                    {
                        "round_id": int(row["id"]),
                        "game_key": round_game_key(row),
                        "title": str(row["title"]),
                    }
                    for row in round_rows
                ],
            },
        )
        event_values = dict(event_row)
        event_values["closed_at"] = closed_at
        return str(event_row["channel_id"]), build_event_summary_embed(
            conn,
            event_values,
            round_rows,
        )


async def publish_event_summary_if_complete(event_session_id: int | None) -> None:
    if not event_session_id:
        return
    summary = close_completed_event(int(event_session_id))
    if not summary:
        return
    channel_id, embed = summary
    channel = await runtime.resolve_text_channel(channel_id)
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


def format_my_score_message(third_score: int | None, my_score: int) -> str:
    """3등 점수와 내 점수만 알려준다. 이름은 넣지 않는다.

    3등이 동점이어도 보여주는 값이 '점수'뿐이라, 누가 3등으로 뽑히든 결과가 같다.
    (동점자가 몇 명인지 유저가 알 수 없어야 한다.)
    """
    third_text = f"**{third_score}점**" if third_score is not None else "아직 없음"
    return f"3등 점수: {third_text}\n내 점수: **{my_score}점**"


def fetch_ranking_rows(conn: sqlite3.Connection, current_bot_id: str) -> list[sqlite3.Row]:
    """점수 순으로 줄 세운 참여자 명단. 게임 기록이 있는 사람만, 봇은 제외.

    동점자는 user_id 순으로 고정한다. 점수가 그대로면 몇 번을 불러도 같은 사람이 3등으로
    뽑혀야 하기 때문이다. (updated_at으로 정렬하면 add_score가 그 값을 건드려 순서가 흔들린다.)
    """
    return conn.execute(
        """
        SELECT users.user_id, users.display_name, users.total_score
        FROM users
        WHERE users.user_id != ?
          AND (
              EXISTS (
                  SELECT 1 FROM votes
                  WHERE votes.user_id = users.user_id
              )
              OR EXISTS (
                  SELECT 1 FROM trust_registrations
                  WHERE trust_registrations.user_id = users.user_id
              )
          )
        ORDER BY users.total_score DESC, users.user_id ASC
        """,
        (current_bot_id,),
    ).fetchall()


def format_public_ranking_lines(rows: list[sqlite3.Row], start_rank: int = 1) -> list[str]:
    """공개 랭킹에 표시할 순위, 멘션, 점수를 만든다."""
    return [
        f"{index}. <@{row['user_id']}> - **{row['total_score']}점**"
        for index, row in enumerate(rows, start=start_rank)
    ]


def format_anonymous_ranking_lines(rows: list[sqlite3.Row], start_rank: int = 1) -> list[str]:
    """사용자 정보 없이 공개할 순위와 점수만 만든다."""
    return [
        f"{index}등 - **{row['total_score']}점**"
        for index, row in enumerate(rows, start=start_rank)
    ]


def fetch_admin_score_rows(conn: sqlite3.Connection, current_bot_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT user_id, display_name, total_score, updated_at
        FROM users
        WHERE user_id != ?
        ORDER BY total_score DESC, updated_at ASC
        """,
        (current_bot_id,),
    ).fetchall()


def write_admin_score_report(path: Path, rows: list[sqlite3.Row]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as report_file:
        writer = csv.writer(report_file)
        writer.writerow(["user_id", "display_name", "total_score", "updated_at"])
        for row in rows:
            writer.writerow([row["user_id"], row["display_name"], row["total_score"], row["updated_at"]])


async def current_guild_member(guild: discord.Guild, user_id: str) -> discord.Member | None:
    try:
        return await guild.fetch_member(int(user_id))
    except discord.NotFound:
        return None


def operator_role_ids() -> set[int]:
    role_ids: set[int] = set()
    for value in os.getenv("OPERATOR_ROLE_IDS", "").replace(";", ",").split(","):
        value = value.strip()
        if value.isdigit():
            role_ids.add(int(value))
    return role_ids


def is_operator_user(user: discord.abc.User) -> bool:
    permissions = getattr(user, "guild_permissions", None)
    if permissions and (permissions.administrator or permissions.manage_guild):
        return True

    allowed_role_ids = operator_role_ids()
    user_roles = getattr(user, "roles", [])
    return bool(allowed_role_ids and any(role.id in allowed_role_ids for role in user_roles))


def admin_only() -> app_commands.check:
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_operator_user(interaction.user):
            return True

        raise app_commands.CheckFailure("이 명령어는 관리자 권한 또는 운영자 역할이 필요합니다.")

    return app_commands.check(predicate)


def anonymous_discussion_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="익명 발언하기",
            emoji="🕶️",
            style=discord.ButtonStyle.secondary,
            custom_id=f"eventbot:anonymous:{round_id}",
        )
    )
    return view


def thread_discussion_view(round_id: int, game_key: str) -> discord.ui.View:
    """토론방 맨 아래에 붙는 버튼 묶음: 선택지 + 익명 발언하기.

    토론 스레드는 라운드 메시지에서 파생시키므로 그 메시지가 스레드의 시작 메시지가 되는데,
    시작 메시지의 버튼은 스레드 안에서 누를 수 없다(실체가 부모 채널에 있는 메시지라 회색으로 뜬다).
    그래서 같은 custom_id의 선택 버튼을 스레드 안에도 둔다. votes는 (round_id, user_id) upsert라
    채널에서 누르든 스레드에서 누르든 마지막 선택 하나만 남는다.
    """
    view = discord.ui.View(timeout=None)
    if game_key in {"balance", "minority_balance"}:
        vote_buttons = vote_view(round_id, BALANCE_VOTE_CHOICES)
    elif game_key == "unique_number":
        vote_buttons = unique_number_vote_view(round_id)
    else:
        vote_buttons = defuse_vote_view(round_id)
    for button in [*vote_buttons.children, *anonymous_discussion_view(round_id).children]:
        view.add_item(button)
    return view


def count_discussion_messages(conn: sqlite3.Connection, round_id: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count FROM discussion_messages
        WHERE round_id = ? AND message_id != 'pending'
        """,
        (round_id,),
    ).fetchone()
    return int(row["count"]) if row else 0


def build_anonymous_guide_embed(
    post_count: int,
    close_at: float | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title="🕶️ 익명 토론방",
        description=ANONYMOUS_GUIDE_TEXT,
        color=discord.Color.dark_grey(),
    )
    embed.add_field(
        name="🗳️ 투표",
        value="아래 선택 버튼으로 이 방에서 바로 투표할 수 있습니다. 마감 전까지 몇 번이든 바꿀 수 있습니다.",
        inline=False,
    )
    embed.add_field(name="💬 누적 발언", value=f"**{post_count}개**", inline=False)
    if close_at:
        embed.add_field(
            name="⏳ 마감 시각",
            value=f"**{close_time_text(close_at)}**",
            inline=False,
        )
    return embed


def build_event_open_embed() -> discord.Embed:
    embed = discord.Embed(
        title="오늘의 이벤트가 열렸습니다!",
        description="아래 `참여하기` 버튼을 누른 유저만 이번 이벤트의 모든 게임에 참여할 수 있습니다.",
        color=discord.Color.green(),
    )
    embed.add_field(
        name="🗺️ 진행 흐름",
        value=(
            "> **1** ─ 지금 `참여하기` 버튼으로 오늘의 참가 등록\n"
            "> **2** ─ 메인게임 1개 진행 (딜레마의 섬 또는 모래성 게임)\n"
            "> **3** ─ 메인게임 결과와 점수 정산\n"
            "> **4** ─ 미니게임 1개 진행 후 결과와 점수 정산"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚠️ 꼭 확인해주세요",
        value=(
            "게임이 시작되면 참가 등록이 마감됩니다. 이전 이벤트의 참가 기록은 이어지지 않으며, "
            "이벤트가 다시 열릴 때마다 새로 `참여하기`를 눌러야 합니다."
        ),
        inline=False,
    )
    return embed


def event_join_view(event_session_id: int, participant_count: int = 0) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label=f"참여하기 ({participant_count}명)",
            emoji="🙋",
            style=discord.ButtonStyle.success,
            custom_id=f"eventbot:eventjoin:{event_session_id}",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="참여 취소하기",
            emoji="↩️",
            style=discord.ButtonStyle.danger,
            custom_id=f"eventbot:eventcancel:{event_session_id}",
        )
    )
    return view


def planned_mini_game_view(main_round_id: int) -> discord.ui.View | None:
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND status = 'closed'",
            (main_round_id,),
        ).fetchone()
    if not round_row or round_row["type"] not in MAIN_GAME_TYPES:
        return None

    extra = round_extra(round_row)
    phase = main_game_phase(round_row)
    mini_game = str(extra.get("planned_mini_game") or "")
    if phase != "scored" or mini_game not in MINI_GAME_TYPES:
        return None

    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label=f"{GAME_NAMES[mini_game]} 설정 후 시작",
            style=discord.ButtonStyle.primary,
            custom_id=f"eventbot:startmini:{main_round_id}",
        )
    )
    return view


async def handle_planned_mini_game(interaction: discord.Interaction, main_round_id: int) -> None:
    if not is_operator_user(interaction.user):
        await interaction.response.send_message(
            "이 버튼은 관리자 권한 또는 운영자 역할이 필요합니다.",
            ephemeral=True,
        )
        return

    with db() as conn:
        main_round = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND status = 'closed'",
            (main_round_id,),
        ).fetchone()
        if not main_round or main_round["type"] not in MAIN_GAME_TYPES:
            await interaction.response.send_message("완료된 메인게임을 찾을 수 없습니다.", ephemeral=True)
            return
        event_row = conn.execute(
            "SELECT status FROM event_sessions WHERE id = ?",
            (main_round["event_session_id"],),
        ).fetchone()
        if not event_row or event_row["status"] == "closed":
            await interaction.response.send_message("이미 종료된 이벤트입니다.", ephemeral=True)
            return

        extra = round_extra(main_round)
        phase = main_game_phase(main_round)
        mini_game = str(extra.get("planned_mini_game") or "")
        if phase != "scored" or mini_game not in MINI_GAME_TYPES:
            await interaction.response.send_message("시작할 미니게임이 지정되어 있지 않습니다.", ephemeral=True)
            return
        if get_open_round(conn):
            await interaction.response.send_message(
                "이미 열린 라운드가 있습니다. 먼저 `/라운드마감`을 해주세요.",
                ephemeral=True,
            )
            return
        latest_round = conn.execute(
            """
            SELECT id FROM rounds
            WHERE event_session_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (main_round["event_session_id"],),
        ).fetchone()
        if not latest_round or int(latest_round["id"]) != main_round_id:
            await interaction.response.send_message("이 이벤트의 미니게임은 이미 시작되었습니다.", ephemeral=True)
            return

    await interaction.response.send_modal(StartSetupModal(mini_game))


async def handle_event_join(interaction: discord.Interaction, event_session_id: int) -> None:
    if interaction.user.bot:
        await interaction.response.send_message("봇 계정은 이벤트에 참여할 수 없습니다.", ephemeral=True)
        return

    with db() as conn:
        event_row = conn.execute(
            "SELECT status FROM event_sessions WHERE id = ?",
            (event_session_id,),
        ).fetchone()
        if not event_row or event_row["status"] != "recruiting":
            await interaction.response.send_message(
                "이 이벤트의 참가 등록은 이미 마감되었습니다. 다음 오픈을 기다려주세요.",
                ephemeral=True,
            )
            return

        ensure_user(conn, interaction.user)
        existing = conn.execute(
            """
            SELECT 1 FROM event_participants
            WHERE event_session_id = ? AND user_id = ?
            """,
            (event_session_id, str(interaction.user.id)),
        ).fetchone()
        if existing:
            await interaction.response.send_message(
                "이미 이번 이벤트에 참여 등록되어 있습니다.",
                ephemeral=True,
            )
            return

        conn.execute(
            """
            INSERT INTO event_participants (event_session_id, user_id, joined_at)
            VALUES (?, ?, ?)
            """,
            (event_session_id, str(interaction.user.id), now_text()),
        )
        participant_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS count FROM event_participants
                WHERE event_session_id = ?
                """,
                (event_session_id,),
            ).fetchone()["count"]
        )
        log_activity(
            conn,
            "participation",
            "join",
            event_session_id=event_session_id,
            actor_user_id=str(interaction.user.id),
            payload={
                "display_name": user_display_name(interaction.user),
                "participant_count": participant_count,
            },
        )

    await interaction.response.edit_message(
        view=event_join_view(event_session_id, participant_count),
    )
    await interaction.followup.send(
        "참여 등록 완료! 이번 이벤트의 메인게임과 미니게임에 참여할 수 있습니다.",
        ephemeral=True,
    )


async def handle_event_cancel(interaction: discord.Interaction, event_session_id: int) -> None:
    if interaction.user.bot:
        await interaction.response.send_message("봇 계정은 이벤트에 참여할 수 없습니다.", ephemeral=True)
        return

    with db() as conn:
        event_row = conn.execute(
            "SELECT status FROM event_sessions WHERE id = ?",
            (event_session_id,),
        ).fetchone()
        if not event_row or event_row["status"] != "recruiting":
            await interaction.response.send_message(
                "이 이벤트의 참가 등록은 이미 마감되어 참여를 취소할 수 없습니다.",
                ephemeral=True,
            )
            return

        cursor = conn.execute(
            """
            DELETE FROM event_participants
            WHERE event_session_id = ? AND user_id = ?
            """,
            (event_session_id, str(interaction.user.id)),
        )
        if cursor.rowcount == 0:
            await interaction.response.send_message(
                "이번 이벤트에 참여 등록되어 있지 않습니다.",
                ephemeral=True,
            )
            return

        participant_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS count FROM event_participants
                WHERE event_session_id = ?
                """,
                (event_session_id,),
            ).fetchone()["count"]
        )
        # DELETE로 행이 사라지므로 취소했다는 사실은 여기서만 남는다.
        log_activity(
            conn,
            "participation",
            "cancel",
            event_session_id=event_session_id,
            actor_user_id=str(interaction.user.id),
            payload={
                "display_name": user_display_name(interaction.user),
                "participant_count": participant_count,
            },
        )

    await interaction.response.edit_message(
        view=event_join_view(event_session_id, participant_count),
    )
    await interaction.followup.send("참여가 취소되었습니다.", ephemeral=True)


class AnonymousDiscussionModal(discord.ui.Modal):
    def __init__(self, round_id: int) -> None:
        super().__init__(title="익명 발언하기")
        self.round_id = round_id
        self.content_input = discord.ui.TextInput(
            label="발언 내용",
            placeholder="다른 참가자에게는 작성자가 표시되지 않습니다.",
            style=discord.TextStyle.paragraph,
            max_length=1000,
        )
        self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # 발언 게시 -> 옛 버튼 삭제 -> 버튼 재게시까지 API 호출이 여러 번이라 모달의 3초 응답
        # 제한을 넘긴다. 먼저 defer하고, 성공하면 그 ephemeral을 지워서 흔적을 남기지 않는다.
        await interaction.response.defer(ephemeral=True)

        if not isinstance(interaction.channel, discord.Thread):
            await interaction.followup.send("토론 스레드에서만 발언할 수 있습니다.", ephemeral=True)
            return

        content = self.content_input.value.strip()
        if not content:
            await interaction.followup.send("발언 내용을 입력해주세요.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        with db() as conn:
            round_row = conn.execute(
                """
                SELECT * FROM rounds
                WHERE id = ? AND status = 'open' AND discussion_thread_id = ?
                """,
                (self.round_id, str(interaction.channel.id)),
            ).fetchone()
            if not round_row:
                await interaction.followup.send("이미 마감되었거나 유효하지 않은 토론방입니다.", ephemeral=True)
                return
            if not can_join_round(conn, self.round_id, user_id):
                await interaction.followup.send(
                    "이번 이벤트에서 `참여하기`를 누른 유저만 토론에 참여할 수 있습니다.",
                    ephemeral=True,
                )
                return

            ensure_user(conn, interaction.user)
            anon_number = assign_anonymous_number(conn, self.round_id, user_id)
            # 게시 전에 행을 잡아둔다. 더블클릭해도 번호가 두 개로 갈리지 않는다.
            cursor = conn.execute(
                """
                INSERT INTO discussion_messages
                    (round_id, user_id, anon_number, message_id, content, created_at)
                VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (self.round_id, user_id, anon_number, content, now_text()),
            )
            post_id = int(cursor.lastrowid)
            game_key = round_game_key(round_row)

        try:
            posted_message = await interaction.channel.send(
                embed=build_anonymous_message_embed(anon_number, content),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            with db() as conn:
                conn.execute("DELETE FROM discussion_messages WHERE id = ?", (post_id,))
            await interaction.followup.send("익명 발언을 올리지 못했습니다. 다시 시도해주세요.", ephemeral=True)
            return

        with db() as conn:
            conn.execute(
                "UPDATE discussion_messages SET message_id = ? WHERE id = ?",
                (str(posted_message.id), post_id),
            )
            # 내용은 discussion_messages(append-only)에 이미 온전히 있다. 여기서는 중복 저장하지
            # 않고 참조만 남겨, 하나의 타임라인에서 발언 시점을 다른 활동과 함께 볼 수 있게 한다.
            log_activity(
                conn,
                "thread",
                "anon_post",
                event_session_id=round_row["event_session_id"],
                round_id=self.round_id,
                actor_user_id=user_id,
                payload={
                    "discussion_message_id": post_id,
                    "anon_number": anon_number,
                    "content_length": len(content),
                    "thread_id": str(interaction.channel.id),
                },
            )

        # 버튼이 발언에 밀려 위로 올라가지 않도록, 옛 버튼을 지우고 맨 아래에 다시 붙인다.
        # 여러 명이 동시에 발언하면 각자 옛 스냅샷에서 버튼 id를 읽어 버튼판이 여러 개로 갈린다.
        # 그래서 스레드별 락을 잡고, 락 안에서 현재 버튼 id를 DB에서 다시 읽는다. (모래성과 같은 방식)
        async with bot.anon_board_lock(interaction.channel.id):
            with db() as conn:
                fresh_row = conn.execute(
                    "SELECT extra_json FROM rounds WHERE id = ?", (self.round_id,)
                ).fetchone()
                fresh_extra = round_extra(fresh_row) if fresh_row else {}
                current_button_id = str(fresh_extra.get("anon_button_message_id") or "")
                current_auto_close_at = fresh_extra.get("auto_close_at")
                post_count = count_discussion_messages(conn, self.round_id)

            if current_button_id:
                try:
                    old_button = await interaction.channel.fetch_message(int(current_button_id))
                    await old_button.delete()
                except (discord.NotFound, discord.HTTPException, ValueError):
                    pass
            try:
                new_button = await interaction.channel.send(
                    embed=build_anonymous_guide_embed(post_count, current_auto_close_at),
                    view=thread_discussion_view(self.round_id, game_key),
                )
            except discord.HTTPException:
                # 버튼 재게시에 실패해도 발언 자체는 이미 올라갔다. 발언을 되돌리지는 않는다.
                new_button = None

            if new_button:
                with db() as conn:
                    fresh_row = conn.execute(
                        "SELECT extra_json FROM rounds WHERE id = ?", (self.round_id,)
                    ).fetchone()
                    if fresh_row:
                        extra = round_extra(fresh_row)
                        extra["anon_button_message_id"] = str(new_button.id)
                        conn.execute(
                            "UPDATE rounds SET extra_json = ? WHERE id = ?",
                            (json.dumps(extra, ensure_ascii=False), self.round_id),
                        )

        # 발언자를 드러내지 않도록 확인 문구를 남기지 않고 defer한 ephemeral만 조용히 지운다.
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass


def start_trust_decision(round_id: int) -> dict[str, Any] | None:
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'trust' AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row:
            return None

        extra = round_extra(round_row)
        if extra.get("trust_phase") != "registration":
            return None

        registrations = conn.execute(
            "SELECT user_id FROM trust_registrations WHERE round_id = ? ORDER BY registered_at ASC",
            (round_id,),
        ).fetchall()
        user_ids = [str(row["user_id"]) for row in registrations]
        if len(user_ids) < 2:
            extra.update({"trust_phase": "cancelled", "cancelled_reason": "not_enough_participants"})
            conn.execute(
                "UPDATE rounds SET status = 'closed', closed_at = ?, extra_json = ? WHERE id = ?",
                (now_text(), json.dumps(extra, ensure_ascii=False), round_id),
            )
            embed = discord.Embed(
                title=f"{round_row['title']} · 취소",
                description="참여자가 2명 미만이라 오늘의 메인 콘텐츠를 시작하지 못했습니다.",
                color=discord.Color.light_grey(),
            )
            return {
                "state": "cancelled",
                "channel_id": str(round_row["channel_id"]),
                "message_id": str(round_row["message_id"] or ""),
                "embed": embed,
            }

        recent_pairs = recent_match_pairs(conn, round_id, "trust")
        pairs, unmatched_user_id = build_random_trust_pairs(user_ids, recent_pairs)
        for user_a_id, user_b_id in pairs:
            conn.execute(
                """
                INSERT INTO trust_pairings (round_id, user_a_id, user_b_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (round_id, user_a_id, user_b_id, now_text()),
            )
            log_activity(
                conn,
                "match",
                "pair_matched",
                event_session_id=round_row["event_session_id"],
                round_id=round_id,
                actor_user_id=user_a_id,
                target_user_id=user_b_id,
                payload={"game_key": "trust"},
            )
        log_activity(
            conn,
            "match",
            "pairing_complete",
            event_session_id=round_row["event_session_id"],
            round_id=round_id,
            payload={
                "game_key": "trust",
                "pair_count": len(pairs),
                "registered_count": len(user_ids),
                "unmatched_user_id": unmatched_user_id,
                "avoided_recent_pairs": len(recent_pairs),
            },
        )

        decision_close_at = time.time() + TRUST_DECISION_SECONDS
        extra.update(
            {
                "trust_phase": "decision",
                "unmatched_user_id": unmatched_user_id,
                "decision_close_at": decision_close_at,
                "auto_close_at": decision_close_at,
                "decision_duration_seconds": TRUST_DECISION_SECONDS,
            }
        )
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )

        embed = build_trust_decision_embed(
            round_id,
            str(round_row["title"]),
            pairs,
            unmatched_user_id,
            close_at=decision_close_at,
        )
        return {
            "state": "decision",
            "channel_id": str(round_row["channel_id"]),
            "message_id": str(round_row["message_id"] or ""),
            "round_id": round_id,
            "title": str(round_row["title"]),
            "pairs": pairs,
            "embed": embed,
            "view": emergency_join_view(round_id) if unmatched_user_id else None,
            "close_at": decision_close_at,
        }


async def update_trust_main_message(transition: dict[str, Any]) -> None:
    try:
        channel = bot.get_channel(int(transition["channel_id"]))
        if channel is None:
            channel = await bot.fetch_channel(int(transition["channel_id"]))
        message_id = transition.get("message_id")
        if message_id:
            message = await channel.fetch_message(int(message_id))
            await message.edit(embed=transition["embed"], view=transition.get("view"))
        elif hasattr(channel, "send"):
            await channel.send(embed=transition["embed"], view=transition.get("view"))
    except discord.HTTPException:
        return


async def archive_round_discussion_thread(
    round_row: sqlite3.Row,
    *,
    reason: str,
) -> None:
    """일반 토론 스레드를 삭제하지 않고 읽기 전용 보관 상태로 남긴다."""
    thread_id = str(round_row["discussion_thread_id"] or "")
    if not thread_id:
        return
    try:
        thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
        if isinstance(thread, discord.Thread):
            await thread.edit(archived=True, locked=True, reason=reason)
    except (discord.HTTPException, ValueError):
        pass


def trust_pair_intro(
    user_a_id: str,
    user_b_id: str,
    close_at: float | None = None,
) -> str:
    intro = (
        f"<@{user_a_id}>님과 <@{user_b_id}>님의 **비공개 협상방**입니다.\n"
        "> 서로 마음껏 대화한 뒤 아래 버튼으로 `신뢰` 또는 `배신`을 선택하세요. (마감 전까지 몇 번이든 바꿀 수 있습니다)\n"
        "> 선택은 마감 전까지 상대에게 공개되지 않으며, 마감 후 이 방에서 함께 공개됩니다.\n"
        "> \\*마감까지 고르지 않으면 `신뢰`/`배신` 중 하나가 랜덤으로 배정됩니다."
    )
    if close_at:
        intro += f"\n\n⏳ **선택 마감:** {close_time_text(close_at)}"
    return intro


async def create_trust_pair_threads(transition: dict[str, Any]) -> None:
    if transition.get("state") != "decision":
        return

    channel = await runtime.resolve_text_channel(str(transition["channel_id"]))
    if channel is None:
        return

    round_id = int(transition["round_id"])
    title = str(transition["title"])
    failed_pairs = 0
    for pair_number, pair in enumerate(transition.get("pairs", []), start=1):
        user_a_id, user_b_id = (str(pair[0]), str(pair[1]))
        created = await create_pair_thread(
            channel,
            f"{title} · {pair_number}세트",
            [user_a_id, user_b_id],
            f"딜레마의 섬 #{round_id} 세트별 비공개 협상방",
            trust_decision_view(round_id),
            content=trust_pair_intro(user_a_id, user_b_id, transition.get("close_at")),
        )
        if created is None:
            failed_pairs += 1
            continue
        thread, _ = created
        save_pair_thread_id(round_id, user_a_id, user_b_id, str(thread.id))

    await warn_failed_pair_threads(channel, round_id, failed_pairs)


def build_trust_pair_result_embed(
    title: str,
    user_a_id: str,
    user_b_id: str,
    user_a_choice: str | None,
    user_b_choice: str | None,
) -> discord.Embed:
    choice_labels = {"trust": "신뢰", "betray": "배신"}
    if not user_a_choice or not user_b_choice:
        outcome = "미선택 포함 · 점수 변동 없음"
    elif user_a_choice == "trust" and user_b_choice == "trust":
        outcome = "상호 신뢰 · 두 사람 모두 +2점"
    elif user_a_choice == "betray" and user_b_choice == "betray":
        outcome = "동시 배신 · 두 사람 모두 -3점"
    else:
        betrayer_id = user_a_id if user_a_choice == "betray" else user_b_id
        outcome = f"단독 배신 · <@{betrayer_id}> +5점, 상대 0점"

    embed = discord.Embed(
        title=f"세트 결과 · {title}",
        description="두 사람의 선택이 공개되었습니다.",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="🃏 서로의 선택",
        value=(
            f"<@{user_a_id}>: **{choice_labels.get(user_a_choice, '미선택')}**\n"
            f"<@{user_b_id}>: **{choice_labels.get(user_b_choice, '미선택')}**"
        ),
        inline=False,
    )
    embed.add_field(name="💰 세트 정산", value=outcome, inline=False)
    return embed


async def publish_trust_pair_results(round_id: int) -> None:
    with db() as conn:
        round_row = conn.execute(
            "SELECT title FROM rounds WHERE id = ? AND type = 'trust'",
            (round_id,),
        ).fetchone()
        if not round_row:
            return
        pairs = conn.execute(
            """
            SELECT user_a_id, user_b_id, discussion_thread_id
            FROM trust_pairings
            WHERE round_id = ? AND discussion_thread_id IS NOT NULL
            ORDER BY created_at ASC
            """,
            (round_id,),
        ).fetchall()
        votes = conn.execute(
            "SELECT user_id, choice FROM votes WHERE round_id = ?",
            (round_id,),
        ).fetchall()

    vote_choices = {str(row["user_id"]): str(row["choice"]) for row in votes}
    for pair in pairs:
        thread_id = int(pair["discussion_thread_id"])
        user_a_id = str(pair["user_a_id"])
        user_b_id = str(pair["user_b_id"])
        try:
            thread = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
            if not isinstance(thread, discord.Thread):
                continue
            if thread.archived:
                await thread.edit(archived=False, reason="딜레마의 섬 세트 결과 공개")
            await thread.send(
                embed=build_trust_pair_result_embed(
                    str(round_row["title"]),
                    user_a_id,
                    user_b_id,
                    vote_choices.get(user_a_id),
                    vote_choices.get(user_b_id),
                )
            )
            await thread.edit(
                archived=True,
                locked=True,
                reason="딜레마의 섬 세트 결과 공개 완료",
            )
        except (discord.Forbidden, discord.HTTPException, ValueError):
            continue


async def handle_trust_join(interaction: discord.Interaction, round_id: int) -> None:
    if interaction.user.bot:
        await interaction.response.send_message("봇 계정은 참여 체크할 수 없습니다.", ephemeral=True)
        return

    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'trust' AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row or round_extra(round_row).get("trust_phase") != "registration":
            await interaction.response.send_message("참여자 모집이 이미 끝났습니다.", ephemeral=True)
            return
        if not can_join_round(conn, round_id, str(interaction.user.id)):
            await interaction.response.send_message(
                "이벤트 오픈 때 `참여하기`를 누른 유저만 게임에 참여할 수 있습니다.",
                ephemeral=True,
            )
            return

        existing = conn.execute(
            "SELECT 1 FROM trust_registrations WHERE round_id = ? AND user_id = ?",
            (round_id, str(interaction.user.id)),
        ).fetchone()
        if existing:
            await interaction.response.send_message("이미 오늘의 메인 게임에 참여 등록했습니다.", ephemeral=True)
            return

        ensure_user(conn, interaction.user)
        conn.execute(
            "INSERT INTO trust_registrations (round_id, user_id, registered_at) VALUES (?, ?, ?)",
            (round_id, str(interaction.user.id), now_text()),
        )
    await interaction.response.send_message("참여 체크 완료! 모집 마감 후 무작위로 2명씩 매칭됩니다.", ephemeral=True)


async def handle_trust_decision(
    interaction: discord.Interaction,
    round_id: int,
    choice: str,
) -> None:
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'trust' AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row or round_extra(round_row).get("trust_phase") != "decision":
            await interaction.response.send_message("현재는 선택할 수 있는 시간이 아닙니다.", ephemeral=True)
            return

        pairing = conn.execute(
            """
            SELECT 1 FROM trust_pairings
            WHERE round_id = ?
              AND (user_a_id = ? OR user_b_id = ?)
              AND discussion_thread_id = ?
            """,
            (
                round_id,
                str(interaction.user.id),
                str(interaction.user.id),
                str(interaction.channel_id),
            ),
        ).fetchone()
        if not pairing:
            await interaction.response.send_message(
                "자신에게 배정된 비공개 협상방에서만 선택할 수 있습니다.",
                ephemeral=True,
            )
            return

        ensure_user(conn, interaction.user)
        ts = now_text()
        # votes는 choice를 덮어쓰므로 바꾸기 전 값을 여기서 잡아둬야 이력이 남는다.
        previous = conn.execute(
            "SELECT choice FROM votes WHERE round_id = ? AND user_id = ?",
            (round_id, str(interaction.user.id)),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(round_id, user_id) DO UPDATE SET
                choice = excluded.choice,
                updated_at = excluded.updated_at
            """,
            (round_id, str(interaction.user.id), choice, ts, ts),
        )
        log_activity(
            conn,
            "vote",
            "trust_decision",
            round_id=round_id,
            actor_user_id=str(interaction.user.id),
            payload={
                "choice": choice,
                "previous_choice": previous["choice"] if previous else None,
                "changed": bool(previous and previous["choice"] != choice),
                "thread_id": str(interaction.channel_id),
            },
        )
    await interaction.response.send_message(
        "선택을 저장했습니다. 마감 전까지 다시 누르면 선택이 바뀝니다.",
        ephemeral=True,
    )


def start_sand_game(round_id: int) -> dict[str, Any] | None:
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'sand' AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row:
            return None

        extra = round_extra(round_row)
        if extra.get("sand_phase") != "registration":
            return None

        registrations = conn.execute(
            "SELECT user_id FROM trust_registrations WHERE round_id = ? ORDER BY registered_at ASC",
            (round_id,),
        ).fetchall()
        user_ids = [str(row["user_id"]) for row in registrations]
        if len(user_ids) < 2:
            extra.update({"sand_phase": "cancelled", "cancelled_reason": "not_enough_participants"})
            conn.execute(
                "UPDATE rounds SET status = 'closed', closed_at = ?, extra_json = ? WHERE id = ?",
                (now_text(), json.dumps(extra, ensure_ascii=False), round_id),
            )
            embed = discord.Embed(
                title=f"{round_row['title']} · 취소",
                description="참여자가 2명 미만이라 모래성 게임을 시작하지 못했습니다.",
                color=discord.Color.light_grey(),
            )
            return {
                "state": "cancelled",
                "channel_id": str(round_row["channel_id"]),
                "message_id": str(round_row["message_id"] or ""),
                "embed": embed,
            }

        recent_pairs = recent_match_pairs(conn, round_id, "sand")
        pairs, unmatched_user_id = build_random_trust_pairs(user_ids, recent_pairs)
        sand_pairs: list[dict[str, Any]] = []
        for user_a_id, user_b_id in pairs:
            conn.execute(
                """
                INSERT INTO trust_pairings (round_id, user_a_id, user_b_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (round_id, user_a_id, user_b_id, now_text()),
            )
            log_activity(
                conn,
                "match",
                "pair_matched",
                event_session_id=round_row["event_session_id"],
                round_id=round_id,
                actor_user_id=user_a_id,
                target_user_id=user_b_id,
                payload={"game_key": "sand"},
            )
            sand_pairs.append(
                {
                    "user_a_id": user_a_id,
                    "user_b_id": user_b_id,
                    "number_a": None,
                    "number_b": None,
                    "phase": "number_selection",
                    "current_user_id": random.choice([user_a_id, user_b_id]),
                    "successful_removals": 0,
                }
            )
        log_activity(
            conn,
            "match",
            "pairing_complete",
            event_session_id=round_row["event_session_id"],
            round_id=round_id,
            payload={
                "game_key": "sand",
                "pair_count": len(pairs),
                "registered_count": len(user_ids),
                "unmatched_user_id": unmatched_user_id,
            },
        )

        play_close_at = time.time() + SAND_PLAY_SECONDS
        extra.update(
            {
                "sand_phase": "playing",
                "sand_pairs": sand_pairs,
                "unmatched_user_id": unmatched_user_id,
                "play_close_at": play_close_at,
                "auto_close_at": play_close_at,
                "play_duration_seconds": SAND_PLAY_SECONDS,
            }
        )
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )
        updated_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        return {
            "state": "playing",
            "channel_id": str(round_row["channel_id"]),
            "message_id": str(round_row["message_id"] or ""),
            "round_id": round_id,
            "title": str(round_row["title"]),
            "pairs": pairs,
            "embed": build_sand_play_embed(updated_row, extra),
            # 숫자·행동 버튼은 짝별 비공개 방 안에만 둔다. 메인 메시지는 진행판 역할만 한다.
            "view": emergency_join_view(round_id) if unmatched_user_id else None,
            "close_at": play_close_at,
        }


def sand_pair_intro(user_a_id: str, user_b_id: str) -> str:
    return (
        f"<@{user_a_id}>님과 <@{user_b_id}>님의 **1:1 모래성 방**입니다.\n"
        "> **1** ─ 먼저 각자 1~10 중 하나를 고르세요. (번복 불가, 마감까지 상대에게 공개되지 않습니다)\n"
        "> **2** ─ 두 사람이 모두 고르면 번갈아가며 `포기하기` 또는 `모래 덜기`를 선택합니다."
    )


def sand_pair_settlement_text(pair: dict[str, Any]) -> str:
    """짝 하나의 정산 문구. 라운드 결과 요약과 방별 결과 공개가 같은 문구를 쓰도록 한 곳에 모은다."""
    outcome = str(pair.get("outcome") or "")
    result = pair.get("result", {})
    if outcome == "mutual_quit":
        return "각 -3점"
    if outcome == "quit_success":
        return f"<@{result['winner']}> +5점 · <@{result['quitter']}> -3점"
    if outcome == "quit_collapse":
        return f"<@{result['winner']}> +2점 · <@{result['collapsed_user']}> -3점"
    if outcome == "plain_collapse":
        return f"<@{result['collapsed_user']}> -3점"
    return "점수 변동 없음"


async def create_sand_pair_threads(transition: dict[str, Any]) -> None:
    """짝마다 비공개 방을 열고 그 안에 숫자·행동 버튼을 둔다. (딜레마의 섬과 같은 방식)"""
    if transition.get("state") != "playing":
        return

    channel = await runtime.resolve_text_channel(str(transition["channel_id"]))
    if channel is None:
        return

    round_id = int(transition["round_id"])
    title = str(transition["title"])

    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
    if not round_row:
        return
    extra = round_extra(round_row)

    created_threads: dict[tuple[str, str], tuple[str, str]] = {}
    failed_pairs = 0
    for pair_number, pair in enumerate(extra.get("sand_pairs", []), start=1):
        user_a_id = str(pair["user_a_id"])
        user_b_id = str(pair["user_b_id"])
        created = await create_pair_thread(
            channel,
            f"{title} · {pair_number}번 모래성",
            [user_a_id, user_b_id],
            f"모래성 게임 #{round_id} 1:1 비공개 방",
            sand_play_view(round_id),
            content=sand_pair_intro(user_a_id, user_b_id),
            embed=build_sand_pair_embed(title, pair, extra),
        )
        if created is None:
            failed_pairs += 1
            continue
        thread, message = created
        save_pair_thread_id(round_id, user_a_id, user_b_id, str(thread.id))
        created_threads[(user_a_id, user_b_id)] = (str(thread.id), str(message.id))

    if created_threads:
        store_sand_thread_ids(round_id, created_threads)
    await warn_failed_pair_threads(channel, round_id, failed_pairs)


def store_sand_thread_ids(
    round_id: int,
    created_threads: dict[tuple[str, str], tuple[str, str]],
) -> None:
    """스레드/메시지 id를 sand_pairs에 심는다. 재시작 후에도 방별 진행판을 갱신할 수 있어야 한다."""
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row:
            return
        extra = round_extra(round_row)
        for pair in extra.get("sand_pairs", []):
            key = (str(pair["user_a_id"]), str(pair["user_b_id"]))
            if key in created_threads:
                pair["thread_id"], pair["play_message_id"] = created_threads[key]
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )


async def publish_sand_pair_results(round_id: int) -> None:
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'sand'",
            (round_id,),
        ).fetchone()
    if not round_row:
        return

    title = str(round_row["title"])
    extra = round_extra(round_row)
    for pair in extra.get("sand_pairs", []):
        thread_id = str(pair.get("thread_id") or "")
        if not thread_id:
            continue
        try:
            bot.sand_board_locks.pop(int(thread_id), None)
            thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
            if not isinstance(thread, discord.Thread):
                continue
            if thread.archived:
                await thread.edit(archived=False, reason="모래성 게임 결과 공개")
            await thread.send(embed=build_sand_pair_result_embed(title, pair))
            await thread.edit(
                archived=True,
                locked=True,
                reason="모래성 게임 결과 공개 완료",
            )
        except (discord.Forbidden, discord.HTTPException, ValueError):
            continue


def apply_emergency_join(
    extra: dict[str, Any],
    game_key: str,
    new_user_id: str,
) -> dict[str, Any] | None:
    """긴급 투입을 extra에 반영한다. 짝 없는 대기자가 없으면 None.

    `game_key`는 rounds.type이 아니라 round_game_key() 값이다 — 가라앉는 배는
    type='sand'로 저장되므로 타입으로는 모래성과 구분되지 않는다.

    DB도 디스코드도 건드리지 않는 순수 함수라 테스트에서 그대로 쓴다.
    호출 전에 라운드 상태·중복 참여 검증이 끝나 있어야 한다.
    """
    unmatched_user_id = str(extra.get("unmatched_user_id") or "")
    if not unmatched_user_id or unmatched_user_id == new_user_id:
        return None

    if game_key == "bail":
        # 가라앉는 배는 짝 구조가 다르다. 늦게 붙은 짝도 협상부터 시작하되,
        # 이미 실행 단계로 넘어간 뒤라면 협상 기회가 없으므로 합의 없이 제출만 한다.
        pair = bail_new_pair(unmatched_user_id, new_user_id)
        if extra.get("bail_phase") == "execution":
            pair.update({"phase": "submitting", "offer": None})
        extra.setdefault("bail_pairs", []).append(pair)
    else:
        pair = {
            "user_a_id": unmatched_user_id,
            "user_b_id": new_user_id,
            "number_a": None,
            "number_b": None,
            "phase": "number_selection",
            "current_user_id": random.choice([unmatched_user_id, new_user_id]),
            "successful_removals": 0,
        }
        if game_key == "sand":
            extra.setdefault("sand_pairs", []).append(pair)

    extra["unmatched_user_id"] = None
    extra["emergency_user_id"] = new_user_id

    # 늦게 합류한 짝도 게임을 끝낼 시간이 필요하다. 전체 마감을 1분 미룬다.
    close_at = float(extra.get("auto_close_at") or time.time()) + EMERGENCY_EXTENSION_SECONDS
    extra["auto_close_at"] = close_at
    if game_key == "trust":
        extra["decision_close_at"] = close_at
    elif game_key == "bail":
        phase_key = (
            "execution_close_at"
            if extra.get("bail_phase") == "execution"
            else "negotiation_close_at"
        )
        extra[phase_key] = close_at
    else:
        extra["play_close_at"] = close_at

    return {
        "user_a_id": unmatched_user_id,
        "user_b_id": new_user_id,
        "pair": pair,
        "close_at": close_at,
    }


def emergency_thread_notice(close_at: float) -> str:
    """비공개 방에 나가는 공지. 투입자가 누구인지는 절대 넣지 않는다.

    방마다 상대가 누구인지가 게임의 일부다. 투입자 이름은 메인 채널의 진행판에서만 공개한다.
    """
    return (
        "🚨 새 참가자가 긴급 투입되어 전체 진행 시간이 1분 연장됩니다.\n"
        f"새 마감: {close_time_text(close_at)}"
    )


async def announce_emergency_join(round_id: int, close_at: float) -> None:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT discussion_thread_id FROM trust_pairings
            WHERE round_id = ? AND discussion_thread_id IS NOT NULL
            """,
            (round_id,),
        ).fetchall()

    notice = emergency_thread_notice(close_at)
    for row in rows:
        thread_id = str(row["discussion_thread_id"])
        try:
            thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
            if isinstance(thread, discord.Thread):
                await thread.send(notice)
        except (discord.HTTPException, ValueError):
            continue


async def refresh_main_trust_board(round_id: int) -> None:
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row or not round_row["message_id"]:
            return
        pair_rows = conn.execute(
            """
            SELECT user_a_id, user_b_id FROM trust_pairings
            WHERE round_id = ? ORDER BY created_at ASC
            """,
            (round_id,),
        ).fetchall()

    extra = round_extra(round_row)
    channel = await runtime.resolve_text_channel(str(round_row["channel_id"]))
    if channel is None:
        return

    pairs = [(str(row["user_a_id"]), str(row["user_b_id"])) for row in pair_rows]
    unmatched_user_id = str(extra.get("unmatched_user_id") or "") or None
    try:
        message = await channel.fetch_message(int(round_row["message_id"]))
        await message.edit(
            embed=build_trust_decision_embed(
                round_id,
                str(round_row["title"]),
                pairs,
                unmatched_user_id,
                str(extra.get("emergency_user_id") or "") or None,
                extra.get("decision_close_at"),
            ),
            view=emergency_join_view(round_id) if unmatched_user_id else None,
        )
    except (discord.HTTPException, ValueError):
        pass


async def handle_emergency_join(interaction: discord.Interaction, round_id: int) -> None:
    if interaction.user.bot:
        await interaction.response.send_message("봇 계정은 긴급 투입할 수 없습니다.", ephemeral=True)
        return

    user_id = str(interaction.user.id)
    # 방 생성 + 전체 공지까지 API 호출이 여러 번이라 3초 응답 제한을 넘긴다.
    await interaction.response.defer(ephemeral=True)

    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND status = 'open' AND type IN ('trust', 'sand')",
            (round_id,),
        ).fetchone()
        if not round_row:
            await interaction.followup.send("이미 끝난 라운드입니다.", ephemeral=True)
            return

        round_type = str(round_row["type"])
        game_key = round_game_key(round_row)
        extra = round_extra(round_row)
        phase = main_game_phase(round_row)
        # 가라앉는 배는 협상·실행 두 단계 모두에서 투입을 받는다.
        if phase not in {"decision", "playing", "negotiation", "execution"}:
            await interaction.followup.send("지금은 긴급 투입할 수 없습니다.", ephemeral=True)
            return

        registered = conn.execute(
            "SELECT 1 FROM trust_registrations WHERE round_id = ? AND user_id = ?",
            (round_id, user_id),
        ).fetchone()
        if registered:
            await interaction.followup.send(
                "이미 이 게임에 참여 중입니다. 긴급 투입은 게임에 참여하지 않은 사람만 할 수 있습니다.",
                ephemeral=True,
            )
            return

        # 동시 클릭 방어: 대기자를 읽고 비우는 것까지 한 트랜잭션 안에서 끝낸다.
        applied = apply_emergency_join(extra, game_key, user_id)
        if not applied:
            await interaction.followup.send("긴급 투입 자리가 이미 채워졌습니다.", ephemeral=True)
            return

        # 서버 아무나 누를 수 있다. can_join_round와 정산이 통과하도록 이벤트 명단에도 넣는다.
        ensure_user(conn, interaction.user)
        if round_row["event_session_id"] is not None:
            conn.execute(
                """
                INSERT OR IGNORE INTO event_participants (event_session_id, user_id, joined_at)
                VALUES (?, ?, ?)
                """,
                (int(round_row["event_session_id"]), user_id, now_text()),
            )
        conn.execute(
            "INSERT INTO trust_registrations (round_id, user_id, registered_at) VALUES (?, ?, ?)",
            (round_id, user_id, now_text()),
        )
        conn.execute(
            """
            INSERT INTO trust_pairings (round_id, user_a_id, user_b_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (round_id, applied["user_a_id"], applied["user_b_id"], now_text()),
        )
        # 긴급 투입은 event_participants에 INSERT OR IGNORE로 끼어들어가 joined_at만 남는다.
        # '정상 참여'와 구분되지 않으므로 투입 경위를 여기서 남긴다.
        log_activity(
            conn,
            "match",
            "emergency_join",
            event_session_id=round_row["event_session_id"],
            round_id=round_id,
            actor_user_id=user_id,
            target_user_id=(
                str(applied["user_b_id"])
                if str(applied["user_a_id"]) == user_id
                else str(applied["user_a_id"])
            ),
            payload={
                "game_key": game_key,
                "extension_seconds": EMERGENCY_EXTENSION_SECONDS,
            },
        )
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )
        title = str(round_row["title"])
        channel_id = str(round_row["channel_id"])

    close_at = float(applied["close_at"])
    bot.schedule_auto_close(round_id, close_at)

    channel = await runtime.resolve_text_channel(channel_id)
    if channel is None:
        await interaction.followup.send("긴급 투입했지만 채널을 찾지 못해 방을 열지 못했습니다.", ephemeral=True)
        return

    # 새 방을 만들기 전에 알린다. 갓 투입된 짝에게 자기 자신에 대한 공지가 가지 않는다.
    await announce_emergency_join(round_id, close_at)

    user_a_id = str(applied["user_a_id"])
    user_b_id = str(applied["user_b_id"])
    if round_type == "trust":
        created = await create_pair_thread(
            channel,
            f"{title} · 긴급 투입 세트",
            [user_a_id, user_b_id],
            f"딜레마의 섬 #{round_id} 긴급 투입 협상방",
            trust_decision_view(round_id),
            content=trust_pair_intro(user_a_id, user_b_id, close_at),
        )
    elif game_key == "bail":
        created = await create_pair_thread(
            channel,
            f"{title} · 긴급 투입 구조선",
            [user_a_id, user_b_id],
            f"가라앉는 배 #{round_id} 긴급 투입 방",
            bail_pair_view(round_id, applied["pair"], extra),
            content=bail_pair_intro(user_a_id, user_b_id),
            embed=build_bail_pair_embed(title, applied["pair"], extra),
        )
    else:
        created = await create_pair_thread(
            channel,
            f"{title} · 긴급 투입 모래성",
            [user_a_id, user_b_id],
            f"모래성 게임 #{round_id} 긴급 투입 방",
            sand_play_view(round_id),
            content=sand_pair_intro(user_a_id, user_b_id),
            embed=build_sand_pair_embed(title, applied["pair"], extra),
        )

    if created is None:
        await interaction.followup.send(
            "긴급 투입은 됐지만 비공개 방을 열지 못했습니다. 운영자에게 알려주세요.",
            ephemeral=True,
        )
    else:
        thread, message = created
        save_pair_thread_id(round_id, user_a_id, user_b_id, str(thread.id))
        if game_key == "bail":
            store_bail_thread_ids(
                round_id, {(user_a_id, user_b_id): (str(thread.id), str(message.id))}
            )
        elif round_type == "sand":
            store_sand_thread_ids(
                round_id, {(user_a_id, user_b_id): (str(thread.id), str(message.id))}
            )
        await interaction.followup.send(
            f"긴급 투입 완료! <@{user_a_id}>님과의 방이 열렸습니다.",
            ephemeral=True,
        )

    if game_key == "bail":
        await refresh_main_bail_board(round_id)
    elif round_type == "sand":
        await refresh_main_sand_board(round_id)
    else:
        await refresh_main_trust_board(round_id)


async def handle_sand_join(interaction: discord.Interaction, round_id: int) -> None:
    if interaction.user.bot:
        await interaction.response.send_message("봇 계정은 참여할 수 없습니다.", ephemeral=True)
        return
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'sand' AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row or round_extra(round_row).get("sand_phase") != "registration":
            await interaction.response.send_message("참여자 모집이 이미 끝났습니다.", ephemeral=True)
            return
        if not can_join_round(conn, round_id, str(interaction.user.id)):
            await interaction.response.send_message(
                "이벤트 오픈 때 `참여하기`를 누른 유저만 게임에 참여할 수 있습니다.",
                ephemeral=True,
            )
            return
        existing = conn.execute(
            "SELECT 1 FROM trust_registrations WHERE round_id = ? AND user_id = ?",
            (round_id, str(interaction.user.id)),
        ).fetchone()
        if existing:
            await interaction.response.send_message("이미 모래성 게임에 참여 등록했습니다.", ephemeral=True)
            return
        ensure_user(conn, interaction.user)
        conn.execute(
            "INSERT INTO trust_registrations (round_id, user_id, registered_at) VALUES (?, ?, ?)",
            (round_id, str(interaction.user.id), now_text()),
        )
    await interaction.response.send_message("참여 체크 완료! 모집 마감 후 1:1로 무작위 매칭됩니다.", ephemeral=True)


def find_sand_pair(extra: dict[str, Any], user_id: str) -> dict[str, Any] | None:
    for pair in extra.get("sand_pairs", []):
        if user_id in {str(pair.get("user_a_id")), str(pair.get("user_b_id"))}:
            return pair
    return None


def sand_pair_threshold(pair: dict[str, Any]) -> tuple[int, int]:
    """(두 수의 차이, 무너지는 덜기 횟수).

    차이만큼은 안전하게 덜 수 있고 그 횟수를 초과하는 순간 무너진다.
    예: 9와 7이면 차이가 2 -> 2번까지 성공, 3번째에 붕괴.
    차이가 0이면 첫 덜기에서 바로 무너진다.
    """
    raw_difference = abs(int(pair["number_a"]) - int(pair["number_b"]))
    return raw_difference, raw_difference + 1


def sand_games_complete(extra: dict[str, Any]) -> bool:
    pairs = extra.get("sand_pairs", [])
    return bool(pairs) and all(pair.get("phase") == "resolved" for pair in pairs)


def apply_sand_action(pair: dict[str, Any], user_id: str, action: str) -> str:
    """모래성 한 수를 pair에 반영하고 행동한 사람에게 보여줄 문구를 돌려준다.

    DB도 디스코드도 건드리지 않는 순수 함수라, 마감 시 랜덤 자동 진행에서도 그대로 재사용한다.
    호출 전에 차례·단계 검증이 끝나 있어야 한다.
    """
    phase = str(pair.get("phase"))
    user_a_id = str(pair["user_a_id"])
    user_b_id = str(pair["user_b_id"])
    other_user_id = user_b_id if user_id == user_a_id else user_a_id
    raw_difference, collapse_attempt = sand_pair_threshold(pair)

    if action == "quit":
        if phase == "awaiting_quit_response":
            quitter_id = str(pair["quitter_id"])
            pair.update(
                {
                    "phase": "resolved",
                    "outcome": "mutual_quit",
                    "result": {"losers": [quitter_id, user_id]},
                }
            )
            return "상대의 포기에 함께 포기했습니다. 두 사람 모두 -3점입니다."
        pair.update(
            {
                "phase": "awaiting_quit_response",
                "quitter_id": user_id,
                "current_user_id": other_user_id,
            }
        )
        return "포기했습니다. 상대가 포기할지, 마지막으로 모래를 덜지 결정합니다."

    attempt = int(pair.get("successful_removals", 0)) + 1
    collapsed = attempt >= collapse_attempt
    if collapsed:
        if phase == "awaiting_quit_response":
            quitter_id = str(pair["quitter_id"])
            pair.update(
                {
                    "phase": "resolved",
                    "outcome": "quit_collapse",
                    "result": {"winner": quitter_id, "collapsed_user": user_id},
                    "collapse_attempt": attempt,
                    "raw_difference": raw_difference,
                }
            )
            return "모래를 덜다가 무너졌습니다. 나는 -3점, 먼저 포기한 상대는 +2점입니다."
        pair.update(
            {
                "phase": "resolved",
                "outcome": "plain_collapse",
                "result": {"collapsed_user": user_id},
                "collapse_attempt": attempt,
                "raw_difference": raw_difference,
            }
        )
        return "-3점입니다."

    if phase == "awaiting_quit_response":
        quitter_id = str(pair["quitter_id"])
        pair.update(
            {
                "phase": "resolved",
                "outcome": "quit_success",
                "successful_removals": attempt,
                "result": {"winner": user_id, "quitter": quitter_id},
                "raw_difference": raw_difference,
            }
        )
        return "마지막 모래 덜기에 성공했습니다. 나는 +5점, 먼저 포기한 상대는 -3점입니다."

    pair.update(
        {
            "successful_removals": attempt,
            "current_user_id": other_user_id,
        }
    )
    return "모래 덜기 성공!"


def autocomplete_sand_pair(pair: dict[str, Any]) -> None:
    """마감 시점에 안 끝난 페어를 랜덤으로 끝까지 진행시킨다.

    숫자를 안 낸 사람에게는 1~10을 무작위로 배정하고, 남은 턴은 포기/덜기를 무작위로 고른다.
    미선택으로 0점이 되는 대신 어떤 식으로든 결과가 나오게 하는 것이 목적이다.
    """
    if str(pair.get("phase")) == "resolved":
        return

    for key in ("number_a", "number_b"):
        if pair.get(key) is None:
            pair[key] = random.randint(1, 10)
    if str(pair.get("phase")) == "number_selection":
        pair["phase"] = "active"
    if not pair.get("current_user_id"):
        pair["current_user_id"] = random.choice([pair["user_a_id"], pair["user_b_id"]])

    # 차이는 최대 9라 붕괴까지 최대 10수 + 포기 응답 1수. 상한은 순수 안전장치다.
    for _ in range(20):
        if str(pair.get("phase")) == "resolved":
            break
        apply_sand_action(pair, str(pair["current_user_id"]), random.choice(["quit", "remove"]))

    pair["auto_completed"] = True


def sand_pair_thread_matches(pair: dict[str, Any], channel_id: int | None) -> bool:
    """누른 곳이 본인 짝의 비공개 방인지 확인한다.

    thread_id가 없는 짝은 이 기능 이전에 시작된 라운드이므로, 종전처럼 어디서든 진행할 수 있게 둔다.
    """
    thread_id = str(pair.get("thread_id") or "")
    if not thread_id:
        return True
    return thread_id == str(channel_id)


async def refresh_main_sand_board(round_id: int) -> None:
    """메인 채널의 실시간 진행판을 다시 그린다. 게임 버튼은 각 비공개 방에만 있다."""
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
    if not round_row or not round_row["message_id"]:
        return

    extra = round_extra(round_row)
    channel = await runtime.resolve_text_channel(str(round_row["channel_id"]))
    if channel is None:
        return
    try:
        message = await channel.fetch_message(int(round_row["message_id"]))
        await message.edit(
            embed=build_sand_play_embed(round_row, extra),
            view=emergency_join_view(round_id) if extra.get("unmatched_user_id") else None,
        )
    except (discord.HTTPException, ValueError):
        pass


def save_sand_pair_message_id(round_id: int, pair: dict[str, Any], message_id: str) -> None:
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row:
            return
        extra = round_extra(round_row)
        target = find_sand_pair(extra, str(pair["user_a_id"]))
        if not target:
            return
        target["play_message_id"] = message_id
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )


async def refresh_sand_pair_board(interaction: discord.Interaction, round_id: int) -> None:
    """누른 사람의 방 진행판을 다시 그린다.

    버튼을 누를 때마다 개인 응답(ephemeral)이 방에 쌓여서 버튼이 달린 진행판이 위로 밀려 올라간다.
    그러면 다음 차례에 버튼을 누르려고 스크롤을 올려야 한다. 그래서 진행 중인 짝은 진행판을 지우고
    맨 아래에 다시 붙인다. 끝난 짝은 더 누를 버튼이 없으므로 제자리에서 비활성화만 한다.

    숫자 선택은 두 사람이 동시에 누를 수 있다. 그때 `interaction.message`는 상대가 이미 재게시해서
    사라진 옛 메시지일 수 있으므로, 락을 잡고 DB에서 현재 진행판 id를 다시 읽어야 방이 두 개로 갈리지 않는다.
    """
    channel = interaction.channel
    if channel is None:
        return

    async with bot.sand_board_lock(channel.id):
        with db() as conn:
            round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row:
            return

        extra = round_extra(round_row)
        pair = find_sand_pair(extra, str(interaction.user.id))
        if not pair:
            return

        resolved = str(pair.get("phase")) == "resolved"
        embed = build_sand_pair_embed(str(round_row["title"]), pair, extra)
        view = sand_play_view(round_id, disabled=resolved)

        board_message_id = str(pair.get("play_message_id") or "")
        if not board_message_id and interaction.message:
            board_message_id = str(interaction.message.id)

        # 방이 없는 옛 라운드(메인 채널 진행)나 이미 끝난 짝은 제자리에서 갱신한다.
        if resolved or not isinstance(channel, discord.Thread):
            if board_message_id:
                try:
                    message = await channel.fetch_message(int(board_message_id))
                    await message.edit(embed=embed, view=view)
                except (discord.NotFound, discord.HTTPException, ValueError):
                    pass
            return

        if board_message_id:
            try:
                old_board = await channel.fetch_message(int(board_message_id))
                await old_board.delete()
            except (discord.NotFound, discord.HTTPException, ValueError):
                pass
        try:
            new_board = await channel.send(embed=embed, view=view)
        except discord.HTTPException:
            return
        save_sand_pair_message_id(round_id, pair, str(new_board.id))


async def refresh_or_finish_sand_round(
    interaction: discord.Interaction,
    round_id: int,
    private_message: str | None = None,
    public_message: str | None = None,
) -> None:
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row:
            await interaction.response.send_message("존재하지 않는 라운드입니다.", ephemeral=True)
            return
        channel_id = str(round_row["channel_id"])
        complete = sand_games_complete(round_extra(round_row))

    # 공개 메시지는 이 1:1 방의 두 사람 모두에게 보이게, 개인 안내(점수 등)는 본인만 보게 보낸다.
    if public_message:
        await interaction.response.send_message(public_message)
        if private_message:
            await interaction.followup.send(private_message, ephemeral=True)
    elif private_message:
        await interaction.response.send_message(private_message, ephemeral=True)
    else:
        await interaction.response.defer()

    # 누른 사람의 방만 갱신한다. 다른 짝의 상황은 이 방에 새어 나가지 않는다.
    await refresh_sand_pair_board(interaction, round_id)
    await refresh_main_sand_board(round_id)

    if not complete:
        return
    result = close_round_record(round_id, interaction.guild, closed_by="모든 1:1 게임 종료")
    if not result:
        return
    task = bot.auto_close_tasks.pop(round_id, None)
    if task:
        task.cancel()
    await publish_sand_pair_results(round_id)

    # followup은 버튼을 누른 비공개 방으로 들어간다. 라운드 결과는 메인 채널에 알려야 한다.
    _, result_embed = result
    channel = await runtime.resolve_text_channel(channel_id)
    if channel is not None:
        try:
            await channel.send(embed=result_embed, view=planned_mini_game_view(round_id))
        except discord.HTTPException:
            pass
    await publish_event_summary_if_complete(round_row["event_session_id"])


async def handle_sand_number(
    interaction: discord.Interaction,
    round_id: int,
    number: int,
) -> None:
    user_id = str(interaction.user.id)
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'sand' AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row:
            await interaction.response.send_message("이미 끝난 모래성 게임입니다.", ephemeral=True)
            return
        extra = round_extra(round_row)
        if extra.get("sand_phase") != "playing":
            await interaction.response.send_message("아직 숫자를 정할 수 없습니다.", ephemeral=True)
            return
        pair = find_sand_pair(extra, user_id)
        if not pair:
            await interaction.response.send_message("이 라운드에 매칭된 참가자가 아닙니다.", ephemeral=True)
            return
        if not sand_pair_thread_matches(pair, interaction.channel_id):
            await interaction.response.send_message(
                "자신에게 배정된 1:1 모래성 방에서만 진행할 수 있습니다.",
                ephemeral=True,
            )
            return
        if pair.get("phase") != "number_selection":
            await interaction.response.send_message("숫자 선택 단계가 이미 끝났습니다.", ephemeral=True)
            return
        key = "number_a" if user_id == str(pair["user_a_id"]) else "number_b"
        if pair.get(key) is not None:
            await interaction.response.send_message("숫자를 이미 확정했습니다. 변경할 수 없습니다.", ephemeral=True)
            return
        pair[key] = number
        both_selected = pair.get("number_a") is not None and pair.get("number_b") is not None
        if both_selected:
            pair["phase"] = "active"
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )

    suffix = (
        " 두 사람의 선택이 끝났습니다. 차례는 아래 박스를 확인해주세요!"
        if both_selected
        else " 상대의 선택을 기다립니다."
    )
    await refresh_or_finish_sand_round(interaction, round_id, f"`{number}`을 확정했습니다.{suffix}")


async def handle_sand_action(
    interaction: discord.Interaction,
    round_id: int,
    action: str,
) -> None:
    user_id = str(interaction.user.id)
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'sand' AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row:
            await interaction.response.send_message("이미 끝난 모래성 게임입니다.", ephemeral=True)
            return
        extra = round_extra(round_row)
        pair = find_sand_pair(extra, user_id)
        if not pair:
            await interaction.response.send_message("이 라운드에 매칭된 참가자가 아닙니다.", ephemeral=True)
            return
        if not sand_pair_thread_matches(pair, interaction.channel_id):
            await interaction.response.send_message(
                "자신에게 배정된 1:1 모래성 방에서만 진행할 수 있습니다.",
                ephemeral=True,
            )
            return
        phase = str(pair.get("phase"))
        if phase == "number_selection":
            await interaction.response.send_message("두 참가자 모두 먼저 1~10 중 숫자를 확정해야 합니다.", ephemeral=True)
            return
        if phase == "resolved":
            await interaction.response.send_message("이 1:1 게임은 이미 끝났습니다.", ephemeral=True)
            return
        if user_id != str(pair.get("current_user_id")):
            await interaction.response.send_message("지금은 상대의 차례입니다.", ephemeral=True)
            return

        private_message = apply_sand_action(pair, user_id, action)

        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )

    outcome = str(pair.get("outcome") or "")
    public_message = None
    if outcome == "plain_collapse":
        # 붕괴 안내는 방 모두에게, 점수(-1점)는 본인만.
        public_message = "모래를 덜다가 무너졌습니다."
    elif outcome == "quit_collapse":
        # 문장 전체를 방 모두에게 공개.
        public_message = private_message
        private_message = None
    await refresh_or_finish_sand_round(interaction, round_id, private_message, public_message)


# ---------------------------------------------------------------------------
# 가라앉는 배 (bail) — 협상 → 실행 2단계 메인게임
#
# 모래성과 같은 1:1 비공개 방 구조를 쓰지만 단계가 하나 더 있다.
#   registration → negotiation(교대 제안) → execution(동시 비공개 제출) → scored
# 협상에서 맺은 합의는 기록만 되고 점수에 영향이 없다. 그게 이 게임의 핵심 규칙이라
# 정산은 오직 execution 단계의 제출값만 본다.
#
# execution 단계는 **조기 종료가 없다.** 협상은 모든 방이 끝나면 바로 넘어가지만, 퍼내기는
# 두 사람이 다 제출해도 제한 시간을 끝까지 쓰고 마감 시점에 한꺼번에 정산한다. 제출 인원수를
# 진행판에 세지 않는 것과 한 묶음이다 — 결과가 뜨는 시점이나 "1/2" 표시 중 하나라도 남으면
# 상대가 제출했는지가 새고, 그러면 비공개 제출이 눈치싸움으로 바뀐다.
# 대신 방 안에서 남은 시간을 알 수 없어지므로 마감 카운트다운을 방마다 띄운다.
# 정산이 마감에만 일어나므로 제출은 그때까지 몇 번이든 덮어쓸 수 있다(`apply_bail_scoop`).
# 번복해도 진행판이 변하지 않아 상대는 알아챌 수 없다 — 위 두 규칙이 무너지면 번복 허용도 못 쓴다.
# ---------------------------------------------------------------------------


# 하루 횟수를 언제 차감할지의 기준.
#   playing/finished          : 게임이 성립했으므로 양쪽 모두 차감
#   betting 만료(accepted_at O): 베팅을 넣지 않은 쪽만 차감
#   pending 만료 / 거절        : 아무도 차감하지 않음 (거절은 언제나 공짜)
#
# betting 만료를 미입력자에게만 물리는 이유: 수락까지 받아놓고 베팅을 안 넣으면
# 상대는 동시 1결투 제한에 묶여 아무것도 못 한다. 차감이 없으면 무한 반복이 가능하다.


register_settler("baseball", settle_baseball)


class EventBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        # 멤버 캐시를 채워 member_name() 미스를 없앤다. Developer Portal의
        # SERVER MEMBERS INTENT 토글이 함께 ON이어야 로그인이 된다 (issue #19).
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.auto_close_tasks: dict[int, asyncio.Task[None]] = {}
        # 라운드 타이머와 키가 겹치면 안 된다(둘 다 int 키인데 round_id와 game_id는 별개다).
        self.roulette_turn_tasks: dict[int, asyncio.Task[None]] = {}
        self.baseball_activation_tasks: dict[int, asyncio.Task[None]] = {}
        self.baseball_pitch_tasks: dict[int, asyncio.Task[None]] = {}
        self.auto_close_restored = False
        self.afk_coin_task: asyncio.Task[None] | None = None
        self.sand_board_locks: dict[int, asyncio.Lock] = {}
        self.anon_board_locks: dict[int, asyncio.Lock] = {}

    def sand_board_lock(self, thread_id: int) -> asyncio.Lock:
        """방별 진행판 재게시를 직렬화한다. 두 사람이 동시에 눌러도 진행판이 하나만 남는다."""
        return self.sand_board_locks.setdefault(thread_id, asyncio.Lock())

    def release_board_lock(self, thread_id: int) -> None:
        """라운드가 끝나 방을 닫을 때 락을 버린다. 게임 모듈이 dict를 직접 만지지 않게 한다."""
        self.sand_board_locks.pop(thread_id, None)

    def anon_board_lock(self, thread_id: int) -> asyncio.Lock:
        """익명 토론방 버튼판 재게시를 직렬화한다. 여러 명이 동시에 발언해도 버튼판이 하나만 남는다."""
        return self.anon_board_locks.setdefault(thread_id, asyncio.Lock())

    async def setup_hook(self) -> None:
        guild_id = os.getenv("GUILD_ID", "").strip()
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"Slash commands synced to guild {guild_id}")
        else:
            await self.tree.sync()
            print("Slash commands synced globally. It can take a while to appear.")

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user} ({self.user.id if self.user else 'unknown'})")
        print(f"Database: {config.DB_PATH}")
        if not self.auto_close_restored:
            self.auto_close_restored = True
            self.restore_auto_close_tasks()
            # 이걸 빠뜨리면 재시작 순간 진행 중이던 차례의 타이머가 사라져, 상대가
            # 방아쇠를 안 당기면 예치금이 영구 동결된다(자동 패배가 영영 안 온다).
            self.restore_roulette_turn_tasks()
            # 야구의 3초 버튼 활성화와 7초 결과 타이머도 DB 시각으로 복구한다.
            self.restore_baseball_pitch_tasks()
        if self.afk_coin_task is None or self.afk_coin_task.done():
            self.afk_coin_task = asyncio.create_task(self.afk_coin_loop())
        # 로그인 + 진행 중이던 라운드 타이머 복구까지 끝나야 배포가 성공으로 처리된다.
        sd_notify("READY=1")

    async def afk_coin_loop(self) -> None:
        """음성채널에 있는 사람에게 1분마다 잠수 코인을 준다.

        입장/퇴장을 추적하지 않고 매 틱마다 현재 접속자를 훑는다. 코인이 DB에만 쌓이므로
        재시작 후 복구할 상태가 없다 (재시작 시 손해는 1분 미만).
        """
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(AFK_TICK_SECONDS)
            try:
                self.grant_voice_afk_coins()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - 틱 한 번 실패로 루프가 죽으면 안 된다.
                print(f"잠수 코인 적립 실패: {error}")

    def grant_voice_afk_coins(self) -> None:
        with db() as conn:
            for guild in self.guilds:
                for voice_channel in guild.voice_channels:
                    for member in voice_channel.members:
                        if member.bot:
                            continue
                        ensure_user(conn, member)
                        grant_afk_coins(conn, str(member.id), AFK_COIN_PER_TICK)

    def schedule_auto_close(self, round_id: int, close_at: float) -> None:
        existing = self.auto_close_tasks.pop(round_id, None)
        if existing:
            existing.cancel()
        self.auto_close_tasks[round_id] = asyncio.create_task(
            self.auto_close_round(round_id, close_at)
        )

    def restore_auto_close_tasks(self) -> None:
        with db() as conn:
            rows = conn.execute(
                "SELECT id, extra_json FROM rounds WHERE status = 'open'"
            ).fetchall()

        for row in rows:
            try:
                extra: dict[str, Any] = json.loads(row["extra_json"])
                close_at = float(extra.get("auto_close_at", 0))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if close_at > 0:
                self.schedule_auto_close(int(row["id"]), close_at)

    def schedule_baseball_pitch(
        self,
        round_id: int,
        result_at: float,
        pitch_number: int,
    ) -> None:
        existing = self.baseball_pitch_tasks.pop(round_id, None)
        if existing:
            existing.cancel()
        self.baseball_pitch_tasks[round_id] = asyncio.create_task(
            self.watch_baseball_pitch(round_id, result_at, pitch_number)
        )

    def schedule_baseball_activation(
        self,
        round_id: int,
        activate_at: float,
        pitch_number: int,
    ) -> None:
        existing = self.baseball_activation_tasks.pop(round_id, None)
        if existing:
            existing.cancel()
        self.baseball_activation_tasks[round_id] = asyncio.create_task(
            self.watch_baseball_activation(round_id, activate_at, pitch_number)
        )

    def restore_baseball_pitch_tasks(self) -> None:
        with db() as conn:
            rows = conn.execute(
                "SELECT * FROM rounds WHERE type = 'grid' AND status = 'open'"
            ).fetchall()
        for row in rows:
            extra = round_extra(row)
            if extra.get("mode") != "baseball":
                continue
            phase = str(extra.get("baseball_phase") or "")
            try:
                pitch_number = int(extra.get("baseball_pitch") or 1)
            except (TypeError, ValueError):
                continue
            if phase == "winding_up":
                try:
                    activate_at = float(extra.get("baseball_activate_at") or 0)
                except (TypeError, ValueError):
                    continue
                if activate_at > 0:
                    self.schedule_baseball_activation(
                        int(row["id"]), activate_at, pitch_number
                    )
                continue
            if phase not in {"swinging", "result_ready"}:
                continue
            try:
                result_at = float(extra.get("baseball_result_at") or 0)
            except (TypeError, ValueError):
                continue
            if result_at > 0:
                self.schedule_baseball_pitch(int(row["id"]), result_at, pitch_number)

    async def watch_baseball_activation(
        self,
        round_id: int,
        activate_at: float,
        pitch_number: int,
    ) -> None:
        try:
            await asyncio.sleep(max(0, activate_at - time.time()))
            await activate_baseball_pitch(round_id, pitch_number, activate_at)
        except asyncio.CancelledError:
            raise
        finally:
            if self.baseball_activation_tasks.get(round_id) is asyncio.current_task():
                self.baseball_activation_tasks.pop(round_id, None)

    async def watch_baseball_pitch(
        self,
        round_id: int,
        result_at: float,
        pitch_number: int,
    ) -> None:
        try:
            await asyncio.sleep(max(0, result_at - time.time()))
            with db() as conn:
                row = conn.execute(
                    "SELECT * FROM rounds WHERE id = ? AND status = 'open'",
                    (round_id,),
                ).fetchone()
                extra = round_extra(row) if row else {}
            if (
                not row
                or extra.get("mode") != "baseball"
                or extra.get("baseball_phase") not in {"swinging", "result_ready"}
                or int(extra.get("baseball_pitch") or 1) != pitch_number
            ):
                return
            try:
                current_result_at = float(extra.get("baseball_result_at") or 0)
            except (TypeError, ValueError):
                return
            if abs(current_result_at - result_at) > 0.05:
                return
            await publish_baseball_pitch_result(round_id, pitch_number)
        except asyncio.CancelledError:
            raise
        finally:
            if self.baseball_pitch_tasks.get(round_id) is asyncio.current_task():
                self.baseball_pitch_tasks.pop(round_id, None)

    def schedule_roulette_turn(
        self, game_id: int, deadline: float, shot_index: int
    ) -> None:
        existing = self.roulette_turn_tasks.pop(game_id, None)
        if existing:
            existing.cancel()
        self.roulette_turn_tasks[game_id] = asyncio.create_task(
            self.watch_roulette_turn(game_id, deadline, shot_index)
        )

    def cancel_roulette_turn(self, game_id: int) -> None:
        task = self.roulette_turn_tasks.pop(game_id, None)
        if task:
            task.cancel()

    def restore_roulette_turn_tasks(self) -> None:
        """진행 중이던 차례 타이머를 DB에서 복구한다.

        라운드 쪽 복구(restore_auto_close_tasks)는 rounds.extra_json을 읽어서 룰렛에는
        쓸 수 없다. 룰렛은 별도 테이블이고 마감 시각을 expires_at에 들고 있다.
        """
        with db() as conn:
            rows = conn.execute(
                "SELECT id, expires_at, shots_fired FROM russian_roulette_games "
                "WHERE status = 'playing' AND expires_at IS NOT NULL"
            ).fetchall()

        for row in rows:
            self.schedule_roulette_turn(
                int(row["id"]), float(row["expires_at"]), int(row["shots_fired"])
            )

    async def watch_roulette_turn(
        self, game_id: int, deadline: float, shot_index: int
    ) -> None:
        """차례 제한 시간을 지키고, 넘기면 자동 패배로 마감한다."""
        try:
            await asyncio.sleep(
                max(0, deadline - time.time() - AUTO_CLOSE_WARNING_SECONDS)
            )
            with db() as conn:
                row = conn.execute(
                    "SELECT * FROM russian_roulette_games "
                    "WHERE id = ? AND status = 'playing'",
                    (game_id,),
                ).fetchone()
                game = dict(row) if row else None
            # 그새 격발돼서 차례가 넘어갔거나 게임이 끝났으면 낡은 타이머다.
            if not game or int(game["shots_fired"]) != shot_index:
                return

            channel = self.get_channel(int(game["channel_id"]))
            if channel is None:
                try:
                    channel = await self.fetch_channel(int(game["channel_id"]))
                except discord.HTTPException:
                    channel = None
            if channel is None:
                await asyncio.sleep(max(0, deadline - time.time()))
            else:
                await publish_auto_close_countdown(channel, "차례 종료", deadline)

            with db() as conn:
                finished = timeout_roulette_turn(conn, game_id, shot_index)
            if finished is None:
                return
            await refresh_roulette_message(self, finished)
        except asyncio.CancelledError:
            raise
        finally:
            if self.roulette_turn_tasks.get(game_id) is asyncio.current_task():
                self.roulette_turn_tasks.pop(game_id, None)

    async def auto_close_round(self, round_id: int, close_at: float) -> None:
        try:
            await asyncio.sleep(
                max(0, close_at - time.time() - AUTO_CLOSE_WARNING_SECONDS)
            )
            with db() as conn:
                row = conn.execute(
                    "SELECT * FROM rounds WHERE id = ? AND status = 'open'",
                    (round_id,),
                ).fetchone()
            if not row:
                return

            channel_id = str(row["channel_id"])
            channel = self.get_channel(int(channel_id))
            if channel is None:
                try:
                    channel = await self.fetch_channel(int(channel_id))
                except discord.HTTPException:
                    return
            guild = getattr(channel, "guild", None)

            # 가라앉는 배 실행 단계는 참가자가 전부 비공개 방에 들어가 있어서 메인 채널
            # 카운트다운이 보이지 않는다. 같은 모듈을 방마다 하나씩 더 돌려 남은 시간을 알린다.
            countdown_targets: list[tuple[Any, str]] = [(channel, auto_close_action_label(row))]
            if round_game_key(row) == "bail" and round_extra(row).get("bail_phase") == "execution":
                countdown_targets.extend(
                    (thread, "퍼내기 마감") for _, thread in await bail_pair_threads(round_id)
                )
            await asyncio.gather(
                *(
                    publish_auto_close_countdown(target, label, close_at)
                    for target, label in countdown_targets
                )
            )

            # 카운트다운 도중 수동 마감이나 긴급 연장으로 상태가 바뀔 수 있다.
            # 취소 신호가 늦게 도착해도 오래된 마감 시각으로 정산하지 않도록 다시 확인한다.
            with db() as conn:
                fresh_row = conn.execute(
                    "SELECT * FROM rounds WHERE id = ? AND status = 'open'",
                    (round_id,),
                ).fetchone()
            if not fresh_row:
                return
            try:
                current_close_at = float(round_extra(fresh_row).get("auto_close_at") or 0)
            except (TypeError, ValueError):
                return
            if abs(current_close_at - close_at) > 0.5:
                return
            row = fresh_row

            if row["type"] == "trust" and round_extra(row).get("trust_phase") == "registration":
                transition = start_trust_decision(round_id)
                if not transition:
                    return
                await create_trust_pair_threads(transition)
                await update_trust_main_message(transition)
                if transition["state"] == "decision":
                    self.schedule_auto_close(round_id, float(transition["close_at"]))
                return

            if row["type"] == "sand" and round_extra(row).get("sand_phase") == "registration":
                transition = start_sand_game(round_id)
                if not transition:
                    return
                await create_sand_pair_threads(transition)
                await update_trust_main_message(transition)
                if transition["state"] == "playing":
                    self.schedule_auto_close(round_id, float(transition["close_at"]))
                return

            # 가라앉는 배는 단계가 하나 더 있다: 모집 → 협상 → 실행 → 정산.
            if round_game_key(row) == "bail":
                bail_phase = round_extra(row).get("bail_phase")
                if bail_phase == "registration":
                    transition = start_bail_negotiation(round_id)
                    if not transition:
                        return
                    await create_bail_pair_threads(transition)
                    await update_trust_main_message(transition)
                    if transition["state"] == "negotiation":
                        self.schedule_auto_close(round_id, float(transition["close_at"]))
                    return
                if bail_phase == "negotiation":
                    transition = start_bail_execution(round_id)
                    if not transition:
                        return
                    await refresh_all_bail_pair_boards(round_id)
                    await refresh_main_bail_board(round_id)
                    self.schedule_auto_close(round_id, float(transition["close_at"]))
                    if hasattr(channel, "send"):
                        try:
                            await channel.send(
                                "🪣 협상 시간이 끝났습니다. **실행 단계**를 시작합니다 — "
                                "각자 실제로 퍼낼 횟수를 비공개로 제출하세요. "
                                f"(마감 {close_time_text(transition['close_at'])})"
                            )
                        except discord.HTTPException:
                            pass
                    return

            row_extra = round_extra(row)
            if (
                row["type"] == "balance"
                and row_extra.get("mode") == "minority_balance"
                and row_extra.get("balance_phase") == "initial"
            ):
                transition = start_minority_balance_revote(round_id)
                if not transition:
                    return
                await publish_minority_balance_revote(transition)
                self.schedule_auto_close(round_id, float(transition["close_at"]))
                return

            result = close_round_record(round_id, guild, closed_by="자동 마감")
            if not result:
                return

            _, embed = result
            if row["type"] == "trust":
                await publish_trust_pair_results(round_id)
            elif round_game_key(row) == "bail":
                await publish_bail_pair_results(round_id)
            elif row["type"] == "sand":
                await publish_sand_pair_results(round_id)
            await archive_round_discussion_thread(row, reason="라운드 종료 후 영구 보관")
            if hasattr(channel, "send"):
                await channel.send(embed=embed, view=planned_mini_game_view(round_id))
            await publish_event_summary_if_complete(row["event_session_id"])
        except asyncio.CancelledError:
            raise
        finally:
            if self.auto_close_tasks.get(round_id) is asyncio.current_task():
                self.auto_close_tasks.pop(round_id, None)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith("eventbot:"):
            return

        parts = custom_id.split(":")
        if len(parts) < 3:
            await interaction.response.send_message("알 수 없는 버튼입니다.", ephemeral=True)
            return

        _, action = parts[:2]
        if action in {
            "rouletteaccept",
            "roulettedecline",
            "roulettebet",
            "roulettescore",
            "roulettesettle",
            "roulettetrigger",
        }:
            try:
                game_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 러시안룰렛 게임 번호입니다.", ephemeral=True)
                return
            if action == "rouletteaccept":
                await handle_roulette_accept(interaction, game_id)
            elif action == "roulettedecline":
                await handle_roulette_decline(interaction, game_id)
            elif action == "roulettebet":
                await handle_roulette_bet(interaction, game_id)
            elif action == "roulettescore":
                await handle_roulette_score(interaction, game_id)
            elif action == "roulettesettle":
                await handle_roulette_settle(interaction, game_id)
            else:
                await handle_roulette_trigger(interaction, game_id)
            return

        if action == "eventjoin":
            try:
                event_session_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 이벤트 번호입니다.", ephemeral=True)
                return
            await handle_event_join(interaction, event_session_id)
            return

        if action == "eventcancel":
            try:
                event_session_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 이벤트 번호입니다.", ephemeral=True)
                return
            await handle_event_cancel(interaction, event_session_id)
            return

        if action == "startmini":
            try:
                main_round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 메인게임 번호입니다.", ephemeral=True)
                return
            await handle_planned_mini_game(interaction, main_round_id)
            return

        if action == "emergency":
            try:
                round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return
            await handle_emergency_join(interaction, round_id)
            return

        if action == "sandjoin":
            try:
                round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return
            await handle_sand_join(interaction, round_id)
            return

        if action == "sandnumber":
            if len(parts) != 4 or parts[3] not in SAND_NUMBER_CHOICES:
                await interaction.response.send_message("잘못된 모래성 숫자입니다.", ephemeral=True)
                return
            try:
                round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return
            await handle_sand_number(interaction, round_id, int(parts[3]))
            return

        if action == "sandaction":
            if len(parts) != 4 or parts[3] not in {"quit", "remove"}:
                await interaction.response.send_message("잘못된 모래성 행동입니다.", ephemeral=True)
                return
            try:
                round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return
            await handle_sand_action(interaction, round_id, parts[3])
            return

        # 협상은 보상금(0~8)을, 실행은 퍼낼 횟수(0~6)를 고르므로 허용 범위가 다르다.
        if action in {"bailoffer", "bailscoop"}:
            choices = BAIL_REWARD_CHOICES if action == "bailoffer" else BAIL_SCOOP_CHOICES
            if len(parts) != 4 or parts[3] not in choices:
                await interaction.response.send_message(
                    "잘못된 보상금 제안입니다." if action == "bailoffer" else "잘못된 퍼내기 횟수입니다.",
                    ephemeral=True,
                )
                return
            try:
                round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return
            if action == "bailoffer":
                await handle_bail_offer(interaction, round_id, int(parts[3]))
            else:
                await handle_bail_scoop(interaction, round_id, int(parts[3]))
            return

        if action == "bailrespond":
            if len(parts) != 4 or parts[3] not in {"accept", "reject"}:
                await interaction.response.send_message("잘못된 협상 응답입니다.", ephemeral=True)
                return
            try:
                round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return
            await handle_bail_response(interaction, round_id, parts[3] == "accept")
            return

        if action == "trustjoin":
            try:
                round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return
            await handle_trust_join(interaction, round_id)
            return

        if action == "trustdecision":
            if len(parts) != 4:
                await interaction.response.send_message("잘못된 딜레마의 섬 선택입니다.", ephemeral=True)
                return
            _, _, round_id_text, choice = parts
            try:
                round_id = int(round_id_text)
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return
            if choice not in {"trust", "betray"}:
                await interaction.response.send_message("잘못된 선택입니다.", ephemeral=True)
                return
            await handle_trust_decision(interaction, round_id, choice)
            return

        if action in {"contractaccept", "contractdecline"}:
            await interaction.response.send_message(
                "거래 제안 방식은 랜덤 매칭 방식으로 변경되었습니다. 새 딜레마의 섬에 참여해주세요.",
                ephemeral=True,
            )
            return

        if action == "trustcontract":
            await interaction.response.send_message(
                "거래 제안 방식은 랜덤 매칭 방식으로 변경되었습니다. 새 딜레마의 섬에 참여해주세요.",
                ephemeral=True,
            )
            return

        if action in {"trustself", "trust"}:
            await interaction.response.send_message(
                "직접 점수 투표 방식은 랜덤 매칭 신뢰/배신 방식으로 변경되었습니다. 새 딜레마의 섬에 참여해주세요.",
                ephemeral=True,
            )
            return

        if action == "anonymous":
            try:
                round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return

            if not isinstance(interaction.channel, discord.Thread):
                await interaction.response.send_message("토론 스레드에서만 사용할 수 있는 버튼입니다.", ephemeral=True)
                return

            # can_join_round는 반드시 with 블록 안에서 부른다. 밖으로 나가면 커넥션이 닫혀 있다.
            with db() as conn:
                round_row = conn.execute(
                    """
                    SELECT id FROM rounds
                    WHERE id = ? AND status = 'open' AND discussion_thread_id = ?
                    """,
                    (round_id, str(interaction.channel.id)),
                ).fetchone()
                joinable = bool(round_row) and can_join_round(
                    conn, round_id, str(interaction.user.id)
                )

            if not round_row:
                await interaction.response.send_message("이미 마감되었거나 유효하지 않은 토론방입니다.", ephemeral=True)
                return
            if not joinable:
                await interaction.response.send_message(
                    "이번 이벤트에서 `참여하기`를 누른 유저만 토론에 참여할 수 있습니다.",
                    ephemeral=True,
                )
                return

            await interaction.response.send_modal(AnonymousDiscussionModal(round_id))
            return

        if action == "cutting":
            try:
                round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return

            await handle_cutting_button(interaction, round_id)
            return

        if action == "baseballswing":
            if len(parts) != 4:
                await interaction.response.send_message("잘못된 치기 버튼입니다.", ephemeral=True)
                return
            try:
                round_id = int(parts[2])
                pitch_number = int(parts[3])
            except ValueError:
                await interaction.response.send_message("잘못된 야구 투구 번호입니다.", ephemeral=True)
                return
            if not 1 <= pitch_number <= BASEBALL_TOTAL_PITCHES:
                await interaction.response.send_message("잘못된 야구 투구 번호입니다.", ephemeral=True)
                return
            await handle_baseball_swing(interaction, round_id, pitch_number)
            return

        if len(parts) < 4:
            await interaction.response.send_message("알 수 없는 버튼입니다.", ephemeral=True)
            return

        _, action, round_id_text, choice = parts[:4]
        if action != "vote":
            await interaction.response.send_message("알 수 없는 동작입니다.", ephemeral=True)
            return

        try:
            round_id = int(round_id_text)
        except ValueError:
            await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
            return

        with db() as conn:
            round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
            if not round_row:
                await interaction.response.send_message("존재하지 않는 라운드입니다.", ephemeral=True)
                return
            if round_row["status"] != "open":
                await interaction.response.send_message("이미 마감된 라운드입니다.", ephemeral=True)
                return
            if not can_join_round(conn, round_id, str(interaction.user.id)):
                await interaction.response.send_message(
                    "이번 이벤트에서 `참여하기`를 누른 유저만 게임에 참여할 수 있습니다.",
                    ephemeral=True,
                )
                return
            if round_row["type"] == "top3" and str(interaction.user.id) in {
                str(candidate["user_id"])
                for candidate in round_extra(round_row).get("candidates", [])
            }:
                await interaction.response.send_message(
                    "견제 대상인 상위 3명은 투표할 수 없습니다.",
                    ephemeral=True,
                )
                return

            game_key = round_game_key(round_row)
            if game_key == "unique_number" and choice not in UNIQUE_NUMBER_CHOICES:
                await interaction.response.send_message(
                    "홍피겔만의 황금금고에서는 1~15 중 하나만 선택할 수 있습니다.",
                    ephemeral=True,
                )
                return

            ensure_user(conn, interaction.user)
            ts = now_text()
            # 덮어쓰기 전 값. 소수파 밸런스는 1차 선택이 재선택으로 덮이므로
            # 이 로그가 없으면 '누가 무엇에서 무엇으로 바꿨는지'가 영구히 사라진다.
            previous = conn.execute(
                "SELECT choice FROM votes WHERE round_id = ? AND user_id = ?",
                (round_id, str(interaction.user.id)),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(round_id, user_id) DO UPDATE SET
                    choice = excluded.choice,
                    updated_at = excluded.updated_at
                """,
                (round_id, str(interaction.user.id), choice, ts, ts),
            )
            log_activity(
                conn,
                "vote",
                "cast",
                event_session_id=round_row["event_session_id"],
                round_id=round_id,
                actor_user_id=str(interaction.user.id),
                payload={
                    "game_key": game_key,
                    "choice": choice,
                    "previous_choice": previous["choice"] if previous else None,
                    "changed": bool(previous and previous["choice"] != choice),
                    # 소수파 밸런스의 1차/재선택 구분. 어느 단계의 표인지 사후에 알아야 한다.
                    "phase": round_extra(round_row).get("balance_phase"),
                    "in_thread": str(interaction.channel_id)
                    != str(round_row["channel_id"]),
                },
            )

        await interaction.response.send_message(
            f"`{choice_display(choice, game_key)}` 선택을 저장했습니다. 마감 전까지 버튼을 다시 누르면 선택이 바뀝니다.",
            ephemeral=True,
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

        with db() as conn:
            round_row = conn.execute(
                """
                SELECT * FROM rounds
                WHERE status = 'open' AND discussion_thread_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (str(message.channel.id),),
            ).fetchone()

            if not round_row:
                return

            if round_row["type"] == "trust":
                # The main game's negotiation is intentionally public and not limited to one post.
                return

            # Direct messages reveal their author before the bot can remove them.
            # Discussion posts must therefore go through the anonymous button/modal.
            try:
                await message.delete()
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass


bot = EventBot()
# 게임 모듈이 타이머 콜백에서 채널을 열 때 쓴다 (bot.py를 import 하면 순환이 된다).
runtime.set_client(bot)


def build_balance_embed(round_id: int, question: str, option_a: str, option_b: str) -> discord.Embed:
    embed = discord.Embed(
        title=question,
        description=question,
        color=discord.Color.blurple(),
    )
    embed.add_field(name="🅰️", value=option_a, inline=True)
    embed.add_field(name="🅱️", value=option_b, inline=True)
    embed.add_field(
        name="📜 규칙",
        value=(
            "> 🔒 투표 현황은 마감 전까지 비공개입니다.\n"
            "> 🕶️ 토론 스레드에서 익명으로 몇 번이든 발언할 수 있습니다.\n"
            "> 🏆 마감 후 다수파 선택자에게 **+1점**이 지급됩니다.\n"
            "> ✨ 단, 한 선택지를 단 1명만 골랐다면 그 사람만 **+2점**, 다수파는 **0점**입니다.\n"
            "> ⚠️ 단, 한쪽이 0명이면 밸런스가 성립하지 않으므로 고른 사람 전원 **-2점**입니다."
        ),
        inline=False,
    )
    return embed


def build_minority_balance_revote_embed(
    round_row: sqlite3.Row | dict[str, Any],
    first_a_count: int,
    first_b_count: int,
    close_at: float,
) -> discord.Embed:
    """1차 집계 뒤에 처음으로 변형 규칙을 공개하는 재선택 안내."""
    embed = discord.Embed(
        title=f"1차 집계 · {round_row['question']}",
        description=(
            "첫 선택의 인원수가 공개되었습니다. 지금부터 같은 선택지로 다시 선택합니다.\n"
            "**1차 선택은 그대로 유지됩니다.** 선택을 바꾸고 싶은 경우에만 A/B 버튼을 다시 눌러주세요."
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name=f"🅰️ {round_row['option_a']}",
        value=f"**{first_a_count}명**",
        inline=True,
    )
    embed.add_field(
        name=f"🅱️ {round_row['option_b']}",
        value=f"**{first_b_count}명**",
        inline=True,
    )
    embed.add_field(
        name="🔓 재선택 규칙 공개",
        value=(
            "> 🏆 최종 집계에서 **인원이 더 적은 선택지**를 고른 사람 전원에게 **+1점**이 지급됩니다.\n"
            "> ⚠️ 두 선택지의 인원이 같거나, 아무도 고르지 않은 쪽이 소수라면 점수는 지급되지 않습니다.\n"
            "> 🔄 재선택 시간 동안에는 선택을 몇 번이든 바꿀 수 있으며, 누르지 않으면 1차 선택으로 최종 집계됩니다."
        ),
        inline=False,
    )
    embed.add_field(
        name="⏳ 재선택 마감",
        value=f"**{close_time_text(close_at)}**",
        inline=False,
    )
    return embed


def start_minority_balance_revote(round_id: int) -> dict[str, Any] | None:
    """1차 표를 유지한 채 같은 라운드를 재선택 단계로 전환한다."""
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'balance' AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row:
            return None

        extra = round_extra(round_row)
        if extra.get("mode") != "minority_balance" or extra.get("balance_phase") != "initial":
            return None

        counts = {"A": 0, "B": 0}
        for vote in conn.execute(
            "SELECT choice FROM votes WHERE round_id = ?",
            (round_id,),
        ).fetchall():
            choice = str(vote["choice"])
            if choice in counts:
                counts[choice] += 1

        duration_minutes = max(1, int(extra.get("duration_minutes") or 1))
        close_at = time.time() + duration_minutes * 60
        extra.update(
            {
                "balance_phase": "revote",
                "first_tally": counts,
                "first_tallied_at": now_text(),
                "auto_close_at": close_at,
            }
        )
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )

        return {
            "round_id": round_id,
            "channel_id": str(round_row["channel_id"]),
            "discussion_thread_id": str(round_row["discussion_thread_id"] or ""),
            "question": str(round_row["question"]),
            "option_a": str(round_row["option_a"]),
            "option_b": str(round_row["option_b"]),
            "first_a_count": counts["A"],
            "first_b_count": counts["B"],
            "close_at": close_at,
        }


async def publish_minority_balance_revote(transition: dict[str, Any]) -> bool:
    """재선택 안내를 메인 채널에 게시하고 토론방의 고정 버튼도 새 마감으로 갱신한다."""
    round_id = int(transition["round_id"])
    close_at = float(transition["close_at"])
    channel = await runtime.resolve_text_channel(str(transition["channel_id"]))
    if channel is None:
        return False

    embed = build_minority_balance_revote_embed(
        transition,
        int(transition["first_a_count"]),
        int(transition["first_b_count"]),
        close_at,
    )
    try:
        await channel.send(embed=embed, view=vote_view(round_id, BALANCE_VOTE_CHOICES))
    except discord.HTTPException:
        return False

    thread_id = str(transition.get("discussion_thread_id") or "")
    if not thread_id:
        return True
    try:
        thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
    except (discord.HTTPException, ValueError):
        return True
    if not isinstance(thread, discord.Thread):
        return True

    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row:
            return True
        extra = round_extra(round_row)
        old_button_message_id = str(extra.get("anon_button_message_id") or "")
        post_count = count_discussion_messages(conn, round_id)

    if old_button_message_id:
        try:
            old_button = await thread.fetch_message(int(old_button_message_id))
            await old_button.delete()
        except (discord.NotFound, discord.HTTPException, ValueError):
            pass
    try:
        new_button = await thread.send(
            embed=build_anonymous_guide_embed(post_count, close_at),
            view=thread_discussion_view(round_id, "minority_balance"),
        )
    except discord.HTTPException:
        return True

    with db() as conn:
        fresh_row = conn.execute("SELECT extra_json FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if fresh_row:
            extra = round_extra(fresh_row)
            extra["anon_button_message_id"] = str(new_button.id)
            conn.execute(
                "UPDATE rounds SET extra_json = ? WHERE id = ?",
                (json.dumps(extra, ensure_ascii=False), round_id),
            )
    return True


def build_defuse_embed(round_id: int, title: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=title,
        color=discord.Color.dark_red(),
    )
    embed.add_field(
        name="🔌 선택 가능한 선",
        value="　　".join(f"{color['emoji']} {color['name']}" for color in DEFUSE_COLORS),
        inline=False,
    )
    embed.add_field(
        name="📜 규칙",
        value=(
            "> 🎯 빨강, 파랑, 초록 중 하나를 선택합니다.\n"
            "> 💥 마감 후 가장 많은 표를 받은 선이 폭발하며, 그 선을 고른 사람은 **-1점**입니다.\n"
            "> ⚖️ 최다 득표가 동률이면 공동 1위인 선이 모두 폭발합니다.\n"
            "> 🕶️ 해체 토론방에서는 익명으로 몇 번이든 심리전과 블러핑을 할 수 있습니다."
        ),
        inline=False,
    )
    return embed


def build_cutting_embed(round_id: int, title: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=title,
        color=discord.Color.teal(),
    )
    embed.add_field(
        name="📈 예측 단계",
        value=" │ ".join(f"{level}강화" for level in CUTTING_LEVEL_CHOICES),
        inline=False,
    )
    embed.add_field(
        name="📜 규칙",
        value=(
            "> 🎯 처음 실패하기 직전의 강화 단계를 맞히는 게임입니다.\n"
            "> 🔨 투표 집계가 끝나면 운영자가 직접 세공을 진행합니다."
        ),
        inline=False,
    )
    # 확률과 보상은 상수에서 만든다. 손으로 적어두면 상수를 바꿨을 때 안내문만 옛날 값으로 남는다.
    # 나란히 붙여서 "확률 대비 보상"을 한눈에 비교하게 한다.
    embed.add_field(
        name="🎲 강화 성공 확률",
        value=f"```\n{cutting_success_rate_text()}\n```",
        inline=True,
    )
    embed.add_field(
        name="🏆 정답 보상",
        value=f"```\n{cutting_reward_text()}\n```",
        inline=True,
    )
    return embed


def build_thread_notice_embed(round_id: int, thread: discord.Thread, title: str, guide: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=f"{thread.mention}\n{guide}",
        color=discord.Color.gold(),
    )
    embed.add_field(name="👉 참여 방법", value="위 스레드를 눌러 의견을 남겨주세요.", inline=False)
    return embed


def build_top3_embed(round_id: int, title: str, candidates: list[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=title,
        color=discord.Color.orange(),
    )
    medals = ["🥇", "🥈", "🥉"]
    candidate_lines = [
        f"> {medals[index - 1] if index <= len(medals) else '▫️'} "
        f"<@{candidate['user_id']}> ─ **{candidate['score']}점**"
        for index, candidate in enumerate(candidates, start=1)
    ]
    embed.add_field(name="👑 후보", value="\n".join(candidate_lines), inline=False)
    embed.add_field(
        name="📜 규칙",
        value=(
            "> 🗳️ 현재 점수가 가장 높은 3명과 `선택하지 않는다` 중 하나에 투표합니다.\n"
            "> 🚫 후보로 오른 3명은 투표할 수 없습니다.\n"
            "> 🏆 `선택하지 않는다` 득표수가 세 후보 득표수 합보다 많으면 `선택하지 않는다`를 고른 사람 전원이 **+1점**입니다.\n"
            "> 🎯 그렇지 않으면 세 후보 중 가장 많이 선택된 1명이 **-1점**입니다.\n"
            "> ⚠️ 아무도 투표하지 않으면 이 라운드는 무효 처리됩니다."
        ),
        inline=False,
    )
    return embed


def unique_number_reward(number: int) -> int:
    """1~5는 1점, 6~10은 2점, 11~15는 3점이다."""
    if not 1 <= number <= 15:
        raise ValueError("홍피겔만의 황금금고의 숫자는 1~15여야 합니다.")
    return ((number - 1) // 5) + 1


def build_unique_number_embed(round_id: int, title: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description="1부터 15까지 중 하나를 선택하세요.",
        color=discord.Color.purple(),
    )
    embed.add_field(
        name="📜 규칙",
        value=(
            "> 👥 같은 숫자를 고른 사람이 2명 이상이면 그 숫자의 선택자는 모두 제외됩니다.\n"
            "> 🏆 중복되지 않은 숫자 중 **가장 높은 숫자**를 고른 단 한 명만 해당 구간의 점수를 받습니다.\n"
            "> ❌ 중복되지 않은 숫자가 없으면 점수는 지급되지 않습니다.\n"
            "> 🕶️ 토론 스레드에서 익명으로 몇 번이든 심리전을 펼칠 수 있습니다."
        ),
        inline=False,
    )
    embed.add_field(
        name="🎯 배점",
        value="```\n1~5     +1점\n6~10    +2점\n11~15   +3점\n```",
        inline=False,
    )
    return embed


def build_trust_registration_embed(round_id: int, title: str, registration_minutes: int) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=title,
        color=discord.Color.dark_gold(),
    )
    embed.add_field(
        name="🎮 진행 게임",
        value="딜레마의 섬",
        inline=False,
    )
    embed.add_field(
        name="📜 메인 규칙",
        value=(
            "무작위로 매칭된 두 명의 참가자가 신뢰와 배신을 선택합니다.\n"
            "```\n"
            "상호 신뢰 시   : 각 +2점\n"
            "한명만 배신 시 : 배신자 +5점, 상대 0점\n"
            "동시 배신 시   : 각 -3점\n"
            "```\n"
            "선택은 마감 전까지 바꿀 수 있고, 끝까지 고르지 않으면 랜덤으로 배정됩니다."
        ),
        inline=False,
    )
    return embed


def vote_view(round_id: int, choices: list[tuple[str, str]]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for label, value in choices:
        view.add_item(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary,
                custom_id=f"eventbot:vote:{round_id}:{value}",
            )
        )
    return view


def trust_registration_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="참여 체크하기",
            emoji="✋",
            style=discord.ButtonStyle.success,
            custom_id=f"eventbot:trustjoin:{round_id}",
        )
    )
    return view


def build_trust_decision_embed(
    round_id: int,
    title: str,
    pairs: list[tuple[str, str]],
    unmatched_user_id: str | None,
    emergency_user_id: str | None = None,
    close_at: float | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=title,
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="🎲 랜덤 매칭 완료",
        value=f"총 **{len(pairs)}세트**의 비공개 협상방을 열었습니다. 각자 초대된 방에서 대화와 선택을 진행하세요.",
        inline=False,
    )
    if unmatched_user_id:
        embed.add_field(
            name="🚨 긴급 투입 대기",
            value=(
                f"<@{unmatched_user_id}>님, 참여자가 홀수라 짝이 없습니다. 잠시만 기다려주세요!\n"
                "> 아직 게임에 참여하지 않은 분이 아래 `긴급 투입하기` 버튼을 누르면 둘만의 방이 새로 열리고, "
                "전체 진행 시간이 **1분 연장**됩니다."
            ),
            inline=False,
        )
    if emergency_user_id:
        embed.add_field(
            name="🚨 긴급 투입",
            value=emergency_join_notice(emergency_user_id),
            inline=False,
        )
    embed.add_field(
        name="⚔️ 선택 규칙 (3분)",
        value=(
            "비공개 협상방에서 매칭된 짝과 대화한 뒤 `신뢰` 또는 `배신`을 선택하세요. 서로의 선택은 마감 후 해당 방에서 공개됩니다.\n"
            "> 선택은 마감 전까지 **몇 번이든 바꿀 수 있습니다.** 끝까지 고르지 않으면 랜덤으로 배정됩니다."
        ),
        inline=False,
    )
    if close_at:
        embed.add_field(
            name="⏳ 선택 마감",
            value=f"**{close_time_text(close_at)}**",
            inline=False,
        )
    return embed


def trust_decision_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="상대를 신뢰",
            emoji="🤝",
            style=discord.ButtonStyle.success,
            custom_id=f"eventbot:trustdecision:{round_id}:trust",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="상대를 배신",
            emoji="🗡️",
            style=discord.ButtonStyle.danger,
            custom_id=f"eventbot:trustdecision:{round_id}:betray",
        )
    )
    return view


def build_sand_registration_embed(round_id: int, title: str, registration_minutes: int) -> discord.Embed:
    embed = discord.Embed(title=title, description=title, color=discord.Color.from_rgb(218, 165, 90))
    embed.add_field(
        name="🎮 진행 게임",
        value="모래성 게임",
        inline=False,
    )
    embed.add_field(
        name="📜 메인 규칙",
        value=SAND_RULES_TEXT,
        inline=False,
    )
    return embed


def sand_registration_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="참여 체크하기",
            emoji="🏖️",
            style=discord.ButtonStyle.success,
            custom_id=f"eventbot:sandjoin:{round_id}",
        )
    )
    return view


def sand_pair_status_text(pair: dict[str, Any]) -> str:
    phase = str(pair.get("phase"))
    if phase == "number_selection":
        selected = int(pair.get("number_a") is not None) + int(pair.get("number_b") is not None)
        return f"숫자를 고르세요(번복 불가) : {selected}/2"
    if phase == "active":
        return (
            f"<@{pair['current_user_id']}> 차례 · 성공한 모래 덜기 "
            f"{int(pair.get('successful_removals', 0))}회"
        )
    if phase == "awaiting_quit_response":
        return f"<@{pair['current_user_id']}>님이 함께 포기/마지막 덜기 선택 중"
    return SAND_OUTCOME_LABELS.get(str(pair.get("outcome")), "종료")


def build_sand_pair_embed(title: str, pair: dict[str, Any], extra: dict[str, Any]) -> discord.Embed:
    """짝 하나의 비공개 방에 걸리는 진행판. 다른 짝의 정보는 담지 않는다."""
    embed = discord.Embed(
        title=f"{title} · 1:1 모래성",
        description=f"<@{pair['user_a_id']}> ↔ <@{pair['user_b_id']}>",
        color=discord.Color.from_rgb(218, 165, 90),
    )
    embed.add_field(name="⚔️ 진행 상황", value=sand_pair_status_text(pair), inline=False)
    embed.add_field(name="📜 규칙", value=SAND_RULES_TEXT, inline=False)
    if str(pair.get("phase")) != "resolved":
        embed.add_field(
            name="⏳ 제한 시간",
            value=(
                f"예정 마감: **{close_time_text(extra.get('play_close_at'))}**\n"
                "> 시간 안에 끝내지 못하면 남은 숫자와 행동이 랜덤으로 배정됩니다."
            ),
            inline=False,
        )
    return embed


def build_sand_pair_result_embed(title: str, pair: dict[str, Any]) -> discord.Embed:
    number_a = pair.get("number_a")
    number_b = pair.get("number_b")
    embed = discord.Embed(
        title=f"1:1 결과 · {title}",
        description=f"<@{pair['user_a_id']}> ↔ <@{pair['user_b_id']}>",
        color=discord.Color.from_rgb(218, 165, 90),
    )
    if number_a is not None and number_b is not None:
        embed.add_field(
            name="🔢 서로의 숫자",
            value=(
                f"<@{pair['user_a_id']}>: **{number_a}**\n"
                f"<@{pair['user_b_id']}>: **{number_b}**\n"
                f"> 차이 **{abs(int(number_a) - int(number_b))}** → "
                f"**{abs(int(number_a) - int(number_b)) + 1}번째** 덜기에서 붕괴"
            ),
            inline=False,
        )
    embed.add_field(
        name="🏁 결과",
        value=f"**{SAND_OUTCOME_LABELS.get(str(pair.get('outcome')), '미완료')}**",
        inline=False,
    )
    embed.add_field(name="💰 정산", value=sand_pair_settlement_text(pair), inline=False)
    if pair.get("auto_completed"):
        embed.add_field(name="🎲 자동 진행", value="제한 시간이 끝나 남은 선택이 랜덤으로 배정되었습니다.", inline=False)
    return embed


def build_sand_play_embed(round_row: sqlite3.Row, extra: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title=str(round_row["title"]),
        description="각자 초대된 1:1 비공개 방에서 숫자와 행동을 선택하세요. 선택 내용은 비공개입니다.",
        color=discord.Color.from_rgb(218, 165, 90),
    )
    lines = [
        f"**{index}.** <@{pair['user_a_id']}> ↔ <@{pair['user_b_id']}>\n"
        f"　└ {sand_pair_status_text(pair)}"
        for index, pair in enumerate(extra.get("sand_pairs", []), start=1)
    ]
    embed.add_field(name="⚔️ 1:1 진행 현황", value=field_text(lines), inline=False)
    unmatched_user_id = str(extra.get("unmatched_user_id") or "")
    if unmatched_user_id:
        embed.add_field(
            name="🚨 긴급 투입 대기",
            value=(
                f"<@{unmatched_user_id}>님, 참여자가 홀수라 짝이 없습니다. 잠시만 기다려주세요!\n"
                "> 아직 게임에 참여하지 않은 분이 아래 `긴급 투입하기` 버튼을 누르면 둘만의 방이 새로 열리고, "
                "전체 진행 시간이 **1분 연장**됩니다."
            ),
            inline=False,
        )
    emergency_user_id = str(extra.get("emergency_user_id") or "")
    if emergency_user_id:
        embed.add_field(
            name="🚨 긴급 투입",
            value=emergency_join_notice(emergency_user_id),
            inline=False,
        )
    embed.add_field(
        name="⏳ 선택 제한 시간",
        value=(
            f"예정 마감: **{close_time_text(extra.get('play_close_at'))}**\n"
            "> 시간 안에 끝내지 못하면 남은 숫자와 행동이 랜덤으로 배정되어 자동으로 결과가 확정됩니다."
        ),
        inline=False,
    )
    return embed


def sand_play_view(round_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for index, value in enumerate(SAND_NUMBER_CHOICES):
        view.add_item(
            discord.ui.Button(
                label=value,
                style=discord.ButtonStyle.secondary,
                custom_id=f"eventbot:sandnumber:{round_id}:{value}",
                disabled=disabled,
                row=index // 5,
            )
        )
    view.add_item(
        discord.ui.Button(
            label="포기하기",
            emoji="🏳️",
            style=discord.ButtonStyle.danger,
            custom_id=f"eventbot:sandaction:{round_id}:quit",
            disabled=disabled,
            row=2,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="모래 덜기",
            emoji="🏖️",
            style=discord.ButtonStyle.primary,
            custom_id=f"eventbot:sandaction:{round_id}:remove",
            disabled=disabled,
            row=2,
        )
    )
    return view


def defuse_vote_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for color in DEFUSE_COLORS:
        view.add_item(
            discord.ui.Button(
                emoji=color["emoji"],
                style=discord.ButtonStyle.secondary,
                custom_id=f"eventbot:vote:{round_id}:{color['id']}",
            )
        )
    return view


def top3_vote_view(round_id: int, candidates: list[dict[str, Any]]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for index, candidate in enumerate(candidates, start=1):
        label = f"{index}위 {candidate['display_name']}"
        if len(label) > 80:
            label = label[:77] + "..."
        view.add_item(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.danger,
                custom_id=f"eventbot:vote:{round_id}:{candidate['user_id']}",
            )
        )
    view.add_item(
        discord.ui.Button(
            label="선택하지 않는다",
            style=discord.ButtonStyle.secondary,
            custom_id=f"eventbot:vote:{round_id}:{TOP3_NONE_CHOICE}",
        )
    )
    return view


def cutting_vote_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for index, value in enumerate(CUTTING_LEVEL_CHOICES):
        view.add_item(
            discord.ui.Button(
                label=f"{value}강화",
                style=discord.ButtonStyle.secondary,
                custom_id=f"eventbot:vote:{round_id}:{value}",
                row=0 if index < 4 else 1,
            )
        )
    return view


def unique_number_vote_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for index, value in enumerate(UNIQUE_NUMBER_CHOICES):
        number = int(value)
        view.add_item(
            discord.ui.Button(
                label=value,
                style=(
                    discord.ButtonStyle.secondary
                    if number <= 5
                    else discord.ButtonStyle.primary
                    if number <= 10
                    else discord.ButtonStyle.success
                ),
                custom_id=f"eventbot:vote:{round_id}:{value}",
                row=index // 5,
            )
        )
    return view


def cutting_action_view(round_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="세공 시도",
            style=discord.ButtonStyle.primary,
            custom_id=f"eventbot:cutting:{round_id}",
            disabled=disabled,
        )
    )
    return view


def auto_close_action_label(round_row: sqlite3.Row | dict[str, Any]) -> str:
    extra = round_extra(round_row)  # type: ignore[arg-type]
    if round_row["type"] == "trust" and extra.get("trust_phase") == "registration":
        return "참가 모집 종료 및 랜덤 매칭"
    if round_row["type"] == "sand" and extra.get("sand_phase") == "registration":
        return "참가 모집 종료 및 모래성 게임 시작"
    if extra.get("mode") == "bail":
        if extra.get("bail_phase") == "registration":
            return "참가 모집 종료 및 협상 시작"
        if extra.get("bail_phase") == "negotiation":
            return "협상 종료 및 실행 단계 시작"
    if extra.get("mode") == "minority_balance" and extra.get("balance_phase") == "revote":
        return "재선택 종료"
    game_key = round_game_key(round_row)  # type: ignore[arg-type]
    return f"{GAME_NAMES.get(game_key, game_key)} 종료"


async def publish_auto_close_countdown(
    channel: Any,
    action_label: str,
    close_at: float,
) -> None:
    """10초 전 공지를 남기고, 별도 메시지를 5→1 카운트다운으로 갱신한다."""
    remaining = close_at - time.time()
    if remaining <= 0 or not hasattr(channel, "send"):
        return

    warning_seconds = min(AUTO_CLOSE_WARNING_SECONDS, max(1, math.ceil(remaining)))
    warning_message = None
    countdown_message = None
    try:
        try:
            warning_message = await channel.send(
                f"⏳ **{action_label} {warning_seconds}초 전입니다.**"
            )
        except discord.HTTPException:
            warning_message = None

        for seconds in AUTO_CLOSE_COUNTDOWN_SECONDS:
            wait_seconds = close_at - time.time() - seconds
            if wait_seconds <= 0:
                continue
            await asyncio.sleep(wait_seconds)
            content = f"⏳ **{action_label}까지 {seconds}**"
            if countdown_message is None:
                try:
                    countdown_message = await channel.send(content)
                except discord.HTTPException:
                    countdown_message = None
            else:
                try:
                    await countdown_message.edit(content=content)
                except discord.HTTPException:
                    countdown_message = None

        await asyncio.sleep(max(0, close_at - time.time()))
        if countdown_message is not None:
            try:
                await countdown_message.edit(content=f"🏁 **{action_label}!**")
            except discord.HTTPException:
                pass
    except asyncio.CancelledError:
        # 수동 마감·긴급 연장으로 기존 타이머가 취소되면 낡은 두 메시지를 모두 치운다.
        for message in (warning_message, countdown_message):
            if message is None:
                continue
            try:
                await message.delete()
            except discord.HTTPException:
                pass
        raise


def cutting_progress_bar(current_level: int, max_level: int) -> str:
    """강화 진척도 막대. 초록(🟩)은 성공한 강화, 흰색(◻️)은 남은 강화. 예: 3강 → 🟩🟩🟩◻️◻️◻️"""
    filled = max(0, min(current_level, max_level))
    return "🟩" * filled + "◻️" * (max_level - filled)


def build_cutting_progress_embed(round_row: sqlite3.Row, extra: dict[str, Any]) -> discord.Embed:
    current_level = int(extra.get("cutting_current_level", 0))
    max_level = int(extra.get("max_level", 6))
    success_rate = cutting_success_rate(current_level) if current_level < max_level else None
    logs = [str(log) for log in extra.get("cutting_logs", [])]

    embed = discord.Embed(
        title=round_row["title"],
        description=round_row["title"],
        color=discord.Color.teal(),
    )
    embed.add_field(name="📍 현재 단계", value=f"**{current_level}강화**", inline=True)
    embed.add_field(name="🔝 최대 단계", value=f"**{max_level}강화**", inline=True)
    embed.add_field(
        name="🎲 다음 시도 확률",
        value=f"**{success_rate * 100:.0f}%**" if success_rate is not None else "최대 단계 도달",
        inline=True,
    )
    embed.add_field(
        name="📊 진척도",
        value=f"{cutting_progress_bar(current_level, max_level)}　　**{current_level}** / {max_level}강",
        inline=False,
    )
    embed.add_field(
        name="🔨 직전 시도",
        value=logs[-1] if logs else "아직 시도 없음",
        inline=False,
    )
    embed.add_field(
        name="ℹ️ 진행 방식",
        value="관리자가 아래 `세공 시도` 버튼을 눌러 실패할 때까지 직접 진행합니다.",
        inline=False,
    )
    return embed


def finalize_cutting_round(
    conn: sqlite3.Connection,
    round_row: sqlite3.Row,
    extra: dict[str, Any],
    result_level: int,
    logs: list[str],
    guild: discord.Guild | None,
) -> discord.Embed:
    reward_points = CUTTING_REWARD_POINTS[result_level]
    votes = conn.execute(
        "SELECT user_id, choice FROM votes WHERE round_id = ? ORDER BY updated_at ASC",
        (round_row["id"],),
    ).fetchall()
    correct_users = [row["user_id"] for row in votes if row["choice"] == str(result_level)]
    for user_id in correct_users:
        add_score(
            conn,
            user_id,
            member_name(guild, user_id),
            reward_points,
            f"라운드 #{round_row['id']} 돌 세공 {result_level}강화 예측",
            round_row["id"],
        )

    grouped: dict[str, list[str]] = {}
    for row in votes:
        grouped.setdefault(row["choice"], []).append(row["user_id"])

    extra.update(
        {
            "cutting_current_level": result_level,
            "cutting_result_level": result_level,
            "cutting_logs": logs,
            "cutting_resolved_at": now_text(),
        }
    )
    conn.execute(
        "UPDATE rounds SET extra_json = ? WHERE id = ?",
        (json.dumps(extra, ensure_ascii=False), round_row["id"]),
    )

    prediction_lines = [
        f"{level}강화 ────── {len(grouped.get(level, []))}명"
        for level in CUTTING_LEVEL_CHOICES
    ]
    embed = discord.Embed(
        title=f"결과 · {round_row['title']}",
        description=round_row["title"],
        color=discord.Color.green(),
    )
    max_level = int(extra.get("max_level", 6))
    embed.add_field(
        name="📊 진척도",
        value=f"{cutting_progress_bar(result_level, max_level)}　　**{result_level}** / {max_level}강",
        inline=False,
    )
    embed.add_field(name="🔨 마지막 시도", value=logs[-1] if logs else "-", inline=False)
    embed.add_field(name="🏁 최종 단계", value=f"**{result_level}강화**", inline=True)
    embed.add_field(name="🎯 정답자", value=f"**{len(correct_users)}명**", inline=True)
    embed.add_field(name="📈 예측 분포", value="```\n" + "\n".join(prediction_lines) + "\n```", inline=False)
    embed.add_field(name="👥 정답자 명단", value=mention_list(correct_users), inline=False)
    embed.add_field(
        name="💰 정산",
        value=f"{result_level}강화를 맞힌 **{len(correct_users)}명** 전원 **+{reward_points}점**",
        inline=False,
    )
    return embed


async def handle_cutting_button(interaction: discord.Interaction, round_id: int) -> None:
    if not is_operator_user(interaction.user):
        await interaction.response.send_message("이 버튼은 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'cutting'",
            (round_id,),
        ).fetchone()
        if not round_row:
            await interaction.response.send_message("존재하지 않는 돌 세공 라운드입니다.", ephemeral=True)
            return
        if round_row["status"] != "closed":
            await interaction.response.send_message("투표 집계를 먼저 마감한 뒤 진행해주세요.", ephemeral=True)
            return

        extra = round_extra(round_row)
        if "cutting_result_level" in extra:
            await interaction.response.send_message("이미 세공이 종료된 라운드입니다.", ephemeral=True)
            return

        max_level = int(extra.get("max_level", 6))
        current_level = int(extra.get("cutting_current_level", 0))
        logs = [str(log) for log in extra.get("cutting_logs", [])]

        if current_level >= max_level:
            logs.append(f"{max_level}강까지 성공, 세공 종료")
            embed = finalize_cutting_round(conn, round_row, extra, max_level, logs, interaction.guild)
            await interaction.response.edit_message(embed=embed, view=cutting_action_view(round_id, disabled=True))
            conn.commit()
            await publish_event_summary_if_complete(round_row["event_session_id"])
            return

        next_level = current_level + 1
        success_rate = cutting_success_rate(current_level)
        if random.random() < success_rate:
            current_level = next_level
            logs.append(f"{next_level}강 시도: 성공")
            if current_level >= max_level:
                logs.append(f"{max_level}강까지 성공, 세공 종료")
                embed = finalize_cutting_round(conn, round_row, extra, current_level, logs, interaction.guild)
                await interaction.response.edit_message(embed=embed, view=cutting_action_view(round_id, disabled=True))
                conn.commit()
                await publish_event_summary_if_complete(round_row["event_session_id"])
                return

            extra.update(
                {
                    "cutting_current_level": current_level,
                    "cutting_logs": logs,
                    "cutting_updated_at": now_text(),
                }
            )
            conn.execute(
                "UPDATE rounds SET extra_json = ? WHERE id = ?",
                (json.dumps(extra, ensure_ascii=False), round_row["id"]),
            )
            embed = build_cutting_progress_embed(round_row, extra)
            await interaction.response.edit_message(embed=embed, view=cutting_action_view(round_id))
            return

        logs.append(f"{next_level}강 시도: 실패, {current_level}강화에서 종료")
        embed = finalize_cutting_round(conn, round_row, extra, current_level, logs, interaction.guild)
        await interaction.response.edit_message(embed=embed, view=cutting_action_view(round_id, disabled=True))
        conn.commit()
        await publish_event_summary_if_complete(round_row["event_session_id"])


register_settler("bail", settle_bail)


def close_round_record(
    round_id: int,
    guild: discord.Guild | None,
    closed_by: str = "수동 마감",
) -> tuple[str, discord.Embed] | None:
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row or round_row["status"] != "open":
            return None

        extra = round_extra(round_row)
        # 이 모드는 1차 집계에서 닫히면 안 된다. 호출자는 먼저 재선택 단계로 전환해야 한다.
        if (
            round_row["type"] == "balance"
            and extra.get("mode") == "minority_balance"
            and extra.get("balance_phase") != "revote"
        ):
            return None

        votes = conn.execute(
            "SELECT user_id, choice FROM votes WHERE round_id = ? ORDER BY updated_at ASC",
            (round_row["id"],),
        ).fetchall()
        conn.execute(
            "UPDATE rounds SET status = 'closed', closed_at = ? WHERE id = ?",
            (now_text(), round_row["id"]),
        )

        # 레지스트리에 정산 훅이 올라온 게임은 그쪽으로 보낸다. 아직 안 올라온 게임은
        # 아래 기존 분기로 떨어진다 — 게임을 하나씩 옮기는 동안 둘이 공존한다.
        settler = settler_for(round_game_key(round_row))
        if settler is not None:
            embed = settler(
                SettleContext(
                    conn=conn,
                    round_row=round_row,
                    extra=extra,
                    votes=votes,
                    guild=guild,
                )
            )
        elif round_row["type"] == "balance":
            a_users = [row["user_id"] for row in votes if row["choice"] == "A"]
            b_users = [row["user_id"] for row in votes if row["choice"] == "B"]

            scored = False
            is_minority_balance = extra.get("mode") == "minority_balance"
            if is_minority_balance:
                if not a_users and not b_users:
                    settlement = "재선택한 사람이 없어 점수가 지급되지 않았습니다."
                elif not a_users or not b_users:
                    settlement = "인원이 더 적은 쪽에 선택자가 없어 점수가 지급되지 않았습니다."
                elif len(a_users) == len(b_users):
                    settlement = "두 선택지의 인원이 같아 점수가 지급되지 않았습니다."
                else:
                    winner = "A" if len(a_users) < len(b_users) else "B"
                    winner_users = a_users if winner == "A" else b_users
                    for user_id in winner_users:
                        add_score(
                            conn,
                            user_id,
                            member_name(guild, user_id),
                            1,
                            f"라운드 #{round_row['id']} 소수파 선택",
                            round_row["id"],
                        )
                    scored = True
                    settlement = f"소수파 `{winner}` 선택자 {len(winner_users)}명 전원 +1점"
            else:
                if not a_users and not b_users:
                    settlement = "투표가 없어 점수가 지급되지 않았습니다."
                elif not a_users or not b_users:
                    # 한쪽으로 몰표. 밸런스가 성립하지 않았으므로 고른 사람 전원에게 패널티.
                    one_sided_users = a_users or b_users
                    one_sided_choice = "A" if a_users else "B"
                    for user_id in one_sided_users:
                        add_score(
                            conn,
                            user_id,
                            member_name(guild, user_id),
                            -2,
                            f"라운드 #{round_row['id']} 몰표 패널티",
                            round_row["id"],
                        )
                    scored = True
                    settlement = (
                        f"모두 `{one_sided_choice}`만 골라 밸런스가 성립하지 않았습니다. "
                        f"선택자 {len(one_sided_users)}명 전원 -2점"
                    )
                elif len(a_users) == len(b_users):
                    settlement = "무승부라 점수가 지급되지 않았습니다."
                else:
                    winner = "A" if len(a_users) > len(b_users) else "B"
                    winner_users = a_users if winner == "A" else b_users
                    minority = "B" if winner == "A" else "A"
                    minority_users = b_users if winner == "A" else a_users
                    if len(minority_users) == 1:
                        minority_user_id = minority_users[0]
                        add_score(
                            conn,
                            minority_user_id,
                            member_name(guild, minority_user_id),
                            2,
                            f"라운드 #{round_row['id']} 단독 소수파 선택",
                            round_row["id"],
                        )
                    else:
                        for user_id in winner_users:
                            add_score(
                                conn,
                                user_id,
                                member_name(guild, user_id),
                                1,
                                f"라운드 #{round_row['id']} 다수파 선택",
                                round_row["id"],
                            )
                    scored = True
                    if len(minority_users) == 1:
                        settlement = (
                            f"소수파 `{minority}` 단독 선택자만 +2점 · "
                            f"다수파 `{winner}` 선택자는 점수 없음"
                        )
                    else:
                        settlement = f"다수파 `{winner}` 선택자 전원 +1점"

            embed = discord.Embed(
                title=(
                    f"최종 결과 · {round_row['title']}"
                    if is_minority_balance
                    else f"결과 · {round_row['title']}"
                ),
                description=round_row["question"],
                color=discord.Color.green() if scored else discord.Color.light_grey(),
            )
            # 누가 무엇을 골랐는지는 공개하지 않는다. 인원수만.
            embed.add_field(
                name=f"🅰️ {round_row['option_a']}",
                value=f"**{len(a_users)}명**",
                inline=True,
            )
            embed.add_field(
                name=f"🅱️ {round_row['option_b']}",
                value=f"**{len(b_users)}명**",
                inline=True,
            )
            embed.add_field(name="💰 정산", value=settlement, inline=False)
        elif round_row["type"] == "bomb":
            wires: dict[str, str] = extra["wires"]
            grouped: dict[str, list[str]] = {}
            for row in votes:
                if row["choice"] in wires:
                    grouped.setdefault(row["choice"], []).append(row["user_id"])

            if extra.get("settlement_rule") == "top_vote_penalty":
                highest_vote_count = max((len(users) for users in grouped.values()), default=0)
                exploded_wire_ids = {
                    wire_id
                    for wire_id, users in grouped.items()
                    if highest_vote_count > 0 and len(users) == highest_vote_count
                }
                penalized_users = [
                    user_id
                    for wire_id in exploded_wire_ids
                    for user_id in grouped.get(wire_id, [])
                ]
                for user_id in penalized_users:
                    add_score(
                        conn,
                        user_id,
                        member_name(guild, user_id),
                        -1,
                        f"라운드 #{round_row['id']} 최다 득표 선 선택",
                        round_row["id"],
                    )

                embed = discord.Embed(
                    title=f"결과 · {round_row['title']}",
                    description=round_row["title"],
                    color=discord.Color.red() if penalized_users else discord.Color.light_grey(),
                )
                exploded_names = ", ".join(
                    wires[wire_id] for wire_id in wires if wire_id in exploded_wire_ids
                )
                embed.add_field(
                    name="💥 폭발한 선",
                    value=f"**{exploded_names}**" if exploded_wire_ids else "없음 (투표 없음)",
                    inline=False,
                )
                add_field_divider(embed)
                # 세 선을 inline 필드로 나란히(가로) 배치한다. 폭발선은 위 헤드라인이 알려주므로
                # 이름은 색만, 값은 선택자 멘션을 세로로 쌓아 칸을 좁게 유지한다(모바일에서 가로로 잘 붙게).
                for wire_id, wire_name in wires.items():
                    users = grouped.get(wire_id, [])
                    embed.add_field(
                        name=wire_name,
                        value=mention_list(users, sep="\n"),
                        inline=True,
                    )
                add_field_divider(embed)

                if not penalized_users:
                    settlement = "투표가 없어 점수 변동이 없습니다."
                elif len(exploded_wire_ids) == 1:
                    settlement = (
                        f"최다 득표 선을 고른 {len(penalized_users)}명 전원 -1점"
                    )
                else:
                    settlement = (
                        f"최다 득표 {highest_vote_count}표 동률로 공동 1위 "
                        f"{len(exploded_wire_ids)}개 선이 모두 폭발했습니다. "
                        f"선택자 {len(penalized_users)}명 전원 -1점"
                    )
                embed.add_field(
                    name="💰 정산",
                    value=settlement,
                    inline=False,
                )
            else:
                # 배포 시점에 이미 열려 있던 구방식 라운드는 기존 안전선 규칙으로 마감한다.
                safe_wire_ids = set(extra["safe_wire_ids"])
                safe_users = [
                    row["user_id"] for row in votes if row["choice"] in safe_wire_ids
                ]
                is_defuse = extra.get("mode") == "defuse4"
                defuse_hint = str(extra.get("hint", "힌트 없음"))

                for user_id in safe_users:
                    add_score(
                        conn,
                        user_id,
                        member_name(guild, user_id),
                        1,
                        f"라운드 #{round_row['id']} 안전선 선택",
                        round_row["id"],
                    )

                embed = discord.Embed(
                    title=f"결과 · {round_row['title']}",
                    description=round_row["title"],
                    color=discord.Color.green(),
                )
                embed.add_field(
                    name="해체 성공 선" if is_defuse else "안전선",
                    value=", ".join(wires[wire_id] for wire_id in extra["safe_wire_ids"]),
                    inline=False,
                )
                if is_defuse:
                    embed.add_field(name="관리자의 힌트", value=defuse_hint, inline=False)
                for wire_id, wire_name in wires.items():
                    users = grouped.get(wire_id, [])
                    marker = "안전" if wire_id in safe_wire_ids else "폭탄"
                    embed.add_field(
                        name=f"{wire_name} ({marker})",
                        value=f"{len(users)}명\n{mention_list(users)}",
                        inline=False,
                    )
                embed.add_field(
                    name="정산",
                    value=(
                        f"{'해체 성공자' if is_defuse else '안전선 선택자'} "
                        f"{len(safe_users)}명 전원 +1점"
                    ),
                    inline=False,
                )
        elif round_row["type"] == "grid" and extra.get("mode") == "unique_number":
            grouped: dict[int, list[str]] = {}
            for row in votes:
                if str(row["choice"]) not in UNIQUE_NUMBER_CHOICES:
                    continue
                grouped.setdefault(int(row["choice"]), []).append(str(row["user_id"]))

            unique_numbers = [number for number, users in grouped.items() if len(users) == 1]
            winning_number = max(unique_numbers, default=None)
            winner_id = grouped[winning_number][0] if winning_number is not None else None
            reward = unique_number_reward(winning_number) if winning_number is not None else 0

            if winner_id is not None and winning_number is not None:
                add_score(
                    conn,
                    winner_id,
                    member_name(guild, winner_id),
                    reward,
                    f"라운드 #{round_row['id']} 최고 유일 숫자 {winning_number}",
                    round_row["id"],
                )

            # 마커 이모지+라벨을 줄 맨 앞에 둬서 우승/중복/유일이 한눈에 구분되게 한다.
            result_lines = []
            for number in sorted(grouped, reverse=True):
                users = grouped[number]
                if number == winning_number:
                    result_lines.append(
                        f"🏆최고 유일 숫자 : {number} · {mention_list(users)}"
                    )
                elif len(users) > 1:
                    result_lines.append(
                        f"❌중복 숫자 선택 : {number} · {mention_list(users)}"
                    )
                else:
                    result_lines.append(
                        f"▫️유일하기만 한 숫자 : {number} · {mention_list(users)}"
                    )

            embed = discord.Embed(
                title=f"결과 · {round_row['title']}",
                description=round_row["title"],
                color=discord.Color.green() if winner_id else discord.Color.light_grey(),
            )
            embed.add_field(
                name="🔢 숫자별 결과",
                value=field_text(result_lines, empty="선택한 사람이 없습니다."),
                inline=False,
            )
            add_field_divider(embed)
            if winner_id is None:
                settlement = (
                    "선택한 사람이 없어 점수가 지급되지 않았습니다."
                    if not grouped
                    else "모든 선택 숫자가 중복되어 점수가 지급되지 않았습니다."
                )
            else:
                settlement = f"<@{winner_id}>님이 최고 유일 숫자 **{winning_number}**으로 **+{reward}점**"
            embed.add_field(name="💰 정산", value=settlement, inline=False)
        elif round_row["type"] == "trust":
            vote_choices = {str(row["user_id"]): str(row["choice"]) for row in votes}
            pairs = conn.execute(
                """
                SELECT user_a_id, user_b_id FROM trust_pairings
                WHERE round_id = ?
                ORDER BY created_at ASC
                """,
                (round_row["id"],),
            ).fetchall()
            outcome_counts = {choice: 0 for choice in TRUST_OUTCOME_LABELS}

            # 마감까지 안 고른 사람은 랜덤 배정한다. 미선택으로 짝 전체가 0점이 되는 일이 없도록.
            # 협상방 결과 공개(publish_trust_pair_results)가 votes를 다시 읽으므로 DB에도 남긴다.
            auto_assigned = 0
            for pair in pairs:
                for user_id in (str(pair["user_a_id"]), str(pair["user_b_id"])):
                    if vote_choices.get(user_id):
                        continue
                    assigned = random.choice(["trust", "betray"])
                    ts = now_text()
                    conn.execute(
                        """
                        INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(round_id, user_id) DO UPDATE SET
                            choice = excluded.choice,
                            updated_at = excluded.updated_at
                        """,
                        (round_row["id"], user_id, assigned, ts, ts),
                    )
                    # 본인이 고른 표와 섞이면 안 된다. votes만 보면 구분이 안 되므로 여기서 남긴다.
                    log_activity(
                        conn,
                        "vote",
                        "auto_assigned",
                        event_session_id=round_row["event_session_id"],
                        round_id=round_row["id"],
                        actor_user_id=user_id,
                        payload={"choice": assigned, "reason": "마감까지 미선택"},
                    )
                    vote_choices[user_id] = assigned
                    auto_assigned += 1

            for pair in pairs:
                user_a_id = str(pair["user_a_id"])
                user_b_id = str(pair["user_b_id"])
                user_a_choice = vote_choices.get(user_a_id)
                user_b_choice = vote_choices.get(user_b_id)
                if not user_a_choice or not user_b_choice:
                    outcome_counts["incomplete"] += 1
                    continue

                if user_a_choice == "trust" and user_b_choice == "trust":
                    outcome = "mutual"
                    for user_id in (user_a_id, user_b_id):
                        add_score(
                            conn,
                            user_id,
                            member_name(guild, user_id),
                            2,
                            f"라운드 #{round_row['id']} 딜레마의 섬 상호 신뢰",
                            round_row["id"],
                        )
                elif user_a_choice == "betray" and user_b_choice == "betray":
                    outcome = "double"
                    for user_id in (user_a_id, user_b_id):
                        add_score(
                            conn,
                            user_id,
                            member_name(guild, user_id),
                            -3,
                            f"라운드 #{round_row['id']} 딜레마의 섬 동시 배신",
                            round_row["id"],
                        )
                else:
                    outcome = "single"
                    betrayer_id = user_a_id if user_a_choice == "betray" else user_b_id
                    add_score(
                        conn,
                        betrayer_id,
                        member_name(guild, betrayer_id),
                        5,
                        f"라운드 #{round_row['id']} 딜레마의 섬 단독 배신",
                        round_row["id"],
                    )
                outcome_counts[outcome] += 1

            extra.update({"trust_phase": "scored", "trust_scored_at": now_text()})
            conn.execute(
                "UPDATE rounds SET extra_json = ? WHERE id = ?",
                (json.dumps(extra, ensure_ascii=False), round_row["id"]),
            )
            embed = discord.Embed(
                title=f"결과 · {round_row['title']}",
                description=round_row["title"],
                color=discord.Color.gold(),
            )
            # 라벨 폭을 한글 2칸 기준으로 맞춰 코드블록 안에서 콜론이 세로로 정렬되게 한다.
            distribution_lines = [
                f"신뢰      : {outcome_counts['mutual']}",
                f"단독 배신 : {outcome_counts['single']}",
                f"동시 배신 : {outcome_counts['double']}",
            ]
            if outcome_counts["incomplete"]:
                distribution_lines.append(f"미선택    : {outcome_counts['incomplete']}")
            embed.add_field(
                name="📊 전체 결과 분포",
                value="```\n" + "\n".join(distribution_lines) + "\n```",
                inline=False,
            )
            if auto_assigned:
                embed.add_field(
                    name="🎲 미선택 랜덤 배정",
                    value=f"**{auto_assigned}명**",
                    inline=False,
                )
            unmatched_user_id = str(extra.get("unmatched_user_id") or "")
            if unmatched_user_id:
                embed.add_field(
                    name="⏳ 매칭 대기",
                    value="**1명**",
                    inline=False,
                )
            embed.add_field(
                name="💰 정산",
                value="상호 신뢰는 각 +2점, 단독 배신은 배신자 +5점·상대 0점, 동시 배신은 각 -3점입니다.",
                inline=False,
            )
        elif round_row["type"] == "sand":
            result_lines: list[str] = []
            completed_count = 0
            auto_completed_count = 0
            # 마감 시점에 안 끝난 짝은 랜덤으로 끝까지 진행시킨다. 미완료 0점은 남기지 않는다.
            for pair in extra.get("sand_pairs", []):
                if str(pair.get("phase")) != "resolved":
                    autocomplete_sand_pair(pair)
                    auto_completed_count += 1

            for pair in extra.get("sand_pairs", []):
                user_a_id = str(pair["user_a_id"])
                user_b_id = str(pair["user_b_id"])
                outcome = str(pair.get("outcome") or "")
                result = pair.get("result", {})
                number_a = pair.get("number_a")
                number_b = pair.get("number_b")
                number_text = (
                    f"선택 {number_a}/{number_b}, 차이 {abs(int(number_a) - int(number_b))}"
                    if number_a is not None and number_b is not None
                    else "숫자 선택 미완료"
                )

                if outcome == "mutual_quit":
                    for user_id in (user_a_id, user_b_id):
                        add_score(
                            conn, user_id, member_name(guild, user_id), -3,
                            f"라운드 #{round_row['id']} 모래성 둘 다 포기", round_row["id"],
                        )
                elif outcome == "quit_success":
                    winner_id = str(result["winner"])
                    quitter_id = str(result["quitter"])
                    add_score(
                        conn, winner_id, member_name(guild, winner_id), 5,
                        f"라운드 #{round_row['id']} 모래성 포기 후 덜기 성공", round_row["id"],
                    )
                    add_score(
                        conn, quitter_id, member_name(guild, quitter_id), -3,
                        f"라운드 #{round_row['id']} 모래성 단독 포기", round_row["id"],
                    )
                elif outcome == "quit_collapse":
                    winner_id = str(result["winner"])
                    collapsed_user_id = str(result["collapsed_user"])
                    add_score(
                        conn, winner_id, member_name(guild, winner_id), 2,
                        f"라운드 #{round_row['id']} 모래성 상대 붕괴", round_row["id"],
                    )
                    add_score(
                        conn, collapsed_user_id, member_name(guild, collapsed_user_id), -3,
                        f"라운드 #{round_row['id']} 모래성 마지막 덜기 붕괴", round_row["id"],
                    )
                elif outcome == "plain_collapse":
                    collapsed_user_id = str(result["collapsed_user"])
                    add_score(
                        conn, collapsed_user_id, member_name(guild, collapsed_user_id), -3,
                        f"라운드 #{round_row['id']} 모래성 일반 붕괴", round_row["id"],
                    )
                else:
                    result_lines.append(
                        f"**{len(result_lines) + 1}.** <@{user_a_id}> ↔ <@{user_b_id}>\n"
                        f"　└ 제한 시간 내 미완료 (0점)"
                    )
                    continue

                completed_count += 1
                auto_mark = " · 자동 진행" if pair.get("auto_completed") else ""
                result_lines.append(
                    f"**{len(result_lines) + 1}.** <@{user_a_id}> ↔ <@{user_b_id}>\n"
                    f"　└ **{SAND_OUTCOME_LABELS[outcome]}** "
                    f"({number_text}) · {sand_pair_settlement_text(pair)}{auto_mark}"
                )

            extra.update({"sand_phase": "scored", "sand_scored_at": now_text()})
            conn.execute(
                "UPDATE rounds SET extra_json = ? WHERE id = ?",
                (json.dumps(extra, ensure_ascii=False), round_row["id"]),
            )
            embed = discord.Embed(
                title=f"결과 · {round_row['title']}",
                description=round_row["title"],
                color=discord.Color.from_rgb(218, 165, 90),
            )
            embed.add_field(name="⚔️ 짝별 결과", value=field_text(result_lines), inline=False)
            unmatched_user_id = str(extra.get("unmatched_user_id") or "")
            if unmatched_user_id:
                embed.add_field(
                    name="⏳ 매칭 대기",
                    value=f"<@{unmatched_user_id}>님은 이번 회차에 짝이 없어 점수 변동이 없습니다.",
                    inline=False,
                )
        elif round_row["type"] == "cutting":
            grouped: dict[str, list[str]] = {}
            for row in votes:
                grouped.setdefault(row["choice"], []).append(row["user_id"])

            lines = []
            for level in CUTTING_LEVEL_CHOICES:
                users = grouped.get(level, [])
                lines.append(f"{level}강화 ────── {len(users)}명")

            embed = discord.Embed(
                title=f"집계 · {round_row['title']}",
                description=round_row["title"],
                color=discord.Color.teal(),
            )
            embed.add_field(
                name="📊 강화 단계별 예측",
                value="```\n" + "\n".join(lines) + "\n```",
                inline=False,
            )
        elif round_row["type"] == "top3":
            candidates: list[dict[str, Any]] = extra["candidates"]
            candidate_ids = [str(candidate["user_id"]) for candidate in candidates]
            grouped: dict[str, list[str]] = {}
            for row in votes:
                grouped.setdefault(row["choice"], []).append(row["user_id"])

            none_voters = grouped.get(TOP3_NONE_CHOICE, [])
            none_votes = len(none_voters)
            candidate_vote_sum = sum(len(grouped.get(user_id, [])) for user_id in candidate_ids)
            candidate_lines = []
            for candidate in candidates:
                user_id = str(candidate["user_id"])
                candidate_lines.append(
                    f"<@{user_id}> ────── **{len(grouped.get(user_id, []))}표**"
                )

            if none_votes + candidate_vote_sum == 0:
                outcome = "아무도 투표하지 않아 이번 라운드는 무효입니다."
                settlement = "점수 변동 없음"
            elif none_votes > candidate_vote_sum:
                for user_id in none_voters:
                    add_score(
                        conn,
                        user_id,
                        member_name(guild, user_id),
                        1,
                        f"라운드 #{round_row['id']} 상위 3명 견제 성공",
                        round_row["id"],
                    )
                outcome = "`선택하지 않는다`가 후보 득표 합보다 많았습니다."
                settlement = f"`선택하지 않는다`를 고른 {none_votes}명 전원 +1점"
            else:
                target = max(
                    candidates,
                    key=lambda candidate: len(grouped.get(str(candidate["user_id"]), [])),
                )
                add_score(
                    conn,
                    str(target["user_id"]),
                    str(target["display_name"]),
                    -1,
                    f"라운드 #{round_row['id']} 상위 3명 견제 피격",
                    round_row["id"],
                )
                outcome = "세 후보 득표 합이 `선택하지 않는다` 이상이었습니다."
                settlement = f"<@{target['user_id']}> -1점"

            embed = discord.Embed(
                title=f"결과 · {round_row['title']}",
                description=round_row["title"],
                color=discord.Color.green(),
            )
            embed.add_field(name="🗳️ 후보별 득표", value="\n".join(candidate_lines), inline=False)
            embed.add_field(name="🚫 선택하지 않는다", value=f"**{none_votes}표**", inline=True)
            embed.add_field(name="👑 후보 득표 합", value=f"**{candidate_vote_sum}표**", inline=True)
            embed.add_field(name="🏁 결과", value=outcome, inline=False)
            embed.add_field(name="💰 정산", value=settlement, inline=False)
        else:
            # 제거된 게임 타입(예: 과거의 'grid')이 열린 채 남아 있는 경우. 정산 없이 닫는다.
            embed = discord.Embed(
                title=f"결과 · {round_row['title']}",
                description=round_row["title"],
                color=discord.Color.light_grey(),
            )
            embed.add_field(
                name="💰 정산",
                value="더 이상 지원하지 않는 게임이라 점수 없이 마감했습니다.",
                inline=False,
            )

        planned_mini_game = str(extra.get("planned_mini_game") or "")
        if round_row["type"] in MAIN_GAME_TYPES and planned_mini_game in MINI_GAME_TYPES:
            embed.add_field(
                name="🎮 다음 미니게임",
                value=f"**{GAME_NAMES[planned_mini_game]}**",
                inline=False,
            )
        embed.set_footer(text=f"시작: {round_row['created_at']} · 종료: {now_text()}")

        # 정산 요약. 분기마다 settlement 문구 유무가 달라서 그 변수에 기대지 않고,
        # 이 라운드에 실제로 적용된 score_logs를 역산한다. 어떤 경로로 닫혔든 결과가 정확하다.
        awarded = conn.execute(
            """
            SELECT user_id, SUM(delta) AS total
            FROM score_logs WHERE round_id = ?
            GROUP BY user_id ORDER BY total DESC
            """,
            (round_row["id"],),
        ).fetchall()
        vote_distribution: dict[str, int] = {}
        for vote_row in votes:
            key = str(vote_row["choice"])
            vote_distribution[key] = vote_distribution.get(key, 0) + 1

        log_activity(
            conn,
            "score",
            "round_settled",
            event_session_id=round_row["event_session_id"],
            round_id=round_row["id"],
            payload={
                "game_key": round_game_key(round_row),
                "closed_by": closed_by,
                "vote_count": len(votes),
                "vote_distribution": vote_distribution,
                "awarded": {
                    str(row["user_id"]): int(row["total"]) for row in awarded
                },
                "total_delta": sum(int(row["total"]) for row in awarded),
                "planned_mini_game": planned_mini_game or None,
                "settings": game_settings_snapshot(round_game_key(round_row)),
            },
        )

        return str(round_row["channel_id"]), embed


async def finish_round_and_publish(round_id: int, channel: Any, closed_by: str) -> None:
    """라운드를 정산하고 결과와 다음 미니게임 버튼을 채널에 발행한다.

    게임 모듈이 스스로 라운드를 끝낼 때(야구 3구 완료처럼 제한 시간이 아니라 진행
    상황으로 끝나는 경우) 부르는 통로다. 게임 모듈은 정산 분기를 아는
    close_round_record를 직접 import 할 수 없다 — 그쪽이 레지스트리를 거쳐 다시
    게임 모듈을 부르므로 순환이 된다. 그래서 runtime에 등록해두고 거기서 꺼내 쓴다.
    """
    result = close_round_record(round_id, getattr(channel, "guild", None), closed_by=closed_by)
    if result:
        await channel.send(embed=result[1], view=planned_mini_game_view(round_id))
    with db() as conn:
        round_row = conn.execute(
            "SELECT event_session_id FROM rounds WHERE id = ?", (round_id,)
        ).fetchone()
    if round_row:
        await publish_event_summary_if_complete(round_row["event_session_id"])


runtime.set_round_finisher(finish_round_and_publish)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    message = "명령어 처리 중 오류가 발생했습니다."
    if isinstance(error, app_commands.CheckFailure):
        message = str(error)

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def parse_duration_minutes(value: str) -> int:
    value = value.strip()
    if not value.isdigit():
        raise ValueError("진행 시간은 숫자로 입력해주세요.")
    minutes = int(value)
    if minutes < 1 or minutes > 1440:
        raise ValueError("진행 시간은 1분부터 1440분 사이로 입력해주세요.")
    return minutes


class StartSetupModal(discord.ui.Modal):
    def __init__(
        self,
        game: str,
        planned_mini_game: str | None = None,
        standalone_mini_game: bool = False,
    ) -> None:
        self.game = game
        self.planned_mini_game = planned_mini_game
        self.standalone_mini_game = standalone_mini_game
        super().__init__(title=f"{GAME_NAMES.get(game, '게임')} 설정")

        self.game_title = discord.ui.TextInput(
            label="제목",
            placeholder="게임 제목 또는 질문",
            default="야구" if game == "baseball" else None,
            max_length=120,
        )
        self.add_item(self.game_title)
        if game != "baseball":
            self.duration = discord.ui.TextInput(
                label="매칭 준비 시간(분)" if game in MAIN_GAME_TYPES else "진행 시간(분)",
                placeholder="예: 2" if game in MAIN_GAME_TYPES else "예: 5",
                default=str(TRUST_REGISTRATION_DEFAULT_MINUTES) if game in MAIN_GAME_TYPES else "5",
                max_length=4,
            )
            self.add_item(self.duration)

        # 가라앉는 배는 협상·실행 두 단계라 각 시간을 운영자가 따로 정한다.
        # (딜레마의 섬·모래성은 플레이 시간이 상수로 고정돼 있다.)
        if game == "bail":
            self.negotiation_minutes = discord.ui.TextInput(
                label="협상 시간(분)",
                placeholder=f"예: {BAIL_DEFAULT_NEGOTIATION_MINUTES}",
                default=str(BAIL_DEFAULT_NEGOTIATION_MINUTES),
                max_length=4,
            )
            self.execution_minutes = discord.ui.TextInput(
                label="실행(퍼내기 제출) 시간(분)",
                placeholder=f"예: {BAIL_DEFAULT_EXECUTION_MINUTES}",
                default=str(BAIL_DEFAULT_EXECUTION_MINUTES),
                max_length=4,
            )
            self.add_item(self.negotiation_minutes)
            self.add_item(self.execution_minutes)

        if game in {"balance", "minority_balance"}:
            self.option_a = discord.ui.TextInput(label="A 선택지", max_length=80)
            self.option_b = discord.ui.TextInput(label="B 선택지", max_length=80)
            self.add_item(self.option_a)
            self.add_item(self.option_b)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            duration = (
                0
                if self.game == "baseball"
                else parse_duration_minutes(str(self.duration.value))
            )
            title = str(self.game_title.value).strip()
            if not title:
                raise ValueError("제목을 입력해주세요.")

            if self.game == "bail":
                await create_round_from_setup(
                    interaction,
                    self.game,
                    duration,
                    title,
                    협상시간분=parse_duration_minutes(str(self.negotiation_minutes.value)),
                    실행시간분=parse_duration_minutes(str(self.execution_minutes.value)),
                    예정_미니게임=self.planned_mini_game,
                    단독_미니게임=self.standalone_mini_game,
                )
            elif self.game in {"balance", "minority_balance"}:
                await create_round_from_setup(
                    interaction,
                    self.game,
                    duration,
                    title,
                    선택_a=str(self.option_a.value).strip(),
                    선택_b=str(self.option_b.value).strip(),
                    예정_미니게임=self.planned_mini_game,
                    단독_미니게임=self.standalone_mini_game,
                )
            else:
                await create_round_from_setup(
                    interaction,
                    self.game,
                    duration,
                    title,
                    예정_미니게임=self.planned_mini_game,
                    단독_미니게임=self.standalone_mini_game,
                )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)


@bot.tree.command(name="오픈", description="새 이벤트 참가 모집을 열고 진행 흐름을 안내합니다.")
@app_commands.default_permissions()
@admin_only()
async def open_event(interaction: discord.Interaction) -> None:
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message(
            "이벤트 메인 채널로 사용할 서버 텍스트 채널에서 실행해주세요. 스레드에서는 열 수 없습니다.",
            ephemeral=True,
        )
        return

    with db() as conn:
        if get_open_round(conn):
            await interaction.response.send_message(
                "진행 중인 라운드가 있습니다. 먼저 `/라운드마감`으로 마무리해주세요.",
                ephemeral=True,
            )
            return

        conn.execute(
            """
            UPDATE event_sessions
            SET status = 'closed', closed_at = ?
            WHERE status IN ('recruiting', 'active')
            """,
            (now_text(),),
        )
        cursor = conn.execute(
            """
            INSERT INTO event_sessions (status, channel_id, created_at)
            VALUES ('recruiting', ?, ?)
            """,
            (str(interaction.channel.id), now_text()),
        )
        event_session_id = int(cursor.lastrowid)
        log_activity(
            conn,
            "event",
            "open",
            event_session_id=event_session_id,
            actor_user_id=str(interaction.user.id),
            payload={
                "channel_id": str(interaction.channel.id),
                "bot_version": BOT_VERSION,
            },
        )

    try:
        await interaction.response.send_message(
            embed=build_event_open_embed(),
            view=event_join_view(event_session_id),
        )
        message = await interaction.original_response()
    except discord.HTTPException:
        with db() as conn:
            conn.execute(
                "UPDATE event_sessions SET status = 'closed', closed_at = ? WHERE id = ?",
                (now_text(), event_session_id),
            )
        raise

    with db() as conn:
        conn.execute(
            "UPDATE event_sessions SET message_id = ? WHERE id = ?",
            (str(message.id), event_session_id),
        )


@bot.tree.command(name="start", description="메인게임과 미니게임을 골라 이벤트를 시작합니다.")
@app_commands.default_permissions()
@admin_only()
@app_commands.choices(
    메인게임=[
        app_commands.Choice(name="딜레마의 섬", value="trust"),
        app_commands.Choice(name="모래성 게임", value="sand"),
        app_commands.Choice(name="가라앉는 배", value="bail"),
    ],
    미니게임=[
        app_commands.Choice(name="밸런스 게임", value="balance"),
        app_commands.Choice(name="소수파 밸런스 (1차 위장)", value="minority_balance"),
        app_commands.Choice(name="몰리면 터진다!", value="defuse4"),
        app_commands.Choice(name="돌 세공 예측", value="cutting"),
        app_commands.Choice(name="상위 3명 견제", value="top3"),
        app_commands.Choice(name="홍피겔만의 황금금고", value="unique_number"),
        app_commands.Choice(name="야구", value="baseball"),
    ],
)
@app_commands.describe(메인게임="먼저 진행할 메인게임", 미니게임="메인게임 뒤에 진행할 미니게임")
async def start(interaction: discord.Interaction, 메인게임: str, 미니게임: str) -> None:
    with db() as conn:
        event_row = get_current_event(conn)
        if not event_row:
            await interaction.response.send_message(
                "먼저 `/오픈`으로 오늘의 이벤트 참가 모집을 열어주세요.",
                ephemeral=True,
            )
            return
        if event_row["status"] != "recruiting":
            await interaction.response.send_message(
                "이번 이벤트의 게임 조합은 이미 확정되었습니다. 다음 이벤트는 `/오픈`부터 다시 시작해주세요.",
                ephemeral=True,
            )
            return
    await interaction.response.send_modal(StartSetupModal(메인게임, planned_mini_game=미니게임))


@bot.tree.command(name="미니게임", description="메인게임 없이 선택한 미니게임만 시작합니다.")
@app_commands.default_permissions()
@admin_only()
@app_commands.choices(
    게임=[
        app_commands.Choice(name="밸런스 게임", value="balance"),
        app_commands.Choice(name="소수파 밸런스 (1차 위장)", value="minority_balance"),
        app_commands.Choice(name="몰리면 터진다!", value="defuse4"),
        app_commands.Choice(name="돌 세공 예측", value="cutting"),
        app_commands.Choice(name="상위 3명 견제", value="top3"),
        app_commands.Choice(name="홍피겔만의 황금금고", value="unique_number"),
        app_commands.Choice(name="야구", value="baseball"),
    ]
)
@app_commands.describe(게임="바로 시작할 미니게임")
async def start_mini_game(interaction: discord.Interaction, 게임: str) -> None:
    with db() as conn:
        event_row = get_current_event(conn)
        if not event_row:
            await interaction.response.send_message(
                "먼저 `/오픈`으로 오늘의 이벤트 참가 모집을 열어주세요.",
                ephemeral=True,
            )
            return
        if get_open_round(conn):
            await interaction.response.send_message(
                "이미 열린 라운드가 있습니다. 먼저 `/라운드마감`을 해주세요.",
                ephemeral=True,
            )
            return
    await interaction.response.send_modal(StartSetupModal(게임, standalone_mini_game=True))


@bot.tree.command(name="준비", description="야구 투구를 준비하고 와인드업을 알립니다.")
@app_commands.default_permissions()
@admin_only()
async def baseball_prepare(interaction: discord.Interaction) -> None:
    await baseball.prepare_command(interaction)


async def create_round_from_setup(
    interaction: discord.Interaction,
    게임: str,
    진행시간분: int,
    제목: str,
    선택_a: str | None = None,
    선택_b: str | None = None,
    선1: str | None = None,
    선2: str | None = None,
    선3: str | None = None,
    선4: str | None = None,
    선5: str | None = None,
    안전선개수: int = 2,
    안전색: str | None = None,
    힌트: str | None = None,
    협상시간분: int | None = None,
    실행시간분: int | None = None,
    예정_미니게임: str | None = None,
    단독_미니게임: bool = False,
) -> None:
    if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("서버 텍스트 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    with db() as conn:
        initial_event_row = get_current_event(conn)
    if not initial_event_row:
        await interaction.followup.send(
            "현재 열린 이벤트가 없습니다. 먼저 `/오픈`을 실행해주세요.",
            ephemeral=True,
        )
        return

    main_channel_id = str(initial_event_row["channel_id"])
    main_channel = await runtime.resolve_text_channel(main_channel_id)
    if main_channel is None:
        await interaction.followup.send(
            "이벤트를 시작한 메인 텍스트 채널을 찾을 수 없습니다.",
            ephemeral=True,
        )
        return

    if 게임 == "baseball" and not BASEBALL_IMAGE_PATH.is_file():
        await interaction.followup.send(
            f"야구 이미지 자산을 찾을 수 없습니다: `{BASEBALL_IMAGE_PATH.name}`",
            ephemeral=True,
        )
        return

    if 게임 in MAIN_GAME_TYPES and not await ensure_pair_thread_permissions(
        interaction,
        게임,
        main_channel,
    ):
        return

    close_at = time.time() + (int(진행시간분) * 60)
    extra: dict[str, Any] = {"duration_minutes": int(진행시간분)}
    if 게임 != "baseball":
        extra["auto_close_at"] = close_at
    if 게임 in MAIN_GAME_TYPES:
        if 예정_미니게임 not in MINI_GAME_TYPES:
            await interaction.followup.send("시작할 미니게임을 함께 선택해주세요.", ephemeral=True)
            return
        extra["planned_mini_game"] = 예정_미니게임

    with db() as conn:
        if get_open_round(conn):
            await interaction.followup.send("이미 열린 라운드가 있습니다. 먼저 `/라운드마감`을 해주세요.", ephemeral=True)
            return

        event_row = get_current_event(conn)
        if not event_row or int(event_row["id"]) != int(initial_event_row["id"]):
            await interaction.followup.send(
                "이벤트 상태가 바뀌었습니다. 현재 이벤트에서 다시 시도해주세요.",
                ephemeral=True,
            )
            return
        event_session_id = int(event_row["id"])
        participant_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS count FROM event_participants
                WHERE event_session_id = ?
                """,
                (event_session_id,),
            ).fetchone()["count"]
        )
        if 게임 in MAIN_GAME_TYPES and participant_count < 2:
            await interaction.followup.send(
                "메인게임을 시작하려면 `/오픈`의 `참여하기` 등록자가 최소 2명 필요합니다.",
                ephemeral=True,
            )
            return

        previous_round = conn.execute(
            """
            SELECT * FROM rounds
            WHERE event_session_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (event_session_id,),
        ).fetchone()
        if 게임 in MAIN_GAME_TYPES and (event_row["status"] != "recruiting" or previous_round):
            await interaction.followup.send(
                "이번 이벤트의 게임 조합은 이미 확정되었습니다. 다음 이벤트는 `/오픈`부터 다시 시작해주세요.",
                ephemeral=True,
            )
            return
        if previous_round and previous_round["type"] == "cutting":
            previous_extra = round_extra(previous_round)
            if "cutting_result_level" not in previous_extra:
                await interaction.followup.send(
                    "이전 돌 세공 예측의 결과가 아직 확정되지 않았습니다. 먼저 `/세공진행`으로 마무리해주세요.",
                    ephemeral=True,
                )
                return

        if 게임 in MAIN_GAME_TYPES:
            previous_phase = None
            if previous_round:
                previous_extra = round_extra(previous_round)
                previous_phase = (
                    previous_extra.get("trust_phase")
                    if previous_round["type"] == "trust"
                    else previous_extra.get("sand_phase")
                )
            if (
                previous_round
                and previous_round["type"] in MAIN_GAME_TYPES
                and previous_phase not in {"cancelled", None}
            ):
                await interaction.followup.send(
                    "메인게임 다음에는 미니게임 하나를 진행해야 합니다.",
                    ephemeral=True,
                )
                return
        elif not 단독_미니게임:
            previous_extra = round_extra(previous_round) if previous_round else {}
            previous_phase = None
            if previous_round and previous_round["type"] == "trust":
                previous_phase = previous_extra.get("trust_phase")
            elif previous_round and previous_round["type"] == "sand":
                previous_phase = previous_extra.get("sand_phase")
            if (
                not previous_round
                or previous_round["type"] not in MAIN_GAME_TYPES
                or previous_round["status"] != "closed"
                or previous_phase not in {"scored", None}
            ):
                await interaction.followup.send(
                    "미니게임을 시작하려면 참여자 모집·매칭·선택·정산까지 끝난 메인게임이 필요합니다.",
                    ephemeral=True,
                )
                return

        if 게임 in {"balance", "minority_balance"}:
            if not 선택_a or not 선택_b:
                await interaction.followup.send(
                    "밸런스 게임은 `선택_a`와 `선택_b`가 필요합니다.",
                    ephemeral=True,
                )
                return

            if 게임 == "minority_balance":
                extra.update({"mode": "minority_balance", "balance_phase": "initial"})

            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, option_a, option_b, status, channel_id, extra_json, created_at)
                VALUES ('balance', ?, ?, ?, ?, 'open', ?, ?, ?)
                """,
                (
                    "밸런스 게임 라운드",
                    제목,
                    선택_a,
                    선택_b,
                    main_channel_id,
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_balance_embed(round_id, 제목, 선택_a, 선택_b)
            view = vote_view(round_id, BALANCE_VOTE_CHOICES)
        elif 게임 == "defuse4":
            wire_map = {
                color["id"]: f"{color['emoji']} {color['name']}"
                for color in DEFUSE_COLORS
            }
            extra.update(
                {
                    "mode": "defuse4",
                    "wires": wire_map,
                    "settlement_rule": "top_vote_penalty",
                }
            )

            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('bomb', ?, ?, 'open', ?, ?, ?)
                """,
                (
                    제목,
                    제목,
                    main_channel_id,
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_defuse_embed(round_id, 제목)
            view = defuse_vote_view(round_id)
        elif 게임 == "trust":
            extra.update(
                {
                    "trust_phase": "registration",
                    "registration_minutes": int(진행시간분),
                    "registration_close_at": close_at,
                }
            )
            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('trust', ?, ?, 'open', ?, ?, ?)
                """,
                (
                    제목,
                    제목,
                    main_channel_id,
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_trust_registration_embed(round_id, 제목, int(진행시간분))
            view = discord.utils.MISSING
        elif 게임 == "sand":
            extra.update(
                {
                    "sand_phase": "registration",
                    "registration_minutes": int(진행시간분),
                    "registration_close_at": close_at,
                }
            )
            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('sand', ?, ?, 'open', ?, ?, ?)
                """,
                (
                    제목,
                    제목,
                    main_channel_id,
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_sand_registration_embed(round_id, 제목, int(진행시간분))
            view = discord.utils.MISSING
        elif 게임 == "bail":
            # rounds.type CHECK 제약을 못 고쳐서 type은 sand를 재사용하고 mode로 갈라 쓴다.
            extra.update(
                {
                    "mode": "bail",
                    "bail_phase": "registration",
                    "registration_minutes": int(진행시간분),
                    "registration_close_at": close_at,
                    "negotiation_seconds": int(
                        협상시간분 or BAIL_DEFAULT_NEGOTIATION_MINUTES
                    ) * 60,
                    "execution_seconds": int(실행시간분 or BAIL_DEFAULT_EXECUTION_MINUTES) * 60,
                }
            )
            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('sand', ?, ?, 'open', ?, ?, ?)
                """,
                (
                    제목,
                    제목,
                    main_channel_id,
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_bail_registration_embed(round_id, 제목, int(진행시간분))
            view = discord.utils.MISSING
        elif 게임 == "top3":
            rows = conn.execute(
                """
                SELECT users.user_id, users.display_name, users.total_score
                FROM event_participants
                JOIN users ON users.user_id = event_participants.user_id
                WHERE event_participants.event_session_id = ?
                ORDER BY users.total_score DESC, users.updated_at ASC
                LIMIT 3
                """,
                (event_session_id,),
            ).fetchall()
            if len(rows) < 3:
                await interaction.followup.send(
                    "상위 3명 견제 게임은 이번 이벤트 참가자 중 점수 기록 유저가 최소 3명 필요합니다.",
                    ephemeral=True,
                )
                return

            candidates = [
                {
                    "user_id": str(row["user_id"]),
                    "display_name": str(row["display_name"]),
                    "score": int(row["total_score"]),
                }
                for row in rows
            ]
            extra.update({"candidates": candidates})
            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('top3', ?, ?, 'open', ?, ?, ?)
                """,
                (
                    제목,
                    제목,
                    main_channel_id,
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_top3_embed(round_id, 제목, candidates)
            view = top3_vote_view(round_id, candidates)
        elif 게임 == "unique_number":
            # BASE_SCHEMA의 불변 CHECK 제약에 남아 있는 레거시 grid 슬롯을 사용하고,
            # mode로 과거 제거된 grid 라운드와 새 게임을 명확히 구분한다.
            extra.update({"mode": "unique_number"})
            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('grid', ?, ?, 'open', ?, ?, ?)
                """,
                (
                    제목,
                    제목,
                    main_channel_id,
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_unique_number_embed(round_id, 제목)
            view = unique_number_vote_view(round_id)
        elif 게임 == "baseball":
            # BASE_SCHEMA의 불변 CHECK 제약에 남아 있는 레거시 grid 슬롯을 사용한다.
            extra.update(
                {
                    "mode": "baseball",
                    "baseball_pitch": 1,
                    "baseball_phase": "waiting_prepare",
                    "baseball_settled_pitches": [],
                    "baseball_result_message_ids": {},
                }
            )
            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('grid', ?, ?, 'open', ?, ?, ?)
                """,
                (
                    제목,
                    제목,
                    main_channel_id,
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_baseball_embed(round_id, 제목, 1)
            view = None
        else:
            extra.update({"max_level": 6})
            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('cutting', ?, ?, 'open', ?, ?, ?)
                """,
                (
                    제목,
                    제목,
                    main_channel_id,
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_cutting_embed(round_id, 제목)
            view = cutting_vote_view(round_id)

        conn.execute(
            "UPDATE rounds SET event_session_id = ? WHERE id = ?",
            (event_session_id, round_id),
        )
        was_recruiting = bool(
            conn.execute(
                "SELECT 1 FROM event_sessions WHERE id = ? AND status = 'recruiting'",
                (event_session_id,),
            ).fetchone()
        )
        conn.execute(
            "UPDATE event_sessions SET status = 'active' WHERE id = ?",
            (event_session_id,),
        )
        # event_sessions에는 created_at/closed_at만 있어 recruiting->active 시각이 남지 않는다.
        # 이벤트 타임라인의 시작점이라 여기서 남겨야 한다. (미니게임 연속 시작 때는 이미 active)
        if was_recruiting:
            participant_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM event_participants WHERE event_session_id = ?",
                    (event_session_id,),
                ).fetchone()["count"]
            )
            log_activity(
                conn,
                "event",
                "start",
                event_session_id=event_session_id,
                actor_user_id=str(interaction.user.id),
                payload={
                    "first_game": 게임,
                    "participant_count": participant_count,
                    "planned_mini_game": extra.get("planned_mini_game"),
                    "bot_version": BOT_VERSION,
                },
            )

        log_activity(
            conn,
            "round",
            "create",
            event_session_id=event_session_id,
            round_id=round_id,
            actor_user_id=str(interaction.user.id),
            payload={
                "game_key": 게임,
                "game_name": GAME_NAMES.get(게임, 게임),
                "title": 제목,
                "duration_minutes": int(진행시간분),
                "is_main_game": 게임 in MAIN_GAME_TYPES,
                # 규칙이 바뀌어도 이 라운드를 당시 기준으로 해석하기 위한 스냅샷
                "settings": game_settings_snapshot(게임),
            },
        )
        if 게임 in MAIN_GAME_TYPES:
            conn.execute(
                """
                INSERT OR IGNORE INTO trust_registrations (round_id, user_id, registered_at)
                SELECT ?, user_id, joined_at
                FROM event_participants
                WHERE event_session_id = ?
                """,
                (round_id, event_session_id),
            )

    # 메인게임 등록 단계에서는 시작 시각만 알린다. 규칙은 임베드 본문에 이미 있고,
    # 다음 미니게임이 뭔지는 참가자가 미리 알 필요가 없다.
    if 게임 in MAIN_GAME_TYPES:
        if 게임 == "bail":
            embed.add_field(
                name="게임 시작",
                value=(
                    f"{진행시간분}분 후 1:1 매칭과 함께 협상이 시작됩니다.\n"
                    f"시작 예정: {close_time_text(close_at)}\n"
                    f"협상 {협상시간분 or BAIL_DEFAULT_NEGOTIATION_MINUTES}분 → "
                    f"실행 {실행시간분 or BAIL_DEFAULT_EXECUTION_MINUTES}분"
                ),
                inline=False,
            )
        elif 게임 == "sand":
            embed.add_field(
                name="게임 시작",
                value=(
                    f"{진행시간분}분 후 1:1 매칭과 함께 게임이 시작됩니다.\n"
                    f"시작 예정: {close_time_text(close_at)}"
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="랜덤 매칭 시작",
                value=(
                    f"오늘의 이벤트 참가 명단으로 {진행시간분}분 후 랜덤 매칭을 시작합니다.\n"
                    f"예정 시각: {close_time_text(close_at)}\n"
                    "매칭 후에는 신뢰/배신 선택 시간이 3분 주어집니다."
                ),
                inline=False,
            )
    elif 게임 != "baseball":
        embed.add_field(
            name="자동 마감",
            value=f"{진행시간분}분 후 자동으로 종료됩니다.\n예정 시각: {close_time_text(close_at)}",
            inline=False,
        )
    send_kwargs: dict[str, Any] = {"embed": embed, "view": view}
    if 게임 == "baseball":
        send_kwargs["file"] = discord.File(
            BASEBALL_IMAGE_PATH,
            filename=BASEBALL_IMAGE_FILENAME,
        )
    message = await main_channel.send(**send_kwargs)
    if 게임 == "baseball":
        control_message = await main_channel.send(
            content=baseball_control_content(1, "waiting_prepare"),
            view=None,
        )
        extra["baseball_control_message_id"] = str(control_message.id)

    discussion_thread_id = None
    if 게임 in {"balance", "minority_balance", "defuse4", "unique_number"}:
        try:
            thread_name = f"{제목} | 토론방"[:100]
            # 보관되면 일반 유저의 버튼 클릭이 막힌다. 라운드가 끝날 때까지 살아 있도록 넉넉히 잡는다.
            thread = await message.create_thread(name=thread_name, auto_archive_duration=1440)
            button_message = await thread.send(
                embed=build_anonymous_guide_embed(0, close_at),
                view=thread_discussion_view(round_id, 게임),
            )
            # 발언이 쌓여도 버튼이 항상 맨 아래에 오도록, 발언마다 이 메시지를 지우고 다시 보낸다.
            extra["anon_button_message_id"] = str(button_message.id)
            if 게임 in {"balance", "minority_balance"}:
                notice_embed = build_thread_notice_embed(
                    round_id,
                    thread,
                    "토론방이 열렸습니다",
                    "버튼을 눌러 익명으로 설득, 블러핑, 진실 말하기를 할 수 있습니다. 횟수 제한은 없습니다.",
                )
            elif 게임 == "unique_number":
                notice_embed = build_thread_notice_embed(
                    round_id,
                    thread,
                    "토론방이 열렸습니다",
                    "버튼을 눌러 익명으로 심리전과 눈치싸움을 펼쳐보세요. 블러핑과 유도 모두 가능합니다. 횟수 제한은 없습니다.",
                )
            else:
                notice_embed = build_thread_notice_embed(
                    round_id,
                    thread,
                    "폭탄 토론방이 열렸습니다",
                    "버튼을 눌러 익명으로 표를 분산하거나 상대를 한 선으로 유도해보세요. 블러핑과 역추리 모두 가능합니다.",
                )
            # 스레드를 잠그면 `스레드 관리` 권한이 없는 유저의 버튼 클릭까지 디스코드가 막는다.
            # 익명성은 on_message의 일반 메시지 삭제로만 지킨다.
            await main_channel.send(embed=notice_embed)
            discussion_thread_id = str(thread.id)
        except discord.HTTPException:
            discussion_thread_id = None

    with db() as conn:
        if 게임 == "baseball":
            log_activity(
                conn,
                "round",
                "baseball_control_published",
                event_session_id=event_session_id,
                round_id=round_id,
                actor_user_id=str(interaction.user.id),
                payload={
                    "pitch_number": 1,
                    "control_message_id": extra.get("baseball_control_message_id"),
                },
            )
        conn.execute(
            """
            UPDATE rounds
            SET message_id = ?, discussion_thread_id = ?, extra_json = ?
            WHERE id = ?
            """,
            (
                str(message.id),
                discussion_thread_id,
                json.dumps(extra, ensure_ascii=False),
                round_id,
            ),
        )

    if 게임 != "baseball":
        bot.schedule_auto_close(round_id, close_at)
    await interaction.followup.send(
        f"게임 메시지를 <#{main_channel_id}>에 게시했습니다.",
        ephemeral=True,
    )


@bot.tree.command(name="라운드마감", description="현재 열린 라운드를 마감하고 결과를 공개합니다.")
@app_commands.default_permissions()
@admin_only()
async def close_round(interaction: discord.Interaction) -> None:
    with db() as conn:
        round_row = get_open_round(conn)
    if not round_row:
        await interaction.response.send_message("현재 열린 라운드가 없습니다.", ephemeral=True)
        return

    if round_row["type"] == "trust" and round_extra(round_row).get("trust_phase") == "registration":
        await interaction.response.defer(ephemeral=True)
        transition = start_trust_decision(int(round_row["id"]))
        if not transition:
            await interaction.followup.send("참여자 모집을 마감하지 못했습니다.", ephemeral=True)
            return
        task = bot.auto_close_tasks.pop(int(round_row["id"]), None)
        if task:
            task.cancel()
        await create_trust_pair_threads(transition)
        await update_trust_main_message(transition)
        if transition["state"] == "decision":
            bot.schedule_auto_close(int(round_row["id"]), float(transition["close_at"]))
            await interaction.followup.send("참여자 모집을 마감하고 랜덤 매칭 및 3분 선택을 시작했습니다.", ephemeral=True)
        else:
            await interaction.followup.send("참여자가 부족해 딜레마의 섬을 취소했습니다.", ephemeral=True)
        return

    if round_row["type"] == "sand" and round_extra(round_row).get("sand_phase") == "registration":
        # 짝마다 비공개 방을 여느라 API 호출이 여러 번이다. 3초 응답 제한을 넘기므로 먼저 defer한다.
        await interaction.response.defer(ephemeral=True)
        transition = start_sand_game(int(round_row["id"]))
        if not transition:
            await interaction.followup.send("참여자 모집을 마감하지 못했습니다.", ephemeral=True)
            return
        task = bot.auto_close_tasks.pop(int(round_row["id"]), None)
        if task:
            task.cancel()
        await create_sand_pair_threads(transition)
        await update_trust_main_message(transition)
        if transition["state"] == "playing":
            bot.schedule_auto_close(int(round_row["id"]), float(transition["close_at"]))
            await interaction.followup.send("참여자 모집을 마감하고 1:1 모래성 게임을 시작했습니다.", ephemeral=True)
        else:
            await interaction.followup.send("참여자가 부족해 모래성 게임을 취소했습니다.", ephemeral=True)
        return

    if round_game_key(round_row) == "bail" and main_game_phase(round_row) in {
        "registration",
        "negotiation",
    }:
        # 짝마다 비공개 방을 여느라 API 호출이 여러 번이다. 3초 응답 제한을 넘기므로 먼저 defer한다.
        await interaction.response.defer(ephemeral=True)
        round_id = int(round_row["id"])
        registration = main_game_phase(round_row) == "registration"
        transition = (
            start_bail_negotiation(round_id) if registration else start_bail_execution(round_id)
        )
        if not transition:
            await interaction.followup.send("현재 단계를 마감하지 못했습니다.", ephemeral=True)
            return
        task = bot.auto_close_tasks.pop(round_id, None)
        if task:
            task.cancel()
        if transition["state"] == "cancelled":
            await update_trust_main_message(transition)
            await interaction.followup.send("참여자가 부족해 가라앉는 배를 취소했습니다.", ephemeral=True)
            return
        if registration:
            await create_bail_pair_threads(transition)
            await update_trust_main_message(transition)
            await interaction.followup.send(
                "참여자 모집을 마감하고 1:1 협상 단계를 시작했습니다.", ephemeral=True
            )
        else:
            await refresh_all_bail_pair_boards(round_id)
            await refresh_main_bail_board(round_id)
            await interaction.followup.send("협상을 마감하고 실행 단계를 시작했습니다.", ephemeral=True)
        bot.schedule_auto_close(round_id, float(transition["close_at"]))
        return

    round_extra_row = round_extra(round_row)
    if (
        round_row["type"] == "balance"
        and round_extra_row.get("mode") == "minority_balance"
        and round_extra_row.get("balance_phase") == "initial"
    ):
        await interaction.response.defer(ephemeral=True)
        transition = start_minority_balance_revote(int(round_row["id"]))
        if not transition:
            await interaction.followup.send("1차 집계를 마감하지 못했습니다.", ephemeral=True)
            return
        task = bot.auto_close_tasks.pop(int(round_row["id"]), None)
        if task:
            task.cancel()
        published = await publish_minority_balance_revote(transition)
        bot.schedule_auto_close(int(round_row["id"]), float(transition["close_at"]))
        if published:
            await interaction.followup.send(
                "1차 인원수를 공개하고 같은 선택지의 재선택을 시작했습니다.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "재선택 단계로 전환했지만 이벤트 메인 채널에 안내를 게시하지 못했습니다.",
                ephemeral=True,
            )
        return

    # 결과 메시지는 명령을 실행한 위치와 무관하게 이벤트가 시작된 메인 채널에만 게시한다.
    is_trust_decision = (
        round_row["type"] == "trust" and round_extra_row.get("trust_phase") == "decision"
    )
    is_sand_playing = (
        round_row["type"] == "sand"
        and round_extra_row.get("mode") != "bail"
        and round_extra_row.get("sand_phase") == "playing"
    )
    is_bail_execution = (
        round_game_key(round_row) == "bail" and round_extra_row.get("bail_phase") == "execution"
    )
    publishes_pair_results = is_trust_decision or is_sand_playing or is_bail_execution
    await interaction.response.defer(ephemeral=True)

    result = close_round_record(int(round_row["id"]), interaction.guild)
    if not result:
        await interaction.followup.send("현재 열린 라운드가 없습니다.", ephemeral=True)
        return

    task = bot.auto_close_tasks.pop(int(round_row["id"]), None)
    if task:
        task.cancel()
    baseball_task = bot.baseball_pitch_tasks.pop(int(round_row["id"]), None)
    if baseball_task:
        baseball_task.cancel()
    baseball_activation_task = bot.baseball_activation_tasks.pop(
        int(round_row["id"]), None
    )
    if baseball_activation_task:
        baseball_activation_task.cancel()
    await delete_baseball_control_message(round_row)

    channel_id, embed = result
    if publishes_pair_results:
        if is_trust_decision:
            await publish_trust_pair_results(int(round_row["id"]))
        elif is_bail_execution:
            await publish_bail_pair_results(int(round_row["id"]))
        else:
            await publish_sand_pair_results(int(round_row["id"]))
    await archive_round_discussion_thread(round_row, reason="라운드 종료 후 영구 보관")

    channel = await runtime.resolve_text_channel(channel_id)
    if channel is None:
        await interaction.followup.send(
            "라운드는 마감했지만 이벤트 메인 채널을 찾지 못해 결과를 게시하지 못했습니다.",
            ephemeral=True,
        )
        return
    await channel.send(
        embed=embed,
        view=planned_mini_game_view(int(round_row["id"])),
    )
    await publish_event_summary_if_complete(round_row["event_session_id"])
    await interaction.followup.send(
        f"라운드 결과를 <#{channel_id}>에 게시했습니다.",
        ephemeral=True,
    )


@bot.tree.command(name="이벤트종료", description="개발 중인 현재 이벤트를 남은 게임과 관계없이 강제로 종료합니다.")
@app_commands.default_permissions()
@admin_only()
async def force_end_event(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    with db() as conn:
        result = force_close_current_event(conn)
        force_summary: tuple[str, discord.Embed] | None = None
        if result:
            original_event_row, _ = result
            closed_event_row = conn.execute(
                "SELECT * FROM event_sessions WHERE id = ?",
                (int(original_event_row["id"]),),
            ).fetchone()
            closed_round_rows = conn.execute(
                "SELECT * FROM rounds WHERE event_session_id = ? ORDER BY id ASC",
                (int(original_event_row["id"]),),
            ).fetchall()
            force_summary = (
                str(closed_event_row["channel_id"]),
                build_event_summary_embed(
                    conn,
                    closed_event_row,
                    closed_round_rows,
                    force_closed=True,
                ),
            )
    if not result:
        await interaction.followup.send("현재 열려 있는 이벤트가 없습니다.", ephemeral=True)
        return

    event_row, round_rows = result
    open_round_count = sum(1 for row in round_rows if row["status"] == "open")

    for round_row in round_rows:
        task = bot.auto_close_tasks.pop(int(round_row["id"]), None)
        if task:
            task.cancel()
        baseball_task = bot.baseball_pitch_tasks.pop(int(round_row["id"]), None)
        if baseball_task:
            baseball_task.cancel()
        baseball_activation_task = bot.baseball_activation_tasks.pop(
            int(round_row["id"]), None
        )
        if baseball_activation_task:
            baseball_activation_task.cancel()
        await delete_baseball_control_message(round_row)

        channel_id = round_row["channel_id"]
        message_id = round_row["message_id"]
        if channel_id and message_id:
            try:
                channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
                if hasattr(channel, "fetch_message"):
                    message = await channel.fetch_message(int(message_id))
                    await message.edit(view=None)
            except (discord.HTTPException, ValueError):
                pass

        thread_id = round_row["discussion_thread_id"]
        if thread_id:
            try:
                thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
                if isinstance(thread, discord.Thread):
                    # archive+lock은 삭제가 아니다. 활성 목록에서만 빠지고 보관 목록에 영구히 남는다.
                    await thread.edit(
                        archived=True,
                        locked=True,
                        reason="개발용 이벤트 강제 종료",
                    )
            except (discord.HTTPException, ValueError):
                pass

        # 딜레마의 섬·모래성은 짝마다 비공개 방을 연다. 강제 종료 시 전부 닫는다.
        if round_row["type"] in MAIN_GAME_TYPES:
            with db() as conn:
                pair_threads = conn.execute(
                    """
                    SELECT discussion_thread_id FROM trust_pairings
                    WHERE round_id = ? AND discussion_thread_id IS NOT NULL
                    """,
                    (int(round_row["id"]),),
                ).fetchall()
            for pair_thread in pair_threads:
                try:
                    pair_thread_id = int(pair_thread["discussion_thread_id"])
                    thread = bot.get_channel(pair_thread_id) or await bot.fetch_channel(pair_thread_id)
                    if isinstance(thread, discord.Thread):
                        # 강제 종료 때도 스레드를 지우지 않고 읽기 전용 보관 상태로만 전환한다.
                        await thread.edit(
                            archived=True,
                            locked=True,
                            reason="개발용 이벤트 강제 종료",
                        )
                except (discord.HTTPException, ValueError):
                    pass

    if event_row["message_id"]:
        try:
            channel = bot.get_channel(int(event_row["channel_id"])) or await bot.fetch_channel(
                int(event_row["channel_id"])
            )
            if hasattr(channel, "fetch_message"):
                message = await channel.fetch_message(int(event_row["message_id"]))
                await message.edit(view=None)
        except (discord.HTTPException, ValueError):
            pass

    if force_summary:
        channel_id, summary_embed = force_summary
        channel = await runtime.resolve_text_channel(channel_id)
        if channel is not None:
            try:
                await channel.send(embed=summary_embed)
            except discord.HTTPException:
                pass

    await interaction.followup.send(
        f"이벤트 #{event_row['id']}을 강제로 종료했습니다. "
        f"미완료 라운드 {open_round_count}개는 점수 정산 없이 취소했습니다.",
        ephemeral=True,
    )


@bot.tree.command(name="세공진행", description="가장 최근 마감된 돌 세공 예측의 관리자 진행 패널을 엽니다.")
@app_commands.default_permissions()
@admin_only()
async def run_cutting(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM rounds
            WHERE type = 'cutting' AND status = 'closed'
            ORDER BY id DESC
            """
        ).fetchall()

        round_row = None
        extra: dict[str, Any] = {}
        for row in rows:
            parsed_extra = round_extra(row)
            if "cutting_result_level" not in parsed_extra and not parsed_extra.get("force_closed"):
                round_row = row
                extra = parsed_extra
                break

        if not round_row:
            await interaction.followup.send("세공을 진행할 마감된 돌 세공 라운드가 없습니다.", ephemeral=True)
            return
        if round_row["status"] != "closed":
            await interaction.followup.send("먼저 투표 집계를 마감한 뒤 `/세공진행`을 실행해주세요.", ephemeral=True)
            return

        extra.setdefault("cutting_current_level", 0)
        extra.setdefault("cutting_logs", [])
        extra.setdefault("cutting_started_at", now_text())
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_row["id"]),
        )

    embed = build_cutting_progress_embed(round_row, extra)
    channel_id = str(round_row["channel_id"])
    channel = await runtime.resolve_text_channel(channel_id)
    if channel is None:
        await interaction.followup.send("이벤트 메인 채널을 찾을 수 없습니다.", ephemeral=True)
        return
    await channel.send(embed=embed, view=cutting_action_view(int(round_row["id"])))
    await interaction.followup.send(
        f"세공 진행 패널을 <#{channel_id}>에 게시했습니다.",
        ephemeral=True,
    )


@bot.tree.command(name="이벤트리스트", description="진행 중인 이벤트와 라운드를 확인합니다.")
@app_commands.default_permissions()
@admin_only()
async def list_events(interaction: discord.Interaction) -> None:
    with db() as conn:
        event_row = get_current_event(conn)
        if not event_row:
            await interaction.response.send_message("진행 중인 이벤트가 없습니다.", ephemeral=True)
            return
        event_session_id = int(event_row["id"])
        participant_count = int(
            conn.execute(
                "SELECT COUNT(*) AS count FROM event_participants WHERE event_session_id = ?",
                (event_session_id,),
            ).fetchone()["count"]
        )
        round_rows = conn.execute(
            "SELECT * FROM rounds WHERE event_session_id = ? ORDER BY id ASC",
            (event_session_id,),
        ).fetchall()

    status = str(event_row["status"])
    embed = discord.Embed(
        title=f"진행 중인 이벤트 #{event_session_id}",
        description=f"<#{event_row['channel_id']}>에서 진행 중입니다.",
        color=discord.Color.green(),
    )
    embed.add_field(
        name="상태",
        value=(
            f"{EVENT_STATUS_LABELS.get(status, status)} · 참가자 {participant_count}명\n"
            f"시작: {event_row['created_at']}"
        ),
        inline=False,
    )

    lines: list[str] = []
    for round_row in round_rows:
        extra = round_extra(round_row)
        game_key = round_game_key(round_row)
        # 메인게임은 main_game_phase가 모아두고, 단계가 있는 미니게임은 여기서 붙인다.
        phase = main_game_phase(round_row) or extra.get("baseball_phase")
        round_status = "진행 중" if round_row["status"] == "open" else "마감"
        lines.append(
            f"#{round_row['id']} {GAME_NAMES.get(game_key, game_key)} · {round_status}"
            f"{f' ({phase})' if phase else ''} · {round_row['question']}"
        )
    embed.add_field(
        name="라운드",
        value=field_text(lines, empty="아직 시작한 라운드가 없습니다."),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="러시안룰렛", description="한 플레이어에게 점수를 건 1:1 러시안룰렛을 신청합니다.")
@app_commands.describe(플레이어="결투를 신청할 플레이어")
async def russian_roulette(
    interaction: discord.Interaction, 플레이어: discord.Member
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "서버의 공개 텍스트 채널에서만 러시안룰렛을 시작할 수 있습니다.", ephemeral=True
        )
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message(
            "러시안룰렛은 일반 텍스트 채널에서만 시작할 수 있습니다.",
            ephemeral=True,
        )
        return
    if not interaction.channel.permissions_for(플레이어).view_channel:
        await interaction.response.send_message(
            "선택한 상대방이 이 채널을 볼 수 없어 결투 요청을 받을 수 없습니다. "
            "상대방이 접근할 수 있는 채널에서 실행해주세요.",
            ephemeral=True,
        )
        return
    if 플레이어.bot:
        await interaction.response.send_message("봇에게는 결투를 신청할 수 없습니다.", ephemeral=True)
        return
    if 플레이어.id == interaction.user.id:
        await interaction.response.send_message("자기 자신에게는 결투를 신청할 수 없습니다.", ephemeral=True)
        return

    challenger_id = str(interaction.user.id)
    target_id = str(플레이어.id)
    with db() as conn:
        ensure_user(conn, interaction.user)
        ensure_user(conn, 플레이어)
        expire_stale_roulette_games(conn)

        if active_roulette_game_for_user(conn, challenger_id):
            await interaction.response.send_message(
                "이미 진행 중이거나 응답을 기다리는 러시안룰렛이 있습니다.", ephemeral=True
            )
            return
        target_active = active_roulette_game_for_user(conn, target_id)
        if target_active is not None:
            await interaction.response.send_message(
                "상대방이 이미 다른 러시안룰렛에 참여 중입니다.", ephemeral=True
            )
            return

        score_rows = conn.execute(
            "SELECT user_id, total_score FROM users WHERE user_id IN (?, ?)",
            (challenger_id, target_id),
        ).fetchall()
        scores = {str(row["user_id"]): int(row["total_score"]) for row in score_rows}
        challenger_score = scores.get(challenger_id, 0)
        target_score = scores.get(target_id, 0)
        if is_roulette_credit_user(challenger_score):
            await interaction.response.send_message(
                f"러시안룰렛을 신청하려면 최소 {ROULETTE_MIN_BET}점이 필요합니다. "
                f"(현재 {challenger_score}점)\n"
                "점수가 부족해도 다른 사람의 신청을 **수락**할 수는 있습니다.",
                ephemeral=True,
            )
            return

        # 상대의 잔여 횟수는 여기서 보지 않는다. 신청 단계에서 걸러주면 그 실패가
        # 상대의 오늘 활동을 알려주는 무료 조회가 된다. 수락 시점에 상대 본인이 확인한다.
        if count_roulette_games_today(conn, challenger_id) >= ROULETTE_DAILY_LIMIT:
            await interaction.response.send_message(
                f"오늘 러시안룰렛을 {ROULETTE_DAILY_LIMIT}번 모두 사용했습니다. "
                "내일 다시 도전해주세요.",
                ephemeral=True,
            )
            return
        if (
            count_roulette_games_with_opponent_today(conn, challenger_id, target_id)
            >= ROULETTE_PAIR_DAILY_LIMIT
        ):
            await interaction.response.send_message(
                "오늘 이 상대와는 이미 겨뤘습니다. 같은 상대와는 하루 "
                f"{ROULETTE_PAIR_DAILY_LIMIT}번까지입니다.",
                ephemeral=True,
            )
            return

        cursor = conn.execute(
            """
            INSERT INTO russian_roulette_games (
                guild_id, channel_id, challenger_id, target_id,
                challenger_name, target_name, status, created_at, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                str(interaction.guild.id),
                str(interaction.channel.id),
                challenger_id,
                target_id,
                getattr(interaction.user, "display_name", interaction.user.name),
                플레이어.display_name,
                now_text(),
                time.time() + ROULETTE_CHALLENGE_SECONDS,
            ),
        )
        game_id = int(cursor.lastrowid)
        log_activity(
            conn,
            "roulette",
            "challenge",
            actor_user_id=challenger_id,
            target_user_id=target_id,
            payload={
                "game_id": game_id,
                "challenger_score": challenger_score,
                "target_score": target_score,
            },
        )
        game = dict(
            conn.execute(
                "SELECT * FROM russian_roulette_games WHERE id = ?", (game_id,)
            ).fetchone()
        )

    try:
        await interaction.response.send_message(
            content=f"<@{target_id}>님, 러시안룰렛 결투 요청이 도착했습니다.",
            embed=build_roulette_embed(game),
            view=roulette_view(game),
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True, replied_user=False
            ),
        )
        message = await interaction.original_response()
    except discord.HTTPException:
        with db() as conn:
            conn.execute(
                "UPDATE russian_roulette_games SET status = 'expired', finished_at = ? WHERE id = ?",
                (now_text(), game_id),
            )
        raise

    with db() as conn:
        conn.execute(
            "UPDATE russian_roulette_games SET message_id = ? WHERE id = ?",
            (str(message.id), game_id),
        )


@bot.tree.command(name="잠수", description="내 잠수 코인을 확인합니다.")
async def show_afk_coins(interaction: discord.Interaction) -> None:
    with db() as conn:
        ensure_user(conn, interaction.user)
        coins = get_afk_coins(conn, str(interaction.user.id))

    await interaction.response.send_message(
        f"현재 잠수 코인 **{coins}개**입니다.\n"
        f"음성채널에 있으면 1분마다 {AFK_COIN_PER_TICK}개씩 쌓이고, "
        f"`/지목` 1회에 {SCORE_REVEAL_COST}개를 씁니다.",
        ephemeral=True,
    )


@bot.tree.command(
    name="지목",
    description=f"잠수 코인 {SCORE_REVEAL_COST}개를 써서 한 사람의 점수를 확인합니다.",
)
@app_commands.describe(사용자="점수를 확인할 사람")
async def reveal_score(interaction: discord.Interaction, 사용자: discord.Member) -> None:
    if 사용자.bot:
        await interaction.response.send_message("봇의 점수는 확인할 수 없습니다.", ephemeral=True)
        return
    if 사용자.id == interaction.user.id:
        await interaction.response.send_message(
            "내 점수는 `/점수`로 코인 없이 확인할 수 있습니다.",
            ephemeral=True,
        )
        return

    user_id = str(interaction.user.id)
    with db() as conn:
        ensure_user(conn, interaction.user)
        if not spend_afk_coins(conn, user_id, SCORE_REVEAL_COST):
            coins = get_afk_coins(conn, user_id)
            await interaction.response.send_message(
                f"잠수 코인이 부족합니다. (현재 {coins}개 / 필요 {SCORE_REVEAL_COST}개)",
                ephemeral=True,
            )
            return
        remaining = get_afk_coins(conn, user_id)
        target_row = conn.execute(
            "SELECT total_score FROM users WHERE user_id = ?", (str(사용자.id),)
        ).fetchone()

    target_score = int(target_row["total_score"]) if target_row else 0
    await interaction.response.send_message(
        f"<@{사용자.id}>님의 현재 점수는 **{target_score}점**입니다.\n"
        f"잠수 코인 {SCORE_REVEAL_COST}개를 사용했습니다. (잔여 {remaining}개)",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="점수", description="3등 점수와 내 점수를 확인합니다.")
async def show_score(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버 안에서만 점수를 확인할 수 있습니다.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    current_bot_id = str(interaction.client.user.id) if interaction.client.user else ""
    with db() as conn:
        ensure_user(conn, interaction.user)
        my_row = conn.execute(
            "SELECT total_score FROM users WHERE user_id = ?", (str(interaction.user.id),)
        ).fetchone()
        candidates = fetch_ranking_rows(conn, current_bot_id)

    third_score = None
    try:
        found = 0
        for candidate in candidates:
            if await current_guild_member(interaction.guild, str(candidate["user_id"])) is None:
                continue
            found += 1
            if found == 3:
                third_score = int(candidate["total_score"])
                break
    except discord.HTTPException:
        await interaction.followup.send(
            "현재 서버 멤버를 확인하지 못했습니다. 잠시 후 다시 시도해주세요.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        format_my_score_message(third_score, int(my_row["total_score"])),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def send_ranking(
    interaction: discord.Interaction,
    rank: int | None,
    *,
    reveal_users: bool,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버 안에서만 랭킹을 공개할 수 있습니다.", ephemeral=True)
        return

    await interaction.response.defer()

    current_bot_id = str(interaction.client.user.id) if interaction.client.user else ""
    with db() as conn:
        candidates = fetch_ranking_rows(conn, current_bot_id)

    requested_count = rank if rank is not None else 10
    rows = []
    try:
        for candidate in candidates:
            if await current_guild_member(interaction.guild, str(candidate["user_id"])) is None:
                continue
            rows.append(candidate)
            if len(rows) == requested_count:
                break
    except discord.HTTPException:
        await interaction.followup.send(
            "현재 서버 멤버를 확인하지 못했습니다. 잠시 후 다시 시도해주세요.",
            ephemeral=True,
        )
        return

    if not rows:
        await interaction.followup.send(
            "현재 서버에 게임 참여 기록이 있는 유저가 없습니다.",
            ephemeral=True,
        )
        return

    if rank is not None and len(rows) < rank:
        await interaction.followup.send(
            f"현재 서버 랭킹에는 {rank}등이 없습니다. 참가자는 {len(rows)}명입니다.",
            ephemeral=True,
        )
        return

    ranking_label = "랭킹" if reveal_users else "익명 랭킹"
    formatter = format_public_ranking_lines if reveal_users else format_anonymous_ranking_lines
    if rank is None:
        title = f"현재 {ranking_label} TOP 10"
        ranking_lines = formatter(rows)
    else:
        title = f"현재 {ranking_label} {rank}등"
        ranking_lines = formatter([rows[rank - 1]], start_rank=rank)

    embed = discord.Embed(
        title=title,
        description="\n".join(ranking_lines),
        color=discord.Color.gold(),
    )
    await interaction.followup.send(
        embed=embed,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="랭킹", description="이름이 표시되는 TOP 10 또는 특정 등수를 공개합니다.")
@app_commands.default_permissions()
@admin_only()
@app_commands.describe(등수="이 등수만 공개합니다. 생략하면 TOP 10을 공개합니다.")
async def show_ranking(
    interaction: discord.Interaction,
    등수: app_commands.Range[int, 1, 1000] | None = None,
) -> None:
    await send_ranking(interaction, 등수, reveal_users=True)


@bot.tree.command(name="익명랭킹", description="이름을 숨긴 TOP 10 또는 특정 등수를 공개합니다.")
@app_commands.default_permissions()
@admin_only()
@app_commands.describe(등수="이 등수만 익명으로 공개합니다. 생략하면 TOP 10을 공개합니다.")
async def show_anonymous_ranking(
    interaction: discord.Interaction,
    등수: app_commands.Range[int, 1, 1000] | None = None,
) -> None:
    await send_ranking(interaction, 등수, reveal_users=False)


def backup_database(dest: Path) -> None:
    """SQLite 온라인 백업 API로 DB를 복사한다.

    WAL 모드에서는 최근 커밋이 아직 -wal 파일에만 있을 수 있어서 DB 파일만
    단순 복사하면 그 내용이 빠진다. backup()은 WAL까지 반영한 일관된 스냅샷을
    만들고, 봇이 돌고 있는 중에 호출해도 안전하다.
    """
    src = sqlite3.connect(config.DB_PATH, timeout=10.0)
    dst = sqlite3.connect(dest)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()


@bot.tree.command(name="데이터백업", description="SQLite 데이터베이스를 백업합니다.")
@app_commands.default_permissions()
@admin_only()
async def backup_data(interaction: discord.Interaction) -> None:
    if not config.DB_PATH.exists():
        await interaction.response.send_message("아직 데이터베이스 파일이 없습니다.", ephemeral=True)
        return

    backup_dir = config.DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"event_game_{timestamp}.sqlite3"
    score_report_path = backup_dir / f"score_report_{timestamp}.csv"
    backup_database(backup_path)
    current_bot_id = str(interaction.client.user.id) if interaction.client.user else ""
    with db() as conn:
        score_rows = fetch_admin_score_rows(conn, current_bot_id)
    write_admin_score_report(score_report_path, score_rows)
    await interaction.response.send_message(
        f"백업 완료: `{backup_path.name}`\n관리자 점수 데이터: `{score_report_path.name}`",
        file=discord.File(score_report_path, filename=score_report_path.name),
        ephemeral=True,
    )


def sd_notify(state: str) -> None:
    """systemd에 상태를 알린다 (Type=notify).

    on_ready에서 READY=1을 보내므로 `systemctl restart`는 봇이 실제로 디스코드
    로그인을 마쳐야 성공으로 끝난다. 배포 스크립트는 이 종료 코드만 보면 된다.
    NOTIFY_SOCKET이 없는 로컬 실행에서는 아무것도 하지 않는다.
    """
    address = os.getenv("NOTIFY_SOCKET")
    if not address or not hasattr(socket, "AF_UNIX"):
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(state.encode())
    except OSError as exc:
        print(f"sd_notify 실패({state}): {exc}", flush=True)


def _install_shutdown_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """systemd가 보내는 SIGTERM에 게이트웨이를 정상 종료시킨다.

    discord.Client.run()은 SIGTERM을 잡지 않아 프로세스가 정리 없이 즉사하고,
    처리 중이던 상호작용 응답과 커밋 직전 트랜잭션이 유실된다.
    """
    stopping = False

    def request_stop(name: str) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        print(f"{name} 수신. 봇을 정상 종료합니다.", flush=True)
        sd_notify("STOPPING=1")
        loop.create_task(bot.close())

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, request_stop, name)
        except NotImplementedError:
            # Windows 이벤트 루프는 add_signal_handler를 지원하지 않는다.
            signal.signal(
                sig,
                lambda _sig, _frame, _name=name: loop.call_soon_threadsafe(
                    request_stop, _name
                ),
            )


async def run_bot(token: str) -> None:
    _install_shutdown_handlers(asyncio.get_running_loop())
    async with bot:
        await bot.start(token)


def main() -> None:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN이 없습니다. 로컬은 .env, 서버는 systemd EnvironmentFile을 확인하세요."
        )

    init_db()
    verify_schema()
    print(f"DB: {config.DB_PATH} (schema v{SCHEMA_VERSION})", flush=True)

    # bot.run()을 우회하므로 discord.py 로깅을 직접 켠다 (journald로 흘러간다).
    discord.utils.setup_logging()
    asyncio.run(run_bot(token))


if __name__ == "__main__":
    main()
