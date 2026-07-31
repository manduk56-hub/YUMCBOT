"""점수 조회·랭킹 공개·관리자 백업 — `/잠수`·`/지목`·`/점수`·`/랭킹`·`/익명랭킹`·`/데이터백업`.

랭킹은 게임 기록이 있는 사람만, 봇은 제외하고 보여준다. 동점자는 `user_id` 순으로 고정한다 —
`updated_at`으로 정렬하면 `add_score()`가 그 값을 건드려 순서가 흔들리기 때문이다.
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime
from pathlib import Path

import discord

from . import config
from .config import (
    AFK_COIN_PER_TICK,
    SCORE_REVEAL_COST,
)
from .db import (
    db,
)
from .scoring import (
    ensure_user,
    get_afk_coins,
    spend_afk_coins,
)


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
