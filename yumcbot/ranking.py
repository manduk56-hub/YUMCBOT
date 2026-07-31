"""점수 조회·랭킹 공개·관리자 백업 — `/잠수`·`/지목`·`/점수`·`/랭킹`·`/익명랭킹`·`/데이터백업`.

랭킹은 정상 종료된 게임의 참가 기록이 있는 사람만 보여준다. 시즌이 열려 있을 때는
점수→참여→승리→user_id 순으로 안정적으로 표시하고, `/시즌종료`가 확정되면 완전
동률 그룹만 안전하게 추첨한 고유 순위를 `final_rankings`에서 그대로 읽는다.
"""

from __future__ import annotations

import csv
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import discord

from . import config
from .activity import log_activity
from .config import (
    AFK_COIN_PER_TICK,
    SCORE_REVEAL_COST,
)
from .db import (
    db,
    now_text,
)
from .scoring import (
    ensure_user,
    get_afk_coins,
    spend_afk_coins,
)


class SeasonAlreadyClosedError(RuntimeError):
    """최종 순위가 이미 확정된 뒤 다시 종료하려 할 때 발생한다."""


class SeasonFinalizationBlockedError(RuntimeError):
    """미정산 게임 때문에 현재 시점의 순위를 확정할 수 없을 때 발생한다."""

    def __init__(self, blockers: list[str]) -> None:
        self.blockers = blockers
        super().__init__(" · ".join(blockers))


def is_placeholder_display_name(display_name: str, user_id: str) -> bool:
    """멤버 캐시 미스 때 만들어지는 정확한 `User <id>` 이름인지 확인한다."""
    return display_name == f"User {user_id}"


def season_end_confirmation_embed() -> discord.Embed:
    embed = discord.Embed(
        title="⚠️ 시즌을 종료합니다",
        description=(
            "시즌을 종료하면 현재 점수와 게임 기록으로 최종 순위를 확정합니다.\n"
            "**종료 후에는 `/점수`를 제외한 게임과 명령을 더 이상 진행할 수 없고, "
            "잠수 코인도 적립되지 않습니다.**"
        ),
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="최종 순위 기준",
        value="점수 → 참여 횟수 → 승리 횟수 → 완전 동률 무작위 추첨",
        inline=False,
    )
    return embed


def season_end_cancelled_embed() -> discord.Embed:
    return discord.Embed(
        title="시즌 종료 취소",
        description="시즌 종료가 취소되었습니다. 현재 상태로 게임을 계속 진행할 수 있습니다.",
        color=discord.Color.light_grey(),
    )


def season_end_confirmation_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="시즌종료",
            style=discord.ButtonStyle.danger,
            custom_id="eventbot:seasonend:confirm",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="시즌종료 취소",
            style=discord.ButtonStyle.secondary,
            custom_id="eventbot:seasonend:cancel",
        )
    )
    return view


def format_final_ranking_lines(rows: list[sqlite3.Row]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        display_name = discord.utils.escape_markdown(str(row["display_name"]))
        lines.append(
            f"**{int(row['final_rank'])}위** · {display_name} · **{int(row['total_score'])}점**"
        )
    return lines


def build_final_ranking_embeds(rows: list[sqlite3.Row]) -> list[discord.Embed]:
    """Discord description 제한을 넘지 않게 전체 최종 순위를 여러 임베드로 나눈다."""
    lines = format_final_ranking_lines(rows)
    chunks: list[list[str]] = []
    current: list[str] = []
    current_length = 0
    for line in lines:
        added_length = len(line) + (1 if current else 0)
        if current and current_length + added_length > 3800:
            chunks.append(current)
            current = []
            current_length = 0
        current.append(line)
        current_length += len(line) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append(current)

    embeds: list[discord.Embed] = []
    for chunk_index, chunk in enumerate(chunks, start=1):
        first_rank = sum(len(previous) for previous in chunks[: chunk_index - 1]) + 1
        last_rank = first_rank + len(chunk) - 1
        title = "🏆 시즌 최종 순위"
        if len(chunks) > 1:
            title += f" · {first_rank}~{last_rank}위"
        embeds.append(
            discord.Embed(
                title=title,
                description="\n".join(chunk),
                color=discord.Color.gold(),
            )
        )
    return embeds


async def refresh_placeholder_user_names(
    guild: discord.Guild,
    client: discord.Client,
    current_bot_id: str,
) -> int:
    """`User <id>` 참가자만 REST API로 다시 확인해 실제 표시 이름을 DB에 반영한다."""
    with db() as conn:
        candidates = fetch_ranking_stat_rows(conn, current_bot_id)
    placeholders = [
        row
        for row in candidates
        if is_placeholder_display_name(str(row["display_name"]), str(row["user_id"]))
    ]

    updates: list[tuple[str, str]] = []
    for row in placeholders:
        user_id = str(row["user_id"])
        user: discord.abc.User | None = None
        try:
            user = await guild.fetch_member(int(user_id))
        except discord.NotFound:
            try:
                user = await client.fetch_user(int(user_id))
            except discord.NotFound:
                continue
        display_name = getattr(user, "display_name", None) if user else None
        if display_name and not is_placeholder_display_name(str(display_name), user_id):
            updates.append((str(display_name), user_id))

    if updates:
        with db() as conn:
            conn.executemany(
                "UPDATE users SET display_name = ?, updated_at = ? WHERE user_id = ?",
                [(display_name, now_text(), user_id) for display_name, user_id in updates],
            )
    return len(updates)


async def season_end_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "서버 안에서만 시즌을 종료할 수 있습니다.", ephemeral=True
        )
        return
    with db() as conn:
        if season_is_closed(conn):
            await interaction.response.send_message(
                "이미 시즌이 종료되어 최종 순위가 확정되었습니다.", ephemeral=True
            )
            return
    await interaction.response.send_message(
        embed=season_end_confirmation_embed(),
        view=season_end_confirmation_view(),
    )


