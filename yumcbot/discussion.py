"""익명 토론 — 라운드 토론 스레드에서 번호로만 발언하는 기능.

**스레드를 잠그지 않는다.** `archived`/`locked` 상태에서는 `스레드 관리` 권한이 없는
유저의 버튼 클릭을 디스코드가 막는다. 익명 토론방을 잠갔더니 "관리자만 익명 발언이
된다"는 버그가 났었다. 스레드는 채널 권한을 상속해 개별 덮어쓰기가 불가능하므로
일반 채팅 차단은 `on_message`의 메시지 삭제로만 한다. 잠그는 것은 라운드가 끝나
버튼이 필요 없어진 뒤뿐이다(`archive_round_discussion_thread`).

`thread_discussion_view`가 선택 버튼을 스레드 안에도 따로 보내는 이유는, 토론 스레드가
라운드 메시지에서 파생돼 그 메시지가 시작 메시지가 되는데 시작 메시지의 버튼은
스레드 안에서 누를 수 없기 때문이다(실체가 부모 채널에 있어 회색으로 뜬다).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any, Protocol, runtime_checkable

import discord

from . import runtime
from .config import (
    ANONYMOUS_EMBED_COLORS,
    ANONYMOUS_GUIDE_TEXT,
    BALANCE_VOTE_CHOICES,
)
from .db import (
    db,
    now_text,
)
from .activity import (
    log_activity,
)
from .scoring import (
    ensure_user,
)
from .rounds import (
    can_join_round,
    round_extra,
    round_game_key,
)
from .ui import (
    close_time_text,
    vote_view,
)
from .ui import vote_view
from .games.defuse import defuse_vote_view
from .games.unique_number import unique_number_vote_view


@runtime_checkable
class AnonBoardClient(Protocol):
    """익명 토론방 버튼판 재게시를 직렬화하는 락을 가진 클라이언트. 실체는 EventBot이다."""

    def anon_board_lock(self, thread_id: int) -> asyncio.Lock: ...


def anon_board_client() -> AnonBoardClient:
    client = runtime.get_client()
    if not isinstance(client, AnonBoardClient):
        raise RuntimeError("익명 토론방 락을 다룰 수 없는 클라이언트입니다.")
    return client


def assign_anonymous_number(conn: sqlite3.Connection, round_id: int, user_id: str) -> int:
    """라운드 안에서 유저에게 고정으로 붙는 익명 번호. 첫 발언 때 1부터 순서대로 발급한다."""
    existing = conn.execute(
        "SELECT anon_number FROM discussion_messages WHERE round_id = ? AND user_id = ? LIMIT 1",
        (round_id, user_id),
    ).fetchone()
    if existing:
        return int(existing["anon_number"])

    row = conn.execute(
        "SELECT COALESCE(MAX(anon_number), 0) + 1 AS next FROM discussion_messages WHERE round_id = ?",
        (round_id,),
    ).fetchone()
    return int(row["next"])


def build_anonymous_message_embed(anon_number: int, content: str) -> discord.Embed:
    """작성자 이름 없이 내부 익명 번호마다 고정 색상만 붙이는 토론 발언 카드."""
    color = ANONYMOUS_EMBED_COLORS[(anon_number - 1) % len(ANONYMOUS_EMBED_COLORS)]
    return discord.Embed(
        description=content,
        color=color,
    )


def anonymous_discussion_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="익명 발언하기",
            emoji="🕶️",
            style=discord.ButtonStyle.secondary,
            custom_id=f"eventbot:anonymous:{round_id}",
        )
    )
    return view


def count_discussion_messages(conn: sqlite3.Connection, round_id: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count FROM discussion_messages
        WHERE round_id = ? AND message_id != 'pending'
        """,
        (round_id,),
    ).fetchone()
    return int(row["count"]) if row else 0


