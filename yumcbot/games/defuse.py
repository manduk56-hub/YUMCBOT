"""몰리면 터진다! — 세 선 중 하나를 고르고, 가장 많이 몰린 선을 고른 사람이 감점.

DB 호환을 위해 내부 키는 `defuse4`(4선 시절)를 유지하지만 현재 규칙은 3선 방식이다.
새 라운드는 `settlement_rule = "top_vote_penalty"`로 구방식과 구분한다.
"""

from __future__ import annotations

import discord

from ..config import (
    DEFUSE_COLORS,
)
from ..scoring import (
    add_score,
)
from ..ui import (
    add_field_divider,
    member_name,
    mention_list,
)
from .registry import SettleContext, register_settler


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


def settle_defuse(ctx: SettleContext) -> discord.Embed:
    """가장 많이 몰린 선을 고른 사람이 감점된다(`settlement_rule=top_vote_penalty`).

    동률이면 그 선들이 모두 터진다. 옛 라운드는 안전선 방식이라 분기가 갈린다.
    """
    conn, round_row, extra, guild, votes = ctx.conn, ctx.round_row, ctx.extra, ctx.guild, ctx.votes
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
    return embed


register_settler("defuse4", settle_defuse)
