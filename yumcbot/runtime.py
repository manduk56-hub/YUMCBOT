"""디스코드 클라이언트를 게임 모듈에 건네주는 자리.

게임 모듈은 **인터랙션이 없는 자리에서도** 채널을 열어야 한다 — 자동 마감 타이머,
야구 투구 타이머처럼 사용자 클릭 없이 도는 콜백들이다. 그런데 클라이언트는 `EventBot`
인스턴스 하나뿐이고, 게임 모듈이 그걸 얻으려고 bot.py를 import 하면 곧바로 순환이 된다
(bot.py가 게임 모듈을 부른다). 그래서 부팅 시 여기에 등록해두고 모듈들이 꺼내 쓴다.

**`resolve_text_channel`은 `runtime.resolve_text_channel(...)`처럼 모듈을 거쳐 부른다.**
`from .runtime import resolve_text_channel`로 이름을 복사해 가면 테스트가
`patch.object(runtime, "resolve_text_channel")`로 갈아끼워도 그 사본은 바뀌지 않아,
가짜 채널을 넣었다고 믿는 테스트가 실제 클라이언트를 부르게 된다. 경로 상수를
`config.DB_PATH`로만 조회하는 것과 같은 이유다.
"""

from __future__ import annotations

import discord

_client: discord.Client | None = None


def set_client(client: discord.Client) -> None:
    """봇 인스턴스를 만든 직후 한 번 부른다."""
    global _client
    _client = client


def get_client() -> discord.Client:
    if _client is None:
        raise RuntimeError(
            "디스코드 클라이언트가 등록되지 않았습니다. "
            "봇 인스턴스를 만든 뒤 runtime.set_client()를 부르세요."
        )
    return _client


async def resolve_text_channel(channel_id: str) -> discord.TextChannel | None:
    client = get_client()
    channel = client.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await client.fetch_channel(int(channel_id))
        except (discord.HTTPException, ValueError):
            return None
    return channel if isinstance(channel, discord.TextChannel) else None