def build_anonymous_guide_embed(
    post_count: int,
    close_at: float | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title="🕶️ 익명 토론방",
        description=ANONYMOUS_GUIDE_TEXT,
        color=discord.Color.dark_grey(),
    )
    embed.add_field(
        name="🗳️ 투표",
        value="아래 선택 버튼으로 이 방에서 바로 투표할 수 있습니다. 마감 전까지 몇 번이든 바꿀 수 있습니다.",
        inline=False,
    )
    embed.add_field(name="💬 누적 발언", value=f"**{post_count}개**", inline=False)
    if close_at:
        embed.add_field(
            name="⏳ 마감 시각",
            value=f"**{close_time_text(close_at)}**",
            inline=False,
        )
    return embed


def thread_discussion_view(round_id: int, game_key: str) -> discord.ui.View:
    """토론방 맨 아래에 붙는 버튼 묶음: 선택지 + 익명 발언하기.

    토론 스레드는 라운드 메시지에서 파생시키므로 그 메시지가 스레드의 시작 메시지가 되는데,
    시작 메시지의 버튼은 스레드 안에서 누를 수 없다(실체가 부모 채널에 있는 메시지라 회색으로 뜬다).
    그래서 같은 custom_id의 선택 버튼을 스레드 안에도 둔다. votes는 (round_id, user_id) upsert라
    채널에서 누르든 스레드에서 누르든 마지막 선택 하나만 남는다.
    """
    view = discord.ui.View(timeout=None)
    if game_key in {"balance", "minority_balance"}:
        vote_buttons = vote_view(round_id, BALANCE_VOTE_CHOICES)
    elif game_key == "unique_number":
        vote_buttons = unique_number_vote_view(round_id)
    else:
        vote_buttons = defuse_vote_view(round_id)
    for button in [*vote_buttons.children, *anonymous_discussion_view(round_id).children]:
        view.add_item(button)
    return view


async def archive_round_discussion_thread(
    round_row: sqlite3.Row,
    *,
    reason: str,
) -> None:
    """일반 토론 스레드를 삭제하지 않고 읽기 전용 보관 상태로 남긴다."""
    thread_id = str(round_row["discussion_thread_id"] or "")
    if not thread_id:
        return
    try:
        thread = await runtime.resolve_thread(thread_id)
        if thread is not None:
            await thread.edit(archived=True, locked=True, reason=reason)
    except (discord.HTTPException, ValueError):
        pass


