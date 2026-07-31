from __future__ import annotations

import asyncio
import json
import math
import os
import random
import secrets
import signal
import socket
import sqlite3
import time
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
    BAIL_FAILED_REWARD_POOL,
    BAIL_NEGOTIATION_MAX_OFFERS,
    BAIL_OUTCOME_LABELS,
    BAIL_PROGRESS_STAGES,
    BAIL_PROGRESS_SUCCESS_IMAGE,
    BAIL_PROGRESS_SUNK_CAPTION,
    BAIL_PROGRESS_SUNK_IMAGE,
    BAIL_PROGRESS_UPDATE_INTERVAL,
    BAIL_REQUIRED_SCOOPS,
    BAIL_REWARD_CHOICES,
    BAIL_REWARD_POOL,
    BAIL_SCOOP_COST,
    BAIL_SINK_PENALTY_EVEN,
    BAIL_SINK_PENALTY_HIGH,
    BAIL_SINK_PENALTY_LOW,
    BALANCE_VOTE_CHOICES,
    BASEBALL_BUTTON_GRID_SIZE,
    BASEBALL_BUTTON_SLOT_COUNT,
    BASEBALL_IMAGE_FILENAME,
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
    KST_LABEL,
    MATCH_HISTORY_ROUNDS,
    ROULETTE_BETTING_SECONDS,
    ROULETTE_MAX_BET,
    ROULETTE_MIN_BET,
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
    SettleContext,
    auto_close_handler_for,
    register_settler,
    settler_for,
)
from yumcbot.ui import (
    add_field_divider,
    choice_display,
    close_time_text,
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
    recent_match_pairs,
    save_pair_thread_id,
    warn_failed_pair_threads,
)
from yumcbot.games import trust
from yumcbot.games.trust import (
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
    advance_or_finish_bail_round,
    apply_bail_offer,
    apply_bail_response,
    apply_bail_scoop_click,
    autocomplete_bail_pair,
    bail_executions_complete,
    bail_negotiations_complete,
    bail_new_pair,
    bail_pair_intro,
    bail_pair_settlement_text,
    bail_pair_status_text,
    bail_pair_thread_matches,
    bail_pair_threads,
    bail_pair_view,
    bail_progress_image_filename,
    bail_progress_stage_image,
    bail_progress_stage_index,
    bail_progress_view,
    bail_reward_share,
    bail_rules_text,
    bail_score_text,
    bail_side_key,
    bail_split_text,
    build_bail_pair_embed,
    build_bail_pair_result_embed,
    build_bail_play_embed,
    build_bail_progress_embed,
    build_bail_progress_embeds,
    build_bail_registration_embed,
    find_bail_pair,
    finish_bail_execution_round,
    handle_bail_offer,
    handle_bail_response,
    handle_bail_scoop,
    load_open_bail_pair,
    publish_bail_pair_results,
    redraw_bail_pair_board,
    redraw_bail_progress_board,
    refresh_all_bail_progress_boards,
    refresh_bail_pair_board,
    refresh_main_bail_board,
    resolve_bail_pair,
    save_bail_pair_message_id,
    save_bail_pair_progress_message_id,
    settle_bail,
    start_bail_execution,
    start_bail_negotiation,
    store_bail_thread_ids,
    write_bail_progress_message,
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
    SeasonClosedError,
    add_score,
    ensure_user,
    get_afk_coins,
    get_roulette_stack,
    grant_afk_coins,
    reset_roulette_stack_if_escaped,
    set_roulette_stack,
    spend_afk_coins,
)
from yumcbot import roulette
from yumcbot.roulette import (
    active_roulette_game_for_user,
    build_roulette_embed,
    count_roulette_games_today,
    count_roulette_games_with_opponent_today,
    handle_roulette_accept,
    handle_roulette_bet,
    handle_roulette_decline,
    handle_roulette_score,
    handle_roulette_settle,
    handle_roulette_trigger,
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
from yumcbot import ranking
from yumcbot.ranking import (
    SeasonAlreadyClosedError,
    SeasonFinalizationBlockedError,
    build_final_ranking_embeds,
    cancel_season_end,
    confirm_season_end,
    fetch_final_ranking_rows,
    fetch_ranking_rows,
    fetch_ranking_stat_rows,
    finalize_season_rankings,
    format_anonymous_ranking_lines,
    format_final_ranking_lines,
    format_my_score_message,
    format_public_ranking_lines,
    is_placeholder_display_name,
    refresh_placeholder_user_names,
    season_end_confirmation_embed,
    season_end_confirmation_view,
    season_end_status_embed,
    season_finalization_blockers,
    season_is_closed,
    write_admin_score_report,
)
from yumcbot import events
from yumcbot.events import (
    StartSetupModal,
    close_completed_event,
    close_round_record,
    event_join_view,
    force_close_current_event,
    planned_mini_game_view,
    publish_event_summary_if_complete,
)


# TODO(activity-log): 운영자용 조회/내보내기 수단이 아직 없다.
# 당분간은 서버에서 직접 조회한다:
#   sqlite3 /var/lib/yumcbot/data/event_game.sqlite3 \
#     "SELECT created_at, category, action, actor_user_id, payload_json
#      FROM activity_logs WHERE event_session_id = 12 ORDER BY created_at"
# 필요해지면 /로그 슬래시 명령이나 CSV 내보내기를 붙인다.




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


# 하루 횟수를 언제 차감할지의 기준.
#   playing/finished          : 게임이 성립했으므로 양쪽 모두 차감
#   betting 만료(accepted_at O): 베팅을 넣지 않은 쪽만 차감
#   pending 만료 / 거절        : 아무도 차감하지 않음 (거절은 언제나 공짜)
#
# betting 만료를 미입력자에게만 물리는 이유: 수락까지 받아놓고 베팅을 안 넣으면
# 상대는 동시 1결투 제한에 묶여 아무것도 못 한다. 차감이 없으면 무한 반복이 가능하다.


register_settler("baseball", settle_baseball)


SEASON_CLOSED_MESSAGE = (
    "시즌이 종료되어 `/점수`와 관리자용 `/시즌종료` 재브리핑만 사용할 수 있습니다."
)


class SeasonCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        command_name = getattr(getattr(interaction, "command", None), "name", "")
        if command_name in {"점수", "시즌종료"}:
            return True
        with db() as conn:
            closed = season_is_closed(conn)
        if closed:
            raise app_commands.CheckFailure(SEASON_CLOSED_MESSAGE)
        return True


class EventBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        # 멤버 캐시를 채워 member_name() 미스를 없앤다. Developer Portal의
        # SERVER MEMBERS INTENT 토글이 함께 ON이어야 로그인이 된다 (issue #19).
        intents.members = True
        super().__init__(intents=intents)
        self.tree = SeasonCommandTree(self)
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
            if season_is_closed(conn):
                return
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

    def cancel_baseball_timers(self, round_id: int) -> None:
        """라운드가 끝나 야구 타이머 두 개(투구·활성화)가 더 이상 필요 없을 때 함께 버린다."""
        pitch_task = self.baseball_pitch_tasks.pop(round_id, None)
        if pitch_task:
            pitch_task.cancel()
        activation_task = self.baseball_activation_tasks.pop(round_id, None)
        if activation_task:
            activation_task.cancel()

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

            if round_game_key(row) == "bail" and round_extra(row).get("bail_phase") == "execution":
                # 참가자가 전부 비공개 방에 들어가 있어서 메인 채널 카운트다운은 의미가 없다.
                # 이미 조기 정산된 방은 기다리기만 하면 되므로 아직 안 끝난 방에만 띄운다.
                unresolved_threads = [
                    thread
                    for pair, thread in await bail_pair_threads(round_id)
                    if str(pair.get("phase")) != "resolved"
                ]
                await asyncio.gather(
                    *(
                        publish_auto_close_countdown(thread, "퍼내기 마감", close_at)
                        for thread in unresolved_threads
                    )
                )
            else:
                await publish_auto_close_countdown(channel, auto_close_action_label(row), close_at)

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

        with db() as conn:
            season_closed = season_is_closed(conn)
        if season_closed:
            await interaction.response.send_message(
                SEASON_CLOSED_MESSAGE,
                ephemeral=True,
            )
            return

        parts = custom_id.split(":")
        if len(parts) < 3:
            await interaction.response.send_message("알 수 없는 버튼입니다.", ephemeral=True)
            return

        _, action = parts[:2]
        if action == "seasonend":
            if len(parts) != 3 or parts[2] not in {"confirm", "cancel"}:
                await interaction.response.send_message(
                    "잘못된 시즌 종료 요청입니다.", ephemeral=True
                )
                return
            if not is_operator_user(interaction.user):
                await interaction.response.send_message(
                    "이 버튼은 관리자 권한 또는 운영자 역할이 필요합니다.",
                    ephemeral=True,
                )
                return
            if parts[2] == "confirm":
                await ranking.confirm_season_end(interaction)
            else:
                await ranking.cancel_season_end(interaction)
            return

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

        if action == "bailoffer":
            if len(parts) != 4 or parts[3] not in BAIL_REWARD_CHOICES:
                await interaction.response.send_message("잘못된 보상금 제안입니다.", ephemeral=True)
                return
            try:
                round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return
            await handle_bail_offer(interaction, round_id, int(parts[3]))
            return

        if action == "bailscoop":
            # 퍼내기는 선택값 없이 누른 횟수만큼 누적되는 버튼이라 round_id만 온다.
            if len(parts) != 3:
                await interaction.response.send_message("잘못된 퍼내기 요청입니다.", ephemeral=True)
                return
            try:
                round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return
            await handle_bail_scoop(interaction, round_id)
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


@bot.tree.command(name="오픈", description="새 이벤트 참가 모집을 열고 진행 흐름을 안내합니다.")
@app_commands.default_permissions()
@admin_only()
async def open_event(interaction: discord.Interaction) -> None:
    await events.open_event_command(interaction)


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
    await events.start_command(interaction, 메인게임, 미니게임)


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
    await events.start_mini_game_command(interaction, 게임)


@bot.tree.command(name="준비", description="야구 투구를 준비하고 와인드업을 알립니다.")
@app_commands.default_permissions()
@admin_only()
async def baseball_prepare(interaction: discord.Interaction) -> None:
    await baseball.prepare_command(interaction)


@bot.tree.command(name="라운드마감", description="현재 열린 라운드를 마감하고 결과를 공개합니다.")
@app_commands.default_permissions()
@admin_only()
async def close_round(interaction: discord.Interaction) -> None:
    await events.close_round_command(interaction)


@bot.tree.command(name="시즌종료", description="누적 점수의 최종 순위를 확정하고 모든 게임을 종료합니다.")
@app_commands.default_permissions()
@admin_only()
async def end_season(interaction: discord.Interaction) -> None:
    await ranking.season_end_command(interaction)


@bot.tree.command(name="이벤트종료", description="개발 중인 현재 이벤트를 남은 게임과 관계없이 강제로 종료합니다.")
@app_commands.default_permissions()
@admin_only()
async def force_end_event(interaction: discord.Interaction) -> None:
    await events.force_end_event_command(interaction)


@bot.tree.command(name="세공진행", description="가장 최근 마감된 돌 세공 예측의 관리자 진행 패널을 엽니다.")
@app_commands.default_permissions()
@admin_only()
async def run_cutting(interaction: discord.Interaction) -> None:
    await cutting.run_command(interaction)


@bot.tree.command(name="이벤트리스트", description="진행 중인 이벤트와 라운드를 확인합니다.")
@app_commands.default_permissions()
@admin_only()
async def list_events(interaction: discord.Interaction) -> None:
    await events.list_events_command(interaction)


@bot.tree.command(name="러시안룰렛", description="한 플레이어에게 점수를 건 1:1 러시안룰렛을 신청합니다.")
@app_commands.describe(플레이어="결투를 신청할 플레이어")
async def russian_roulette(
    interaction: discord.Interaction, 플레이어: discord.Member
) -> None:
    await roulette.russian_roulette_command(interaction, 플레이어)


@bot.tree.command(name="잠수", description="내 잠수 코인을 확인합니다.")
async def show_afk_coins(interaction: discord.Interaction) -> None:
    await ranking.show_afk_coins_command(interaction)


@bot.tree.command(
    name="지목",
    description=f"잠수 코인 {SCORE_REVEAL_COST}개를 써서 한 사람의 점수를 확인합니다.",
)
@app_commands.describe(사용자="점수를 확인할 사람")
async def reveal_score(interaction: discord.Interaction, 사용자: discord.Member) -> None:
    await ranking.reveal_score_command(interaction, 사용자)


@bot.tree.command(name="점수", description="3등 점수와 내 점수를 확인합니다.")
async def show_score(interaction: discord.Interaction) -> None:
    await ranking.show_score_command(interaction)


@bot.tree.command(name="랭킹", description="이름이 표시되는 TOP 10 또는 특정 등수를 공개합니다.")
@app_commands.default_permissions()
@admin_only()
@app_commands.describe(등수="이 등수만 공개합니다. 생략하면 TOP 10을 공개합니다.")
async def show_ranking(
    interaction: discord.Interaction,
    등수: app_commands.Range[int, 1, 1000] | None = None,
) -> None:
    await ranking.show_ranking_command(interaction, 등수)


@bot.tree.command(name="익명랭킹", description="이름을 숨긴 TOP 10 또는 특정 등수를 공개합니다.")
@app_commands.default_permissions()
@admin_only()
@app_commands.describe(등수="이 등수만 익명으로 공개합니다. 생략하면 TOP 10을 공개합니다.")
async def show_anonymous_ranking(
    interaction: discord.Interaction,
    등수: app_commands.Range[int, 1, 1000] | None = None,
) -> None:
    await ranking.show_anonymous_ranking_command(interaction, 등수)


@bot.tree.command(name="데이터백업", description="SQLite 데이터베이스를 백업합니다.")
@app_commands.default_permissions()
@admin_only()
async def backup_data(interaction: discord.Interaction) -> None:
    await ranking.backup_data_command(interaction)


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
