"""게임 레지스트리 — 게임 하나의 정체성과 훅을 한 줄에 모은다.

`rounds.type` CHECK 제약이 불변이라(BASE_SCHEMA는 고칠 수 없다) 게임 이름과 저장 타입이
어긋난 채 `extra_json.mode`로 갈라 쓴다. 문제는 그 **쌍이 코드 두 곳에 나뉘어 있다**는
것이다 — 저장하는 곳(라운드 INSERT)과 되읽는 곳(정산 분기)이 멀리 떨어져 있어서,
한쪽만 고치면 라운드는 정상으로 열리고 진행도 되다가 정산에서 조용히 빠진다.
`GameSpec`은 그 쌍을 한 줄로 묶어 어긋남 자체를 불가능하게 만든다.

`GAMES`가 권위 있는 목록이고, `MAIN_GAME_TYPES` / `MINI_GAME_TYPES` / `GAME_NAMES`는
전부 여기서 파생된다. 게임을 추가할 때 고칠 곳은 이 파일 하나다.

**훅은 늦게 등록한다.** 이 모듈은 게임 구현을 import 하지 않는다 — 게임 코드는 임베드
빌더·DB 헬퍼·EventBot을 두루 쓰므로 여기서 끌어오면 곧바로 순환이 된다. 대신 각 게임이
자기 자신을 `register_settler()`로 올린다. 덕분에 옮긴 게임과 아직 bot.py에 남은 게임이
같은 디스패치 아래 공존할 수 있다 (패키지 분할이 끝날 때까지의 과도기 구조다).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

import discord

MAIN = "main"
MINI = "mini"


@dataclass(frozen=True)
class SettleContext:
    """정산 훅이 받는 것. `close_round_record`가 이미 열어둔 것들을 그대로 넘긴다.

    라운드 행은 이미 `status='closed'`로 갱신된 뒤이고, 트랜잭션은 아직 열려 있다.
    훅은 점수를 지급하고 결과 임베드를 만들기만 한다 — 커밋·활동 로그·다음 미니게임
    안내는 호출자가 모든 게임에 공통으로 처리한다.
    """

    conn: sqlite3.Connection
    round_row: sqlite3.Row
    extra: dict[str, Any]
    votes: list[sqlite3.Row]
    guild: discord.Guild | None


Settler = Callable[[SettleContext], discord.Embed]


@dataclass(frozen=True)
class GameSpec:
    key: str
    """코드상 게임 이름. `round_game_key()`가 돌려주는 값이자 이 레지스트리의 키."""

    name: str
    """참가자에게 보이는 이름."""

    category: str
    """`MAIN`(메인게임) 또는 `MINI`(미니게임)."""

    round_type: str
    """`rounds.type`에 저장하는 값. CHECK 제약 안의 것이어야 한다."""

    mode: str | None = None
    """`extra_json.mode`. None이면 `round_type`이 곧 키다."""

    def __post_init__(self) -> None:
        # round_game_key()는 `mode or type`이다. 그 결과가 이 spec을 다시 찾아내지
        # 못하면 라운드가 어느 게임인지 알 수 없게 되므로 정의 시점에 막는다.
        derived = self.mode or self.round_type
        if derived != self.key:
            raise ValueError(
                f"게임 '{self.key}': round_game_key()가 '{derived}'를 돌려주므로 "
                f"이 spec을 찾지 못한다. mode 또는 round_type을 키와 맞춰라."
            )
        if self.category not in {MAIN, MINI}:
            raise ValueError(f"게임 '{self.key}': 알 수 없는 분류 '{self.category}'")


_SPECS: tuple[GameSpec, ...] = (
    GameSpec("balance", "밸런스 게임", MINI, "balance"),
    # 참가자에게는 1차 집계가 끝날 때까지 일반 밸런스 게임으로 보여야 한다.
    GameSpec("minority_balance", "밸런스 게임", MINI, "balance", mode="minority_balance"),
    # DB 호환을 위해 내부 키는 defuse4를 유지하지만 현재 규칙은 3선 방식이다.
    GameSpec("defuse4", "몰리면 터진다!", MINI, "bomb", mode="defuse4"),
    GameSpec("cutting", "돌 세공 예측", MINI, "cutting"),
    GameSpec("top3", "상위 3명 견제", MINI, "top3"),
    # 'grid'(관리자 시점 타일)는 제거된 미니게임이다. 불변인 CHECK 제약의 grid 값을
    # 아래 두 게임의 저장 타입으로 재사용하고 mode로 갈라 쓴다.
    GameSpec("unique_number", "홍피겔만의 황금금고", MINI, "grid", mode="unique_number"),
    GameSpec("baseball", "야구", MINI, "grid", mode="baseball"),
    GameSpec("trust", "딜레마의 섬", MAIN, "trust"),
    GameSpec("sand", "모래성 게임", MAIN, "sand"),
    GameSpec("bail", "가라앉는 배", MAIN, "sand", mode="bail"),
)

GAMES: dict[str, GameSpec] = {spec.key: spec for spec in _SPECS}
if len(GAMES) != len(_SPECS):
    raise RuntimeError("게임 키가 중복됐습니다.")

# 아래 셋은 전부 게임 **키** 집합이다 (rounds.type 값 집합이 아니다).
# 'bail'은 type='sand'로 저장되므로 `round_row["type"] in MAIN_GAME_TYPES` 검사도
# 우연히 그대로 통과한다 — 호출부가 그 성질에 기대고 있어서 테스트로 고정해두었다.
# 게임을 갈라야 할 때는 round_game_key()를 쓴다.
MAIN_GAME_TYPES = {key for key, spec in GAMES.items() if spec.category == MAIN}
MINI_GAME_TYPES = {key for key, spec in GAMES.items() if spec.category == MINI}
GAME_NAMES = {key: spec.name for key, spec in GAMES.items()}

_settlers: dict[str, Settler] = {}


def register_settler(game_key: str, settler: Settler) -> None:
    """정산 훅을 올린다. 모듈이 import 될 때 한 번 부른다.

    등록하지 않은 게임은 `close_round_record`의 기존 분기로 떨어진다.
    """
    if game_key not in GAMES:
        raise KeyError(f"레지스트리에 없는 게임: {game_key}")
    _settlers[game_key] = settler


def settler_for(game_key: str) -> Settler | None:
    return _settlers.get(game_key)


def spec_for(game_key: str) -> GameSpec | None:
    return GAMES.get(game_key)