async def cancel_season_end(interaction: discord.Interaction) -> None:
    await interaction.response.edit_message(
        embed=season_end_cancelled_embed(),
        view=None,
    )


async def confirm_season_end(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "서버 안에서만 시즌을 종료할 수 있습니다.", ephemeral=True
        )
        return

    await interaction.response.defer()
    current_bot_id = str(interaction.client.user.id) if interaction.client.user else ""
    try:
        refreshed_count = await refresh_placeholder_user_names(
            interaction.guild, interaction.client, current_bot_id
        )
        with db() as conn:
            final_rows = finalize_season_rankings(
                conn,
                current_bot_id,
                str(interaction.user.id),
            )
    except SeasonAlreadyClosedError as error:
        await interaction.followup.send(str(error), ephemeral=True)
        return
    except SeasonFinalizationBlockedError as error:
        await interaction.followup.send(
            "아직 시즌을 종료할 수 없습니다: " + " · ".join(error.blockers),
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        await interaction.followup.send(
            "참가자 이름을 Discord에서 다시 확인하지 못했습니다. 잠시 후 다시 시도해주세요.",
            ephemeral=True,
        )
        return

    completed = discord.Embed(
        title="✅ 시즌 종료 완료",
        description=(
            f"총 **{len(final_rows)}명**의 최종 순위를 확정했습니다.\n"
            "이제 `/점수`를 제외한 게임과 명령은 사용할 수 없습니다."
        ),
        color=discord.Color.green(),
    )
    if refreshed_count:
        completed.set_footer(text=f"플레이스홀더 이름 {refreshed_count}건 갱신")
    await interaction.edit_original_response(embed=completed, view=None)
    for embed in build_final_ranking_embeds(final_rows):
        await interaction.followup.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def format_my_score_message(third_score: int | None, my_score: int) -> str:
    """3등 점수와 내 점수만 알려준다. 이름은 넣지 않는다.

    최종 순위가 확정된 뒤에는 추첨 결과까지 반영된 정확한 3등 행을 사용한다.
    그 전에는 보여주는 값이 점수뿐이라 동점자의 신원은 노출되지 않는다.
    """
    third_text = f"**{third_score}점**" if third_score is not None else "아직 없음"
    return f"3등 점수: {third_text}\n내 점수: **{my_score}점**"


def season_is_closed(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT status FROM season_state WHERE singleton_id = 1"
    ).fetchone()
    return bool(row and row["status"] == "closed")


def season_finalization_blockers(conn: sqlite3.Connection) -> list[str]:
    """점수·예치금이 아직 변할 수 있어 시즌 종료를 막아야 하는 상태를 돌려준다."""
    blockers: list[str] = []
    open_events = int(
        conn.execute(
            "SELECT COUNT(*) FROM event_sessions WHERE status != 'closed'"
        ).fetchone()[0]
    )
    open_rounds = int(
        conn.execute("SELECT COUNT(*) FROM rounds WHERE status = 'open'").fetchone()[0]
    )
    active_roulette = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM russian_roulette_games
            WHERE status IN ('pending', 'betting', 'playing')
            """
        ).fetchone()[0]
    )
    if open_events:
        blockers.append(f"진행 중인 이벤트 {open_events}개")
    if open_rounds:
        blockers.append(f"열린 라운드 {open_rounds}개")
    if active_roulette:
        blockers.append(f"진행 중인 러시안룰렛 {active_roulette}개")
    return blockers


def fetch_ranking_stat_rows(
    conn: sqlite3.Connection, current_bot_id: str
) -> list[sqlite3.Row]:
    """정상 종료된 메인·미니게임별 참가 횟수와 양수 정산 승수를 계산한다.

    현재 이벤트 모델에서는 event_participants가 한 이벤트의 모든 라운드 참가 명단이다.
    event_session_id가 없던 과거 라운드는 votes/trust_registrations/baseball_swings와
    score_logs로 보완한다. 강제 종료된 개발용 라운드는 참가·승리 양쪽에서 제외한다.
    """
    return conn.execute(
        """
        WITH eligible_round_participants(round_id, user_id) AS (
            SELECT rounds.id, event_participants.user_id
            FROM rounds
            JOIN event_participants
              ON event_participants.event_session_id = rounds.event_session_id
            WHERE rounds.status = 'closed'
              AND CASE
                    WHEN json_valid(rounds.extra_json)
                    THEN COALESCE(json_extract(rounds.extra_json, '$.force_closed'), 0)
                    ELSE 0
                  END = 0
            UNION
            SELECT rounds.id, votes.user_id
            FROM rounds JOIN votes ON votes.round_id = rounds.id
            WHERE rounds.status = 'closed'
              AND CASE
                    WHEN json_valid(rounds.extra_json)
                    THEN COALESCE(json_extract(rounds.extra_json, '$.force_closed'), 0)
                    ELSE 0
                  END = 0
            UNION
            SELECT rounds.id, trust_registrations.user_id
            FROM rounds
            JOIN trust_registrations ON trust_registrations.round_id = rounds.id
            WHERE rounds.status = 'closed'
              AND CASE
                    WHEN json_valid(rounds.extra_json)
                    THEN COALESCE(json_extract(rounds.extra_json, '$.force_closed'), 0)
                    ELSE 0
                  END = 0
            UNION
            SELECT rounds.id, baseball_swings.user_id
            FROM rounds
            JOIN baseball_swings ON baseball_swings.round_id = rounds.id
            WHERE rounds.status = 'closed'
              AND CASE
                    WHEN json_valid(rounds.extra_json)
                    THEN COALESCE(json_extract(rounds.extra_json, '$.force_closed'), 0)
                    ELSE 0
                  END = 0
            UNION
            SELECT rounds.id, score_logs.user_id
            FROM rounds JOIN score_logs ON score_logs.round_id = rounds.id
            WHERE rounds.status = 'closed'
              AND CASE
                    WHEN json_valid(rounds.extra_json)
                    THEN COALESCE(json_extract(rounds.extra_json, '$.force_closed'), 0)
                    ELSE 0
                  END = 0
        ),
        -- DB의 round_id 하나는 메인게임 또는 미니게임 한 번을 가리킨다.
        -- 서로 다른 게임의 점수를 더하지 않는다. 게임당 최종 점수 로그가 정확히
        -- 한 건이고 그 값이 양수일 때만 해당 게임을 승리로 표시한다.
        positive_game_results AS (
            SELECT scored.round_id, scored.user_id
            FROM score_logs AS scored
            WHERE scored.round_id IS NOT NULL
              AND scored.delta > 0
              AND NOT EXISTS (
                  SELECT 1
                  FROM score_logs AS duplicate
                  WHERE duplicate.round_id = scored.round_id
                    AND duplicate.user_id = scored.user_id
                    AND duplicate.id != scored.id
              )
        )
        SELECT
            users.user_id,
            users.display_name,
            users.total_score,
            COUNT(*) AS participation_count,
            SUM(CASE WHEN positive_game_results.user_id IS NOT NULL THEN 1 ELSE 0 END) AS win_count
        FROM eligible_round_participants
        JOIN users ON users.user_id = eligible_round_participants.user_id
        LEFT JOIN positive_game_results
          ON positive_game_results.round_id = eligible_round_participants.round_id
         AND positive_game_results.user_id = eligible_round_participants.user_id
        WHERE users.user_id != ?
        GROUP BY users.user_id, users.display_name, users.total_score
        ORDER BY
            users.total_score DESC,
            participation_count DESC,
            win_count DESC,
            users.user_id ASC
        """,
        (current_bot_id,),
    ).fetchall()


def fetch_final_ranking_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            user_id,
            display_name,
            total_score,
            participation_count,
            win_count,
            final_rank,
            draw_order
        FROM final_rankings
        ORDER BY final_rank ASC
        """
    ).fetchall()


