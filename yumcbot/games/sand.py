"""모래성 게임 — 짝마다 숫자를 골라 겨루는 메인게임.

진행판은 **상호작용마다 지웠다가 맨 아래에 다시 붙인다**(`refresh_sand_pair_board`).
ephemeral 응답이 쌓여 진행판을 위로 밀어내기 때문이다. 두 사람이 동시에 누르면
`interaction.message`가 이미 사라진 옛 진행판일 수 있어, 방별 락을 잡고 DB에서 진행판
메시지 id를 다시 읽는다. (가라앉는 배는 반대로 제자리에서 edit만 한다 — 협상이 여러 번
오가는 게임이라 매번 지웠다 붙이면 방이 깜빡인다.)

마감 시점에 안 끝난 짝은 `autocomplete_sand_pair`로 끝까지 진행시킨다. 미완료를 0점으로
남기면 '안 누르고 버티기'가 이득이 되기 때문이다.

모든 짝이 게임을 마치면 제한 시간을 기다리지 않고 그 자리에서 정산한다
(`sand_games_complete`). 짝끼리 독립적이라 남은 방이 없으면 더 기다릴 이유가 없다.
"""

from __future__ import annotations

import json
import random
import time
import sqlite3
from typing import Any

import discord

from .. import runtime
from ..config import (
    SAND_NUMBER_CHOICES,
    SAND_OUTCOME_LABELS,
    SAND_PLAY_SECONDS,
    SAND_RULES_TEXT,
)
from ..db import (
    db,
    now_text,
)
from ..activity import (
    log_activity,
)
from ..scoring import (
    add_score,
    ensure_user,
)
from ..rounds import (
    can_join_round,
    round_extra,
)
from ..ui import (
    close_time_text,
    field_text,
    member_name,
)
from . import pairing
from .pairing import (
    build_random_trust_pairs,
    create_pair_thread,
    emergency_join_notice,
    emergency_join_view,
    recent_match_pairs,
    save_pair_thread_id,
    warn_failed_pair_threads,
)
from .registry import SettleContext, register_settler


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
        pairing.board_client().release_board_lock(int(thread_id))
        thread = await runtime.resolve_thread(thread_id)
        if thread is None:
            continue
        try:
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

    async with pairing.board_client().sand_board_lock(channel.id):
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
    services = runtime.round_services()
    result = services.close_round(round_id, interaction.guild, "모든 1:1 게임 종료")
    if not result:
        return
    pairing.board_client().cancel_auto_close(round_id)
    await publish_sand_pair_results(round_id)

    # followup은 버튼을 누른 비공개 방으로 들어간다. 라운드 결과는 메인 채널에 알려야 한다.
    _, result_embed = result
    channel = await runtime.resolve_text_channel(channel_id)
    if channel is not None:
        try:
            await channel.send(
                embed=result_embed, view=services.next_mini_game_view(round_id)
            )
        except discord.HTTPException:
            pass
    await services.publish_event_summary(round_row["event_session_id"])


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


def settle_sand(ctx: SettleContext) -> discord.Embed:
    """짝마다 숫자를 골라 겨루는 게임. 마감 시점에 안 끝난 짝은 자동으로 끝까지 진행시킨다.

    미완료를 0점으로 남기지 않는 이유는 그게 '안 누르고 버티기'를 이득으로 만들기
    때문이다(`autocomplete_sand_pair`).
    """
    conn, round_row, extra, guild = ctx.conn, ctx.round_row, ctx.extra, ctx.guild
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
    return embed

register_settler("sand", settle_sand)
