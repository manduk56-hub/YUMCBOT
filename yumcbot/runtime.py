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

from typing import Any, Protocol, runtime_checkable

import discord

_client: discord.Client | None = None


@runtime_checkable
class RoundServices(Protocol):
    """게임 모듈이 라운드를 끝낼 때 필요한 bot.py 쪽 기능들.

    게임이 스스로 라운드를 끝내는 경우가 있다 — 야구는 3구를 다 던지면, 모래성은
    모든 짝이 게임을 마치면 제한 시간과 무관하게 끝난다. 그때 정산과 결과 발행이
    필요한데, 정산은 **모든 게임의 분기를 아는** `close_round_record`가 해야 하므로
    게임 모듈이 직접 부를 수 없다(그쪽이 레지스트리를 거쳐 다시 게임 모듈을 부른다).

    묶어서 두는 이유는 셋이 늘 같이 쓰이면서도 **순서와 조건이 게임마다 다르기**
    때문이다. 야구는 정산 결과가 없어도 이벤트 요약을 시도하고, 모래성은 그 자리에서
    멈춘다. 한 번에 다 해주는 함수로 감싸면 그 차이를 표현할 수 없다.
    """

    def close_round(
        self, round_id: int, guild: Any, closed_by: str
    ) -> tuple[str, discord.Embed] | None: ...

    def next_mini_game_view(self, round_id: int) -> discord.ui.View | None: ...

    async def publish_event_summary(self, event_session_id: int | None) -> None: ...


@runtime_checkable
class AutoCloseScheduler(Protocol):
    """자동 마감 타이머를 다시 거는 데 필요한 최소 기능. 실체는 EventBot이다.

    1:1 방을 쓰지 않는 게임(예: 소수파 밸런스의 1차→재선택 전이)도 다음 마감을
    다시 걸어야 하는데, 그 게임엔 `games.pairing.PairBoardClient`가 요구하는
    스레드 락 기능이 없다. 그 두 게임에만 있는 것을 억지로 요구하지 않도록
    스케줄링만 따로 뗀다.
    """

    def schedule_auto_close(self, round_id: int, close_at: float) -> None: ...

    def cancel_auto_close(self, round_id: int) -> None: ...


def auto_close_scheduler() -> AutoCloseScheduler:
    client = get_client()
    if not isinstance(client, AutoCloseScheduler):
        raise RuntimeError("자동 마감 타이머를 걸 수 없는 클라이언트입니다.")
    return client


_round_services: RoundServices | None = None


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


def set_round_services(services: RoundServices) -> None:
    """라운드 마감 서비스를 등록한다. 부팅 시 한 번 부른다."""
    global _round_services
    _round_services = services


def round_services() -> RoundServices:
    if _round_services is None:
        raise RuntimeError(
            "라운드 마감 서비스가 등록되지 않았습니다. runtime.set_round_services()를 부르세요."
        )
    return _round_services


async def resolve_text_channel(channel_id: str) -> discord.TextChannel | None:
    client = get_client()
    channel = client.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await client.fetch_channel(int(channel_id))
        except (discord.HTTPException, ValueError):
            return None
    return channel if isinstance(channel, discord.TextChannel) else None


async def resolve_thread(thread_id: str) -> discord.Thread | None:
    """1:1 비공개 방을 연다. 캐시에 없으면 가져오고, 실패하면 None.

    메인게임 셋이 짝마다 스레드를 열기 때문에 같은 조회가 반복된다. 방 하나를
    못 찾는 것은 정상 동작에 속하므로(수동 삭제·권한 변경) 예외 대신 None을 준다.
    """
    client = get_client()
    try:
        thread = client.get_channel(int(thread_id)) or await client.fetch_channel(
            int(thread_id)
        )
    except (discord.HTTPException, ValueError):
        return None
    return thread if isinstance(thread, discord.Thread) else None