class AnonymousDiscussionModal(discord.ui.Modal):
    def __init__(self, round_id: int) -> None:
        super().__init__(title="익명 발언하기")
        self.round_id = round_id
        self.content_input = discord.ui.TextInput(
            label="발언 내용",
            placeholder="다른 참가자에게는 작성자가 표시되지 않습니다.",
            style=discord.TextStyle.paragraph,
            max_length=1000,
        )
        self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # 발언 게시 -> 옛 버튼 삭제 -> 버튼 재게시까지 API 호출이 여러 번이라 모달의 3초 응답
        # 제한을 넘긴다. 먼저 defer하고, 성공하면 그 ephemeral을 지워서 흔적을 남기지 않는다.
        await interaction.response.defer(ephemeral=True)

        if not isinstance(interaction.channel, discord.Thread):
            await interaction.followup.send("토론 스레드에서만 발언할 수 있습니다.", ephemeral=True)
            return

        content = self.content_input.value.strip()
        if not content:
            await interaction.followup.send("발언 내용을 입력해주세요.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        with db() as conn:
            round_row = conn.execute(
                """
                SELECT * FROM rounds
                WHERE id = ? AND status = 'open' AND discussion_thread_id = ?
                """,
                (self.round_id, str(interaction.channel.id)),
            ).fetchone()
            if not round_row:
                await interaction.followup.send("이미 마감되었거나 유효하지 않은 토론방입니다.", ephemeral=True)
                return
            if not can_join_round(conn, self.round_id, user_id):
                await interaction.followup.send(
                    "이번 이벤트에서 `참여하기`를 누른 유저만 토론에 참여할 수 있습니다.",
                    ephemeral=True,
                )
                return

            ensure_user(conn, interaction.user)
            anon_number = assign_anonymous_number(conn, self.round_id, user_id)
            # 게시 전에 행을 잡아둔다. 더블클릭해도 번호가 두 개로 갈리지 않는다.
            cursor = conn.execute(
                """
                INSERT INTO discussion_messages
                    (round_id, user_id, anon_number, message_id, content, created_at)
                VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (self.round_id, user_id, anon_number, content, now_text()),
            )
            post_id = int(cursor.lastrowid)
            game_key = round_game_key(round_row)

        try:
            posted_message = await interaction.channel.send(
                embed=build_anonymous_message_embed(anon_number, content),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            with db() as conn:
                conn.execute("DELETE FROM discussion_messages WHERE id = ?", (post_id,))
            await interaction.followup.send("익명 발언을 올리지 못했습니다. 다시 시도해주세요.", ephemeral=True)
            return

        with db() as conn:
            conn.execute(
                "UPDATE discussion_messages SET message_id = ? WHERE id = ?",
                (str(posted_message.id), post_id),
            )
            # 내용은 discussion_messages(append-only)에 이미 온전히 있다. 여기서는 중복 저장하지
            # 않고 참조만 남겨, 하나의 타임라인에서 발언 시점을 다른 활동과 함께 볼 수 있게 한다.
            log_activity(
                conn,
                "thread",
                "anon_post",
                event_session_id=round_row["event_session_id"],
                round_id=self.round_id,
                actor_user_id=user_id,
                payload={
                    "discussion_message_id": post_id,
                    "anon_number": anon_number,
                    "content_length": len(content),
                    "thread_id": str(interaction.channel.id),
                },
            )

        # 버튼이 발언에 밀려 위로 올라가지 않도록, 옛 버튼을 지우고 맨 아래에 다시 붙인다.
        # 여러 명이 동시에 발언하면 각자 옛 스냅샷에서 버튼 id를 읽어 버튼판이 여러 개로 갈린다.
        # 그래서 스레드별 락을 잡고, 락 안에서 현재 버튼 id를 DB에서 다시 읽는다. (모래성과 같은 방식)
        async with anon_board_client().anon_board_lock(interaction.channel.id):
            with db() as conn:
                fresh_row = conn.execute(
                    "SELECT extra_json FROM rounds WHERE id = ?", (self.round_id,)
                ).fetchone()
                fresh_extra = round_extra(fresh_row) if fresh_row else {}
                current_button_id = str(fresh_extra.get("anon_button_message_id") or "")
                current_auto_close_at = fresh_extra.get("auto_close_at")
                post_count = count_discussion_messages(conn, self.round_id)

            if current_button_id:
                try:
                    old_button = await interaction.channel.fetch_message(int(current_button_id))
                    await old_button.delete()
                except (discord.NotFound, discord.HTTPException, ValueError):
                    pass
            try:
                new_button = await interaction.channel.send(
                    embed=build_anonymous_guide_embed(post_count, current_auto_close_at),
                    view=thread_discussion_view(self.round_id, game_key),
                )
            except discord.HTTPException:
                # 버튼 재게시에 실패해도 발언 자체는 이미 올라갔다. 발언을 되돌리지는 않는다.
                new_button = None

            if new_button:
                with db() as conn:
                    fresh_row = conn.execute(
                        "SELECT extra_json FROM rounds WHERE id = ?", (self.round_id,)
                    ).fetchone()
                    if fresh_row:
                        extra = round_extra(fresh_row)
                        extra["anon_button_message_id"] = str(new_button.id)
                        conn.execute(
                            "UPDATE rounds SET extra_json = ? WHERE id = ?",
                            (json.dumps(extra, ensure_ascii=False), self.round_id),
                        )

        # 발언자를 드러내지 않도록 확인 문구를 남기지 않고 defer한 ephemeral만 조용히 지운다.
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass
