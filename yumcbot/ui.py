"""임베드·메시지에 값을 얹기 위한 표시 유틸. 전부 순수 함수다.

디스코드 임베드는 필드 값이 비면 렌더링이 깨지고 길이 상한(1024자)을 넘으면 전송이
통째로 실패한다. 그래서 목록을 넣을 때는 `field_text`로 빈 값과 상한을 함께 처리한다.
"""

from __future__ import annotations


import discord

from .config import (
    CUTTING_LEVEL_CHOICES,
    DEFUSE_COLOR_BY_ID,
    TOP3_NONE_CHOICE,
    UNIQUE_NUMBER_CHOICES,
)


def mention_list(user_ids: list[str], limit: int = 40, sep: str = ", ") -> str:
    if not user_ids:
        return "없음"
    mentions = [f"<@{user_id}>" for user_id in user_ids[:limit]]
    suffix = f"\n외 {len(user_ids) - limit}명" if len(user_ids) > limit else ""
    return sep.join(mentions) + suffix


def field_text(lines: list[str], empty: str = "없음", limit: int = 1024) -> str:
    text = "\n".join(lines) if lines else empty
    return text if len(text) <= limit else text[: limit - 3] + "..."


def add_field_divider(embed: discord.Embed) -> None:
    """임베드 필드 사이에 가로 구분선을 넣어 섹션 경계를 분명히 한다.

    임베드는 필드 사이에 굵은 가로줄을 못 넣으므로, 이름이 빈(폭 0 공백) 전체폭 필드에
    선 문자를 채워 대신한다.
    """
    embed.add_field(name="​", value="━" * 15, inline=False)


def close_time_text(close_at: float | None) -> str:
    if not close_at:
        return "수동 마감"
    timestamp = int(float(close_at))
    return f"<t:{timestamp}:F>"


def choice_display(choice: str, game_key: str | None = None) -> str:
    if game_key == "unique_number" and choice in UNIQUE_NUMBER_CHOICES:
        return f"{choice}번"
    if choice in DEFUSE_COLOR_BY_ID:
        color = DEFUSE_COLOR_BY_ID[choice]
        return f"{color['emoji']} {color['name']}"
    if choice == TOP3_NONE_CHOICE:
        return "선택하지 않는다"
    if choice in CUTTING_LEVEL_CHOICES:
        return f"{choice}강화"
    return choice


def member_name(guild: discord.Guild | None, user_id: str) -> str | None:
    """멤버 캐시에서 표시 이름을 읽는다. 미스면 None.

    반환값은 add_score()의 display_name 인자로만 흘러가며, None이면 upsert가
    기존 이름을 보존한다. 예전엔 미스 시 'User <id>' 플레이스홀더를 반환해 그게
    DB에 그대로 덮여쓰였다 (issue #19). 결과는 embed 멘션으로 표시되므로 표시용
    플레이스홀더는 따로 필요 없다."""
    if guild:
        member = guild.get_member(int(user_id))
        if member:
            return member.display_name
    return None
