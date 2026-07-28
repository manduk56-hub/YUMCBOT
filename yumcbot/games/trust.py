"""딜레마의 섬 — 짝마다 신뢰/배신을 고르는 메인게임 (죄수의 딜레마).

상호 신뢰는 각 +2점, 단독 배신은 배신자 +5점·상대 0점, 동시 배신은 각 -3점이다.
배신이 확실히 이득인 구간이 있어서, 세 메인게임 중 배신 동기가 가장 뚜렷하다.

짝마다 비공개 스레드를 열고 그 안에서만 선택을 받는다. 방을 잠그는 것은 결과를 공개해
버튼이 필요 없어진 뒤뿐이다(`publish_trust_pair_results`) — 잠근 스레드에서는
`스레드 관리` 권한이 없는 유저의 버튼 클릭을 디스코드가 막기 때문이다.
"""

from __future__ import annotations

import json
import random
import time
from typing import Any

import discord

from .. import runtime
from ..config import (
    TRUST_DECISION_SECONDS,
    TRUST_OUTCOME_LABELS,
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
            thread = await runtime.resolve_thread(str(thread_id))
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


def settle_trust(ctx: SettleContext) -> discord.Embed:
    """짝마다 신뢰/배신을 집계한다 (죄수의 딜레마).

    상호 신뢰 각 +2점, 단독 배신은 배신자 +5점·상대 0점, 동시 배신 각 -3점.
    배신이 확실히 이득인 구간이 있어 세 메인게임 중 배신 동기가 가장 뚜렷하다.
    """
    conn, round_row, extra, guild = ctx.conn, ctx.round_row, ctx.extra, ctx.guild
    votes = ctx.votes
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
    return embed

register_settler("trust", settle_trust)
