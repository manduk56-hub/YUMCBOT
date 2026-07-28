"""상위 3명 견제 — 후보 셋 중 하나를 찍거나 `선택하지 않는다`를 고른다.

`선택하지 않는다`가 후보 득표 합보다 많으면 그쪽이 이긴다. 견제가 몰릴수록
견제당한 사람만 손해라서, 아무도 안 찍는 담합이 성립할 수 있게 만든 장치다.
"""

from __future__ import annotations

from typing import Any

import discord

from ..config import (
    TOP3_NONE_CHOICE,
)
from ..scoring import (
    add_score,
)
from ..ui import (
    member_name,
)
from .registry import SettleContext, register_settler


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


def settle_top3(ctx: SettleContext) -> discord.Embed:
    """`선택하지 않는다`가 후보 득표 합보다 많으면 그쪽이 이긴다.

    아니면 최다 득표 후보만 -1점이라, 견제가 몰릴수록 찍힌 사람만 손해다.
    """
    conn, round_row, extra, guild, votes = ctx.conn, ctx.round_row, ctx.extra, ctx.guild, ctx.votes
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
    return embed


register_settler("top3", settle_top3)