def fetch_ranking_rows(conn: sqlite3.Connection, current_bot_id: str) -> list[sqlite3.Row]:
    """진행 중에는 계산 랭킹, 종료 후에는 영속된 고유 최종 랭킹을 반환한다."""
    if season_is_closed(conn):
        return fetch_final_ranking_rows(conn)
    return fetch_ranking_stat_rows(conn, current_bot_id)


def finalize_season_rankings(
    conn: sqlite3.Connection,
    current_bot_id: str,
    closed_by_user_id: str,
    *,
    rng: Any | None = None,
) -> list[sqlite3.Row]:
    """점수→참여→승리 순으로 정렬하고 완전 동률만 추첨해 한 번만 확정한다."""
    if season_is_closed(conn):
        raise SeasonAlreadyClosedError("이미 시즌이 종료되어 최종 순위가 확정되었습니다.")

    blockers = season_finalization_blockers(conn)
    if blockers:
        raise SeasonFinalizationBlockedError(blockers)

    candidates = fetch_ranking_stat_rows(conn, current_bot_id)
    if not candidates:
        raise SeasonFinalizationBlockedError(["최종 순위를 계산할 게임 참가자가 없음"])

    random_source = rng if rng is not None else secrets.SystemRandom()
    ordered: list[tuple[sqlite3.Row, int]] = []
    index = 0
    while index < len(candidates):
        first = candidates[index]
        key = (
            int(first["total_score"]),
            int(first["participation_count"]),
            int(first["win_count"]),
        )
        end = index + 1
        while end < len(candidates):
            candidate = candidates[end]
            candidate_key = (
                int(candidate["total_score"]),
                int(candidate["participation_count"]),
                int(candidate["win_count"]),
            )
            if candidate_key != key:
                break
            end += 1

        group = list(candidates[index:end])
        if len(group) > 1:
            random_source.shuffle(group)
        ordered.extend((row, draw_order) for draw_order, row in enumerate(group, start=1))
        index = end

    finalized_at = now_text()
    conn.execute("SAVEPOINT finalize_season")
    try:
        conn.execute("DELETE FROM final_rankings")
        for final_rank, (row, draw_order) in enumerate(ordered, start=1):
            conn.execute(
                """
                INSERT INTO final_rankings (
                    user_id, final_rank, display_name, total_score,
                    participation_count, win_count, draw_order, finalized_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(row["user_id"]),
                    final_rank,
                    str(row["display_name"]),
                    int(row["total_score"]),
                    int(row["participation_count"]),
                    int(row["win_count"]),
                    draw_order,
                    finalized_at,
                ),
            )
        updated = conn.execute(
            """
            UPDATE season_state
            SET status = 'closed', closed_at = ?, closed_by_user_id = ?
            WHERE singleton_id = 1 AND status = 'open'
            """,
            (finalized_at, closed_by_user_id),
        )
        if updated.rowcount != 1:
            raise SeasonAlreadyClosedError("이미 시즌이 종료되어 최종 순위가 확정되었습니다.")
        log_activity(
            conn,
            "season",
            "close",
            actor_user_id=closed_by_user_id,
            payload={
                "participant_count": len(ordered),
                "criteria": ["total_score", "participation_count", "win_count", "random_draw"],
            },
        )
        conn.execute("RELEASE SAVEPOINT finalize_season")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT finalize_season")
        conn.execute("RELEASE SAVEPOINT finalize_season")
        raise

    return fetch_final_ranking_rows(conn)


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


async def show_afk_coins_command(interaction: discord.Interaction) -> None:
    with db() as conn:
        ensure_user(conn, interaction.user)
        coins = get_afk_coins(conn, str(interaction.user.id))

    await interaction.response.send_message(
        f"현재 잠수 코인 **{coins}개**입니다.\n"
        f"음성채널에 있으면 1분마다 {AFK_COIN_PER_TICK}개씩 쌓이고, "
        f"`/지목` 1회에 {SCORE_REVEAL_COST}개를 씁니다.",
        ephemeral=True,
    )


async def reveal_score_command(interaction: discord.Interaction, 사용자: discord.Member) -> None:
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


async def show_score_command(interaction: discord.Interaction) -> None:
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


async def show_ranking_command(interaction: discord.Interaction, 등수: int | None) -> None:
    await send_ranking(interaction, 등수, reveal_users=True)


async def show_anonymous_ranking_command(interaction: discord.Interaction, 등수: int | None) -> None:
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


async def backup_data_command(interaction: discord.Interaction) -> None:
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
