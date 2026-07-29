"""돌 세공 예측 — 강화 단계를 올릴지 멈출지 예측하는 미니게임.

성공률이 단계마다 떨어지고, 진행은 운영자가 `/세공진행` 패널로 한 단계씩 돌린다.
"""

from __future__ import annotations

import json
import random
import sqlite3
from typing import Any

import discord

from .. import runtime
from ..config import (
    CUTTING_LEVEL_CHOICES,
    CUTTING_REWARD_POINTS,
    CUTTING_SUCCESS_RATES,
)
from ..db import (
    db,
    now_text,
)
from ..scoring import (
    add_score,
)
from ..rounds import (
    round_extra,
)
from ..ui import (
    member_name,
    mention_list,
)
from ..permissions import (
    is_operator_user,
)
from .registry import (
    CreateContext,
    CreateResult,
    SettleContext,
    register_creator,
    register_settler,
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


def create_cutting_round(ctx: CreateContext) -> CreateResult:
    ctx.extra.update({"max_level": 6})
    cursor = ctx.conn.execute(
        """
        INSERT INTO rounds
            (type, title, question, status, channel_id, extra_json, created_at)
        VALUES ('cutting', ?, ?, 'open', ?, ?, ?)
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
        embed=build_cutting_embed(round_id, ctx.title),
        view=cutting_vote_view(round_id),
    )


register_creator("cutting", create_cutting_round)


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
            await runtime.round_services().publish_event_summary(round_row["event_session_id"])
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
                await runtime.round_services().publish_event_summary(round_row["event_session_id"])
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
        await runtime.round_services().publish_event_summary(round_row["event_session_id"])


async def run_command(interaction: discord.Interaction) -> None:
    """`/세공진행` 본체. 슬래시 등록 껍데기는 bot.py에 있다."""
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


def settle_cutting(ctx: SettleContext) -> discord.Embed:
    """예측만 집계해 공개한다. 점수는 `/세공진행` 패널이 단계를 돌릴 때 지급된다."""
    round_row, votes = ctx.round_row, ctx.votes
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
    return embed


register_settler("cutting", settle_cutting)
