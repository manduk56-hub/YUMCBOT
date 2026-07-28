"""메인게임 셋(딜레마의 섬·모래성·가라앉는 배)이 공유하는 1:1 짝 로직.

세 게임 모두 짝을 무작위로 지어 **짝마다 비공개 스레드**를 열고 그 안에 버튼을 둔다.
공유되는 것은 매칭과 방 만들기까지이고, 방 안에서 무엇을 하는지는 각 게임 모듈이 갖는다.

- **방에 초대하는 것은 짝 두 사람뿐이다. 운영자를 참관용으로 넣지 말 것.**
  운영자도 게임에 참여할 수 있어서 모든 방에 넣으면 1:1이 성립하지 않는다.
  운영자는 `스레드 관리` 권한으로 필요한 방만 골라 본다(서버 설정 — 코드가 아니다).
- **짝 이력(`recent_match_pairs`)은 `rounds.type`으로 조회하므로 모래성과 가라앉는 배가
  이력을 공유한다.** 의도된 동작이다 — 같은 사람과 연속으로 만나는 것을 두 게임에
  걸쳐 회피한다.
- 스레드를 만들 때 `auto_archive_duration=1440`을 명시한다. 보관된 스레드에서는
  `스레드 관리` 권한이 없는 유저의 버튼 클릭을 디스코드가 막기 때문이다.

`build_random_trust_pairs`는 이름만 trust일 뿐 셋 다 쓴다(딜레마의 섬에서 먼저 만들어졌다).
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
from typing import Any, Protocol, runtime_checkable

import discord

from .. import runtime
from ..config import (
    MATCH_HISTORY_ROUNDS,
)
from ..db import (
    db,
)
from .registry import GAME_NAMES


@runtime_checkable
class PairBoardClient(Protocol):
    """짝 진행판을 다루는 데 필요한 클라이언트 기능. 실체는 EventBot이다.

    구체 타입 대신 프로토콜로 확인하는 이유는 순환 import를 피하기 위해서다
    (bot.py가 게임 모듈을 부른다). 프로토콜은 메서드 **이름**만 보므로 EventBot 쪽에서
    이름이 바뀌면 `isinstance`가 조용히 False가 된다 — `tests/test_packaging.py`가
    양쪽을 붙잡아 둔다.
    """

    def schedule_auto_close(self, round_id: int, close_at: float) -> None: ...

    def cancel_auto_close(self, round_id: int) -> None: ...

    def sand_board_lock(self, thread_id: int) -> asyncio.Lock: ...

    def release_board_lock(self, thread_id: int) -> None: ...


def board_client() -> PairBoardClient:
    client = runtime.get_client()
    if not isinstance(client, PairBoardClient):
        raise RuntimeError("짝 진행판을 다룰 수 없는 클라이언트입니다.")
    return client


def recent_match_pairs(
    conn: sqlite3.Connection,
    round_id: int,
    round_type: str,
    history_rounds: int = MATCH_HISTORY_ROUNDS,
) -> set[tuple[str, str]]:
    """직전 `history_rounds`개 같은 타입 라운드에서 성사된 1:1 짝을 정규화해 모은다.

    trust·sand 모두 `trust_pairings`에 짝을 남기므로 `round_type`으로 갈라 조회한다.
    """
    history_rows = conn.execute(
        """
        SELECT id FROM rounds
        WHERE type = ? AND id < ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (round_type, round_id, history_rounds),
    ).fetchall()
    history_ids = [int(row["id"]) for row in history_rows]
    if not history_ids:
        return set()
    placeholders = ", ".join("?" for _ in history_ids)
    history_pairs = conn.execute(
        f"SELECT user_a_id, user_b_id FROM trust_pairings WHERE round_id IN ({placeholders})",
        tuple(history_ids),
    ).fetchall()
    return {
        tuple(sorted((str(row["user_a_id"]), str(row["user_b_id"]))))
        for row in history_pairs
    }


def build_random_trust_pairs(
    user_ids: list[str],
    recent_pairs: set[tuple[str, str]],
) -> tuple[list[tuple[str, str]], str | None]:
    participants = list(user_ids)
    unmatched_user_id = None
    if len(participants) % 2:
        unmatched_user_id = random.choice(participants)
        participants.remove(unmatched_user_id)

    best_pairs: list[tuple[str, str]] = []
    best_penalty: int | None = None
    for _ in range(200):
        shuffled = random.sample(participants, len(participants))
        pairs = [(shuffled[index], shuffled[index + 1]) for index in range(0, len(shuffled), 2)]
        penalty = sum(tuple(sorted(pair)) in recent_pairs for pair in pairs)
        if best_penalty is None or penalty < best_penalty:
            best_pairs = pairs
            best_penalty = penalty
        if penalty == 0:
            break
    return best_pairs, unmatched_user_id


