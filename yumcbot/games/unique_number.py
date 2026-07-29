"""홍피겔만의 황금금고 — 아무도 안 고른 숫자 중 가장 큰 것을 고른 사람이 이긴다.

`rounds.type`은 제거된 게임 `grid`를 재사용하고 `extra_json.mode`로 갈라 쓴다.
"""

from __future__ import annotations

import json

import discord

from ..config import (
    UNIQUE_NUMBER_CHOICES,
)
from ..db import (
    now_text,
)
from ..scoring import (
    add_score,
)
from ..ui import (
    add_field_divider,
    field_text,
    member_name,
    mention_list,
)
from .registry import (
    CreateContext,
    CreateResult,
    SettleContext,
    register_creator,
    register_settler,
)


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


def create_unique_number_round(ctx: CreateContext) -> CreateResult:
    """`rounds.type`에 남아 있는 제거된 게임 `grid` 슬롯을 재사용하고 `mode`로 구분한다."""
    ctx.extra.update({"mode": "unique_number"})
    cursor = ctx.conn.execute(
        """
        INSERT INTO rounds
            (type, title, question, status, channel_id, extra_json, created_at)
        VALUES ('grid', ?, ?, 'open', ?, ?, ?)
        """,
        (
            ctx.title,
            ctx.title,
            ctx.channel_id,
            json.dumps(ctx.extra, ensure_ascii=False),
            now_text(),
        ),
    )
    round_id = int(cursor.lastrowid)
    return CreateResult(
        round_id=round_id,
        embed=build_unique_number_embed(round_id, ctx.title),
        view=unique_number_vote_view(round_id),
    )


register_creator("unique_number", create_unique_number_round)


def settle_unique_number(ctx: SettleContext) -> discord.Embed:
    """아무도 겹치지 않은 숫자 중 가장 큰 것을 고른 사람만 보상을 받는다."""
    conn, round_row, guild, votes = ctx.conn, ctx.round_row, ctx.guild, ctx.votes
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
    return embed


register_settler("unique_number", settle_unique_number)
