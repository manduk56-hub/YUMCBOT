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
    AutoCloseContext,
    CreateContext,
    RoundCreationError,
    SettleContext,
    auto_close_handler_for,
    creator_for,
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
from yumcbot.ui import vote_view
from yumcbot.permissions import (
    admin_only,
    is_operator_user,
    operator_role_ids,
)
from yumcbot import discussion
from yumcbot.discussion import (
    AnonymousDiscussionModal,
    anonymous_discussion_view,
    archive_round_discussion_thread,
    assign_anonymous_number,
    build_anonymous_guide_embed,
    build_anonymous_message_embed,
    count_discussion_messages,
    thread_discussion_view,
)
from yumcbot.games import balance, cutting, defuse, top3, unique_number
from yumcbot.games.defuse import (
    build_defuse_embed,
)
from yumcbot.games.unique_number import (
    build_unique_number_embed,
    unique_number_reward,
    unique_number_vote_view,
)
from yumcbot.games.balance import (
    advance_minority_balance_initial,
    build_balance_embed,
    build_minority_balance_revote_embed,
    start_minority_balance_revote,
)
from yumcbot.games.cutting import (
    build_cutting_embed,
    build_cutting_progress_embed,
    cutting_action_view,
    cutting_progress_bar,
    cutting_reward_text,
    cutting_success_rate,
    cutting_success_rate_text,
    finalize_cutting_round,
    handle_cutting_button,
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
from yumcbot.games import trust
from yumcbot.games.trust import (
    advance_trust_registration,
    build_trust_decision_embed,
    build_trust_pair_result_embed,
    handle_trust_decision,
    handle_trust_join,
    publish_trust_pair_results,
    refresh_main_trust_board,
    settle_trust,
    trust_decision_view,
    trust_pair_intro,
    trust_registration_view,
)
from yumcbot.games import sand
from yumcbot.games.sand import (
    advance_sand_registration,
    apply_sand_action,
    autocomplete_sand_pair,
    build_sand_pair_embed,
    build_sand_pair_result_embed,
    build_sand_play_embed,
    build_sand_registration_embed,
    find_sand_pair,
    handle_sand_action,
    handle_sand_join,
    handle_sand_number,
    publish_sand_pair_results,
    refresh_main_sand_board,
    refresh_or_finish_sand_round,
    refresh_sand_pair_board,
    sand_games_complete,
    sand_pair_intro,
    sand_pair_settlement_text,
    sand_pair_status_text,
    sand_pair_thread_matches,
    sand_pair_threshold,
    sand_play_view,
    sand_registration_view,
    save_sand_pair_message_id,
    settle_sand,
    start_sand_game,
    store_sand_thread_ids,
)
from yumcbot.games import bail
from yumcbot.games.bail import (
    advance_bail_negotiation,
    advance_bail_registration,
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
    find_bail_pair,
    handle_bail_offer,
    handle_bail_response,
    handle_bail_scoop,
    load_open_bail_pair,
    publish_bail_pair_results,
    redraw_bail_pair_board,
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

    def cancel_auto_close(self, round_id: int) -> None:
        """게임이 제한 시간보다 먼저 끝났을 때 남은 자동 마감 타이머를 버린다."""
        task = self.auto_close_tasks.pop(round_id, None)
        if task:
            task.cancel()

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

            # 등록 마감 등 게임별 단계 전이는 각 게임 모듈이 register_auto_close_handler()로
            # 올린 훅으로 간다. 여기에 게임별 분기를 다시 넣지 말 것 — 훅이 없거나 False를
            # 돌려주면(예: 가라앉는 배 실행 단계처럼 제한 시간을 끝까지 써야 하는 경우)
            # 아래 close_round_record로 최종 정산한다.
            handler = auto_close_handler_for(round_game_key(row))
            if handler is not None:
                handled = await handler(
                    AutoCloseContext(round_id=round_id, row=row, channel=channel, guild=guild)
                )
                if handled:
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


def build_thread_notice_embed(round_id: int, thread: discord.Thread, title: str, guide: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=f"{thread.mention}\n{guide}",
        color=discord.Color.gold(),
    )
    embed.add_field(name="👉 참여 방법", value="위 스레드를 눌러 의견을 남겨주세요.", inline=False)
    return embed


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


register_settler("bail", settle_bail)


register_settler("sand", settle_sand)


register_settler("trust", settle_trust)


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

        # 정산은 전부 게임 모듈이 레지스트리에 올린 훅으로 간다. 여기에 게임별 분기를
        # 다시 넣지 말 것 — 새 게임은 자기 모듈에서 register_settler()로 등록한다.
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


class BotRoundServices:
    """게임 모듈이 라운드를 끝낼 때 쓰는 통로. runtime에 등록해두고 거기서 꺼내 쓴다.

    게임 모듈은 close_round_record를 직접 import 할 수 없다 — 그쪽이 레지스트리를
    거쳐 다시 게임 모듈을 부르므로 순환이 된다.
    """

    @staticmethod
    def close_round(
        round_id: int, guild: Any, closed_by: str
    ) -> tuple[str, discord.Embed] | None:
        return close_round_record(round_id, guild, closed_by=closed_by)

    @staticmethod
    def next_mini_game_view(round_id: int) -> discord.ui.View | None:
        return planned_mini_game_view(round_id)

    @staticmethod
    async def publish_event_summary(event_session_id: int | None) -> None:
        await publish_event_summary_if_complete(event_session_id)


runtime.set_round_services(BotRoundServices())


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

        # 라운드 INSERT·embed·view는 각 게임 모듈이 register_creator()로 올린 훅이 만든다.
        # 여기에 게임별 분기를 다시 넣지 말 것 — 컬럼 구성(밸런스만 option_a/b를 쓰는 등)이
        # 게임마다 달라 훅이 직접 INSERT까지 한다.
        creator = creator_for(게임)
        if creator is None:
            await interaction.followup.send(f"'{게임}'은(는) 아직 지원하지 않는 게임입니다.", ephemeral=True)
            return
        try:
            created = creator(
                CreateContext(
                    conn=conn,
                    game_key=게임,
                    title=제목,
                    duration_minutes=int(진행시간분),
                    close_at=close_at,
                    extra=extra,
                    channel_id=main_channel_id,
                    event_session_id=event_session_id,
                    option_a=선택_a,
                    option_b=선택_b,
                    negotiation_minutes=협상시간분,
                    execution_minutes=실행시간분,
                )
            )
        except RoundCreationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        round_id = created.round_id
        embed = created.embed
        view = created.view

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
        transition = await advance_trust_registration(int(round_row["id"]))
        if not transition:
            await interaction.followup.send("참여자 모집을 마감하지 못했습니다.", ephemeral=True)
            return
        task = bot.auto_close_tasks.pop(int(round_row["id"]), None)
        if task:
            task.cancel()
        if transition["state"] == "decision":
            bot.schedule_auto_close(int(round_row["id"]), float(transition["close_at"]))
            await interaction.followup.send("참여자 모집을 마감하고 랜덤 매칭 및 3분 선택을 시작했습니다.", ephemeral=True)
        else:
            await interaction.followup.send("참여자가 부족해 딜레마의 섬을 취소했습니다.", ephemeral=True)
        return

    if round_row["type"] == "sand" and round_extra(round_row).get("sand_phase") == "registration":
        # 짝마다 비공개 방을 여느라 API 호출이 여러 번이다. 3초 응답 제한을 넘기므로 먼저 defer한다.
        await interaction.response.defer(ephemeral=True)
        transition = await advance_sand_registration(int(round_row["id"]))
        if not transition:
            await interaction.followup.send("참여자 모집을 마감하지 못했습니다.", ephemeral=True)
            return
        task = bot.auto_close_tasks.pop(int(round_row["id"]), None)
        if task:
            task.cancel()
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
            await advance_bail_registration(round_id)
            if registration
            else await advance_bail_negotiation(round_id)
        )
        if not transition:
            await interaction.followup.send("현재 단계를 마감하지 못했습니다.", ephemeral=True)
            return
        task = bot.auto_close_tasks.pop(round_id, None)
        if task:
            task.cancel()
        if transition["state"] == "cancelled":
            await interaction.followup.send("참여자가 부족해 가라앉는 배를 취소했습니다.", ephemeral=True)
            return
        if registration:
            await interaction.followup.send(
                "참여자 모집을 마감하고 1:1 협상 단계를 시작했습니다.", ephemeral=True
            )
        else:
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
        transition = await advance_minority_balance_initial(int(round_row["id"]))
        if not transition:
            await interaction.followup.send("1차 집계를 마감하지 못했습니다.", ephemeral=True)
            return
        task = bot.auto_close_tasks.pop(int(round_row["id"]), None)
        if task:
            task.cancel()
        bot.schedule_auto_close(int(round_row["id"]), float(transition["close_at"]))
        if transition["published"]:
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
    await cutting.run_command(interaction)


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
