"""밸런스 게임 — 두 선택지 중 하나를 고른다. 두 가지 모드가 한 파일에 있다.

- 일반(`balance`): 다수파가 +1점. 한쪽으로 몰리면 밸런스가 성립하지 않은 것으로
  보고 고른 사람 전원에게 -2점. 소수파가 단 한 명이면 그 사람만 +2점.
- 소수파(`minority_balance`): 1차 집계 뒤 **재선택 단계**가 한 번 더 있고,
  인원이 적은 쪽이 이긴다. 참가자에게는 1차가 끝날 때까지 일반 밸런스로 보인다.

두 모드는 `extra_json.mode`로 갈리며 `rounds.type`은 둘 다 `balance`다.
"""

from __future__ import annotations

import json
import time
import sqlite3
from typing import Any

import discord

from .. import runtime
from ..config import (
    BALANCE_VOTE_CHOICES,
)
from ..db import (
    db,
    now_text,
)
from ..rounds import (
    round_extra,
)
from ..ui import (
    close_time_text,
    member_name,
    vote_view,
)
from ..discussion import (
    build_anonymous_guide_embed,
    count_discussion_messages,
    thread_discussion_view,
)
from ..scoring import (
    add_score,
)
from .registry import (
    AutoCloseContext,
    CreateContext,
    CreateResult,
    RoundCreationError,
    SettleContext,
    register_auto_close_handler,
    register_creator,
    register_settler,
)


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


def create_balance_round(ctx: CreateContext) -> CreateResult:
    """일반/소수파 두 모드를 한 곳에서 만든다 — `extra_json.mode`로 갈린다.

    참가자에게는 1차 집계가 끝날 때까지 일반 밸런스 게임으로 보여야 하므로,
    소수파일 때만 `mode`/`balance_phase`를 얹는다.
    """
    if not ctx.option_a or not ctx.option_b:
        raise RoundCreationError("밸런스 게임은 `선택_a`와 `선택_b`가 필요합니다.")

    if ctx.game_key == "minority_balance":
        ctx.extra.update({"mode": "minority_balance", "balance_phase": "initial"})

    cursor = ctx.conn.execute(
        """
        INSERT INTO rounds
            (type, title, question, option_a, option_b, status, channel_id, extra_json, created_at)
        VALUES ('balance', ?, ?, ?, ?, 'open', ?, ?, ?)
        """,
        (
            "밸런스 게임 라운드",
            ctx.title,
            ctx.option_a,
            ctx.option_b,
            ctx.channel_id,
            json.dumps(ctx.extra, ensure_ascii=False),
            now_text(),
        ),
    )
    round_id = int(cursor.lastrowid)
    return CreateResult(
        round_id=round_id,
        embed=build_balance_embed(round_id, ctx.title, ctx.option_a, ctx.option_b),
        view=vote_view(round_id, BALANCE_VOTE_CHOICES),
    )


register_creator("balance", create_balance_round)
register_creator("minority_balance", create_balance_round)


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
        thread = await runtime.resolve_thread(thread_id)
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


async def advance_minority_balance_initial(round_id: int) -> dict[str, Any] | None:
    """1차 집계를 마감하고 재선택 안내 게시까지 한 번에 진행한다.

    자동 마감 타이머와 `/라운드마감` 양쪽에서 똑같이 필요한 시퀀스라 여기 모아둔다.
    실패(1차 집계 마감 실패)면 `None`. 결과 dict에 `published`(안내 게시 성공 여부)를 얹는다.
    """
    transition = start_minority_balance_revote(round_id)
    if not transition:
        return None
    transition["published"] = await publish_minority_balance_revote(transition)
    return transition


async def handle_minority_balance_auto_close(ctx: AutoCloseContext) -> bool:
    """1차 집계 단계 마감 타이머가 울렸을 때만 처리한다. 재선택 단계는 정산으로 넘긴다."""
    if round_extra(ctx.row).get("balance_phase") != "initial":
        return False
    transition = await advance_minority_balance_initial(ctx.round_id)
    if transition:
        runtime.auto_close_scheduler().schedule_auto_close(ctx.round_id, float(transition["close_at"]))
    return True


register_auto_close_handler("minority_balance", handle_minority_balance_auto_close)


def settle_balance(ctx: SettleContext) -> discord.Embed:
    """두 모드를 한 곳에서 집계한다 — `extra_json.mode`로 갈린다.

    일반은 다수파 +1점이되 한쪽으로 몰리면 밸런스가 성립하지 않은 것으로 보고
    고른 사람 전원 -2점, 소수파가 한 명뿐이면 그 사람만 +2점이다.
    소수파 모드는 재선택 결과만 집계해 인원이 적은 쪽에 +1점을 준다.
    """
    conn, round_row, extra, guild, votes = ctx.conn, ctx.round_row, ctx.extra, ctx.guild, ctx.votes
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
    return embed


register_settler("balance", settle_balance)
register_settler("minority_balance", settle_balance)