async def create_pair_thread(
    channel: discord.TextChannel,
    thread_name: str,
    member_ids: list[str],
    reason: str,
    view: discord.ui.View,
    content: str | None = None,
    embed: discord.Embed | None = None,
) -> tuple[discord.Thread, discord.Message] | None:
    """짝 하나를 위한 비공개 스레드를 열고 첫 메시지를 올린다. 실패하면 None.

    딜레마의 섬 · 모래성 · 긴급투입이 모두 이 헬퍼를 쓴다.
    보관되면 `스레드 관리` 권한이 없는 유저의 버튼 클릭이 막히므로 보관 시간을 넉넉히 잡는다.

    초대는 짝 두 사람뿐이다. 운영자도 게임에 참여할 수 있으므로 참관용으로 방에 넣지 않는다.
    운영자는 `스레드 관리` 권한으로 스레드 목록에서 필요한 방만 골라 보면 된다.
    """
    try:
        thread = await channel.create_thread(
            name=thread_name[:100],
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=1440,
            reason=reason,
        )
    except (discord.Forbidden, discord.HTTPException):
        return None

    for user_id in member_ids:
        try:
            await thread.add_user(discord.Object(id=int(user_id)))
        except (discord.HTTPException, ValueError):
            pass

    try:
        message = await thread.send(content=content, embed=embed, view=view)
    except discord.HTTPException:
        return None
    return thread, message


def save_pair_thread_id(round_id: int, user_a_id: str, user_b_id: str, thread_id: str) -> None:
    with db() as conn:
        conn.execute(
            """
            UPDATE trust_pairings
            SET discussion_thread_id = ?
            WHERE round_id = ? AND user_a_id = ? AND user_b_id = ?
            """,
            (thread_id, round_id, user_a_id, user_b_id),
        )


async def warn_failed_pair_threads(channel: discord.TextChannel, round_id: int, failed: int) -> None:
    if not failed:
        return
    try:
        await channel.send(
            f"라운드 #{round_id}의 비공개 방 {failed}개를 만들지 못했습니다. "
            "봇의 비공개 스레드 생성·관리 권한을 확인해주세요."
        )
    except discord.HTTPException:
        pass


async def ensure_pair_thread_permissions(
    interaction: discord.Interaction,
    게임: str,
    channel: discord.TextChannel,
) -> bool:
    """딜레마의 섬·모래성은 짝마다 비공개 스레드를 연다. 시작 전에 권한을 확인한다."""
    game_name = GAME_NAMES[게임]
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("서버에서 봇 권한을 확인하지 못했습니다.", ephemeral=True)
        return False

    bot_member = guild.me
    if bot_member is None:
        await interaction.followup.send("서버에서 봇 권한을 확인하지 못했습니다.", ephemeral=True)
        return False

    permissions = channel.permissions_for(bot_member)
    if (
        permissions.create_private_threads
        and permissions.send_messages_in_threads
        and permissions.manage_threads
    ):
        return True

    await interaction.followup.send(
        f"{game_name}에는 봇의 `비공개 스레드 만들기`, `스레드에서 메시지 보내기`, "
        "`스레드 관리` 권한이 필요합니다.",
        ephemeral=True,
    )
    return False


def emergency_join_view(round_id: int) -> discord.ui.View:
    """참가자가 홀수라 짝이 없는 사람이 생겼을 때만 메인 메시지에 붙는다."""
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="긴급 투입하기",
            emoji="🚨",
            style=discord.ButtonStyle.danger,
            custom_id=f"eventbot:emergency:{round_id}",
        )
    )
    return view


def emergency_join_notice(emergency_user_id: str) -> str:
    return f"<@{emergency_user_id}>님이 긴급 투입되어 전체 진행 시간이 1분 연장되었습니다."


async def update_trust_main_message(transition: dict[str, Any]) -> None:
    """단계 전이 후 메인 채널의 안내 메시지를 갈아끼운다.

    이름만 trust일 뿐 메인게임 셋이 모두 쓴다(딜레마의 섬에서 먼저 만들어졌다).
    메시지가 없으면 새로 보낸다 — 재시작 등으로 원본을 잃은 경우다.
    """
    try:
        client = runtime.get_client()
        channel = client.get_channel(int(transition["channel_id"]))
        if channel is None:
            channel = await client.fetch_channel(int(transition["channel_id"]))
        message_id = transition.get("message_id")
        if message_id:
            message = await channel.fetch_message(int(message_id))
            await message.edit(embed=transition["embed"], view=transition.get("view"))
        elif hasattr(channel, "send"):
            await channel.send(embed=transition["embed"], view=transition.get("view"))
    except discord.HTTPException:
        return
