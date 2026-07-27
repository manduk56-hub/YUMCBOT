"""러시안룰렛 — 이벤트/라운드 모델 밖에서 도는 상시 1:1 게임.

**점수가 유저 사이를 오가는 유일한 경로**라 순위 조작이 여기로만 들어온다.
아래 제약이 전부 그 방어선이라 하나씩 떼면 안 된다 (자세한 근거는 AGENTS.md).

- **양쪽 베팅액은 반드시 같다.** 낮은 쪽으로 통일하지 않으면 '무조건 최소액'이
  유일한 정답이 되어 베팅이라는 선택 자체가 사라진다. 같은 이유로 둘 다 넣기 전에는
  금액을 공개하지 않는다.
- **신용 유저**(보유 점수 < ROULETTE_MIN_BET)는 이겨도 점수가 오르지 않고 스택이 쌓인다.
  신용 여부는 베팅 시점에 `*_negative` 컬럼으로 박아둔다(예치가 점수를 깎아 나중에
  다시 계산할 수 없다). 시간 초과 승리는 스택에 세지 않는다.
- **차례 3분 제한, 넘기면 자동 패배.** 무승부 환불로 두면 잠수가 지배 전략이 된다.
  타이머 복구는 라운드 쪽과 별개다 — `restore_roulette_turn_tasks()`를 on_ready에
  반드시 등록해야 한다.
- **성립 실패 사유를 신청자에게 알리지 않는다.** 알려주면 금액을 바꿔가며 신청하는
  것만으로 상대 점수를 캐는 무료 조회 수단이 된다.

연승 스택 함수(get/set_roulette_stack)는 여기가 아니라 scoring에 있다 — add_score가
스택 무효화를 확인해야 해서, 여기 두면 순환이 된다.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import discord

from .config import (
    BOT_VERSION,
    ROULETTE_BETTING_SECONDS,
    ROULETTE_DAILY_LIMIT,
    ROULETTE_MAX_BET,
    ROULETTE_MIN_BET,
    ROULETTE_PAIR_DAILY_LIMIT,
    ROULETTE_STACK_GOAL,
    ROULETTE_STACK_REWARD,
    ROULETTE_TURN_SECONDS,
)
from .db import (
    db,
    now_text,
)
from .activity import (
    log_activity,
)
from .scoring import (
    add_score,
    get_roulette_stack,
    set_roulette_stack,
)


@runtime_checkable
class RouletteTimerClient(Protocol):
    """차례 타이머를 걸 수 있는 클라이언트. 실체는 EventBot이다.

    구체 타입 대신 프로토콜로 확인하는 이유는 순환 import를 피하기 위해서다.
    EventBot(runtime)은 이 모듈의 핸들러를 부르므로, 여기서 EventBot을 import 하면
    양방향이 된다. 필요한 것은 타입이 아니라 '타이머를 걸 수 있는가'뿐이다.
    """

    def schedule_roulette_turn(
        self, game_id: int, expires_at: float, shots_fired: int
    ) -> None: ...

    def cancel_roulette_turn(self, game_id: int) -> None: ...


def expire_stale_roulette_games(conn: sqlite3.Connection, now: float | None = None) -> None:
    """수락 또는 베팅을 오래 방치한 게임을 닫는다. 이 단계에는 아직 예치금이 없다."""
    current = time.time() if now is None else now
    stale = conn.execute(
        """
        SELECT * FROM russian_roulette_games
        WHERE status IN ('pending', 'betting') AND expires_at IS NOT NULL AND expires_at <= ?
        """,
        (current,),
    ).fetchall()
    if not stale:
        return

    for row in stale:
        # 어느 단계에서 만료됐는지, 누가 베팅을 안 넣었는지가 곧 횟수 차감 근거다.
        # status를 'expired'로 덮고 나면 pending 만료와 구분할 수 없게 된다.
        log_activity(
            conn,
            "roulette",
            "expire",
            actor_user_id=str(row["challenger_id"]),
            target_user_id=str(row["target_id"]),
            payload={
                "game_id": int(row["id"]),
                "phase": str(row["status"]),
                "challenger_bet": row["challenger_bet"],
                "target_bet": row["target_bet"],
            },
        )

    conn.execute(
        """
        UPDATE russian_roulette_games
        SET status = 'expired', finished_at = ?
        WHERE status IN ('pending', 'betting') AND expires_at IS NOT NULL AND expires_at <= ?
        """,
        (now_text(), current),
    )
def active_roulette_game_for_user(
    conn: sqlite3.Connection, user_id: str
) -> sqlite3.Row | None:
    """동시에 한 결투만. 단 pending은 신청자만 묶는다.

    지목당한 쪽까지 pending으로 묶으면, 아무에게나 신청을 걸어두고 방치하는 것만으로
    상대를 만료 시간 내내 룰렛에서 배제할 수 있다. 비용이 0이라 무한 반복이 가능하다.
    받는 쪽은 여러 건을 동시에 받아두고 그중 하나만 수락하게 둔다.
    """
    return conn.execute(
        """
        SELECT * FROM russian_roulette_games
        WHERE (
                (status = 'pending' AND challenger_id = ?)
                OR (
                    status IN ('betting', 'playing')
                    AND (challenger_id = ? OR target_id = ?)
                )
              )
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id, user_id, user_id),
    ).fetchone()
def is_roulette_credit_user(score: int) -> bool:
    """최소 베팅액조차 못 내는 유저. 정확히 0점인 유저도 포함이다.

    '마이너스'로만 잡으면 0점인 유저가 어느 규칙에도 속하지 못한 채 참여만 막힌다
    (-1점은 되는데 0점은 안 되는 상태가 된다).
    """
    return score < ROULETTE_MIN_BET
def roulette_day_key(now: datetime | None = None) -> str:
    """하루 경계는 KST 자정. created_at이 서버 로컬시간이라 앞 10자리가 곧 날짜다."""
    return (now or datetime.now()).strftime("%Y-%m-%d")
_ROULETTE_CONSUMED_SQL = """
    status IN ('playing', 'finished')
    OR (
        status = 'expired' AND accepted_at IS NOT NULL
        AND (
            (challenger_id = :uid AND challenger_bet IS NULL)
            OR (target_id = :uid AND target_bet IS NULL)
        )
    )
"""
def count_roulette_games_today(
    conn: sqlite3.Connection, user_id: str, day: str | None = None
) -> int:
    return int(
        conn.execute(
            f"""
            SELECT COUNT(*) FROM russian_roulette_games
            WHERE substr(created_at, 1, 10) = :day
              AND (challenger_id = :uid OR target_id = :uid)
              AND ({_ROULETTE_CONSUMED_SQL})
            """,
            {"day": day or roulette_day_key(), "uid": user_id},
        ).fetchone()[0]
    )
def count_roulette_games_with_opponent_today(
    conn: sqlite3.Connection, user_id: str, opponent_id: str, day: str | None = None
) -> int:
    """같은 상대와의 하루 횟수. 성립한 판만 센다.

    베팅 미입력 만료까지 세면, 신청해놓고 베팅을 안 넣는 것만으로 특정 상대와의
    '하루 한 판'을 태워버릴 수 있다. 성립한 판만 세면 그 수법이 통하지 않는다.
    """
    return int(
        conn.execute(
            """
            SELECT COUNT(*) FROM russian_roulette_games
            WHERE substr(created_at, 1, 10) = :day
              AND status IN ('playing', 'finished')
              AND (
                    (challenger_id = :uid AND target_id = :opp)
                    OR (challenger_id = :opp AND target_id = :uid)
                  )
            """,
            {"day": day or roulette_day_key(), "uid": user_id, "opp": opponent_id},
        ).fetchone()[0]
    )
def roulette_challenge_view(game_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="수락",
            style=discord.ButtonStyle.success,
            custom_id=f"eventbot:rouletteaccept:{game_id}",
            disabled=disabled,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="거절",
            style=discord.ButtonStyle.danger,
            custom_id=f"eventbot:roulettedecline:{game_id}",
            disabled=disabled,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="내 점수 확인",
            style=discord.ButtonStyle.secondary,
            custom_id=f"eventbot:roulettescore:{game_id}",
            disabled=disabled,
        )
    )
    return view
def roulette_betting_view(game_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="베팅 입력",
            style=discord.ButtonStyle.primary,
            custom_id=f"eventbot:roulettebet:{game_id}",
            disabled=disabled,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="내 점수 확인",
            style=discord.ButtonStyle.secondary,
            custom_id=f"eventbot:roulettescore:{game_id}",
            disabled=disabled,
        )
    )
    return view
def roulette_play_view(game_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="방아쇠 당기기",
            emoji="🔫",
            style=discord.ButtonStyle.danger,
            custom_id=f"eventbot:roulettetrigger:{game_id}",
            disabled=disabled,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="내 점수 확인",
            style=discord.ButtonStyle.secondary,
            custom_id=f"eventbot:roulettescore:{game_id}",
            disabled=disabled,
        )
    )
    return view
def roulette_finished_view(game_id: int) -> discord.ui.View:
    """끝난 판에 남기는 개인 정산 버튼.

    신용 유저는 이겨도 점수가 오르지 않는데 그 사실을 공개 결과에 쓰면 '저 사람은
    점수가 없다'가 채널 전체에 박제된다. 그렇다고 이긴 순간 본인에게 ephemeral을
    보낼 수도 없다 — 그 인터랙션의 주인은 방아쇠를 당긴 상대이고, 타임아웃이면
    인터랙션 자체가 없다. 그래서 본인이 눌러서 확인하는 형태로 둔다.

    모든 판에 예외 없이 붙기 때문에 버튼의 존재 자체는 아무것도 알려주지 않는다.
    """
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="내 정산 확인",
            style=discord.ButtonStyle.secondary,
            custom_id=f"eventbot:roulettesettle:{game_id}",
        )
    )
    return view
def roulette_view(game: sqlite3.Row | dict[str, Any]) -> discord.ui.View | None:
    status = str(game["status"])
    game_id = int(game["id"])
    if status == "pending":
        return roulette_challenge_view(game_id)
    if status == "betting":
        return roulette_betting_view(game_id)
    if status == "playing":
        return roulette_play_view(game_id)
    if status == "finished":
        return roulette_finished_view(game_id)
    return None
def build_roulette_embed(game: sqlite3.Row | dict[str, Any]) -> discord.Embed:
    challenger_id = str(game["challenger_id"])
    target_id = str(game["target_id"])
    status = str(game["status"])
    title = "🔫 러시안룰렛 1:1"

    if status == "pending":
        expires_at = int(float(game["expires_at"]))
        description = (
            f"<@{challenger_id}>님이 <@{target_id}>님에게 결투를 신청했습니다.\n\n"
            "지목받은 플레이어만 수락하거나 거절할 수 있습니다.\n"
            f"요청 만료: <t:{expires_at}:R>"
        )
        color = discord.Color.orange()
    elif status == "betting":
        # 입력한 금액은 둘 다 넣을 때까지 공개하지 않는다. 먼저 넣은 쪽 금액이 보이면
        # 나중에 넣는 쪽이 그걸 보고 정할 수 있어 후행 베팅자가 일방적으로 유리해진다.
        challenger_text = "입력 완료" if game["challenger_bet"] is not None else "입력 대기 중"
        target_text = "입력 완료" if game["target_bet"] is not None else "입력 대기 중"
        expires_at = int(float(game["expires_at"]))
        description = (
            f"두 플레이어가 각자 걸 점수를 입력합니다. "
            f"**{ROULETTE_MIN_BET}~{ROULETTE_MAX_BET}점** 사이이며, "
            "서로 금액이 다르면 **낮은 쪽으로 맞춰집니다**.\n"
            "입력한 금액은 둘 다 넣을 때까지 공개되지 않습니다.\n\n"
            f"<@{challenger_id}>: {challenger_text}\n"
            f"<@{target_id}>: {target_text}\n\n"
            f"베팅 입력 만료: <t:{expires_at}:R>"
        )
        color = discord.Color.blurple()
    elif status == "playing":
        shots_fired = int(game["shots_fired"])
        remaining = 6 - shots_fired
        current_player_id = str(game["current_player_id"])
        bet = int(game["challenger_bet"])
        pot = bet + int(game["target_bet"])
        description = (
            f"<@{challenger_id}> vs <@{target_id}> · 각 **{bet}점**\n"
            f"총 예치금: **{pot}점**\n\n"
            f"격발 횟수: **{shots_fired}/6**\n"
            f"남은 탄창: **{remaining}칸** (실탄 1발)\n\n"
            f"지금 차례: <@{current_player_id}>"
        )
        if game["expires_at"] is not None:
            turn_deadline = int(float(game["expires_at"]))
            description += f"\n차례 마감: <t:{turn_deadline}:R> — 넘기면 **자동 패배**"
        color = discord.Color.red()
    elif status == "finished":
        loser_id = str(game["loser_id"])
        stolen_points = (
            int(game["challenger_bet"])
            if loser_id == challenger_id
            else int(game["target_bet"])
        )
        if str(game["finish_cause"] or "") == "timeout":
            headline = f"⏱️ <@{loser_id}>님이 제한 시간 안에 방아쇠를 당기지 않았습니다."
        else:
            headline = f"💥 <@{loser_id}>님에게서 총이 격발되었습니다."
        # 신용 유저가 이겼을 때도 문구가 똑같아야 한다. 실제 점수 변동은 본인만
        # '내 정산 확인'으로 볼 수 있고, 공개 결과로는 구분되지 않는다.
        description = (
            f"{headline}\n\n"
            f"🏆 <@{game['winner_id']}>님이 <@{loser_id}>님의 "
            f"**{stolen_points}점**을 빼앗았습니다."
        )
        color = discord.Color.gold()
    elif status == "declined":
        description = f"<@{target_id}>님이 결투 요청을 거절했습니다."
        color = discord.Color.light_grey()
    else:
        description = "정해진 시간 안에 응답하지 않아 결투가 만료되었습니다."
        color = discord.Color.light_grey()

    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text=f"게임 #{game['id']} · 6칸 중 실탄 1발 · 발사 후 탄창을 다시 돌리지 않음")
    return embed
def submit_roulette_bet(
    conn: sqlite3.Connection,
    game_id: int,
    user_id: str,
    amount: int,
) -> tuple[str, dict[str, Any]]:
    """베팅을 저장하고 둘 다 준비됐으면 낮은 쪽 금액으로 맞춰 예치한 뒤 게임을 시작한다."""
    expire_stale_roulette_games(conn)
    row = conn.execute(
        "SELECT * FROM russian_roulette_games WHERE id = ?", (game_id,)
    ).fetchone()
    if not row:
        raise ValueError("존재하지 않는 게임입니다.")
    if row["status"] != "betting":
        raise ValueError("베팅을 입력할 수 있는 상태가 아닙니다.")
    if user_id not in {str(row["challenger_id"]), str(row["target_id"])}:
        raise ValueError("이 결투의 플레이어만 베팅할 수 있습니다.")

    balance_row = conn.execute(
        "SELECT total_score FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    balance = int(balance_row["total_score"]) if balance_row else 0
    credit = is_roulette_credit_user(balance)

    submitted = amount
    if credit:
        # 신용 유저는 입력값과 관계없이 최소액이 된다. 밖에서 보면 그냥 최소액을 건
        # 신중한 플레이어와 구분되지 않는다.
        amount = ROULETTE_MIN_BET
    else:
        if amount < ROULETTE_MIN_BET or amount > ROULETTE_MAX_BET:
            raise ValueError(
                f"베팅 점수는 {ROULETTE_MIN_BET}~{ROULETTE_MAX_BET}점 사이여야 합니다."
            )
        if amount > balance:
            raise ValueError(
                f"현재 점수는 {balance}점입니다. 가진 점수보다 많이 걸 수 없습니다."
            )

    column = "challenger_bet" if user_id == str(row["challenger_id"]) else "target_bet"
    flag_column = (
        "challenger_negative"
        if user_id == str(row["challenger_id"])
        else "target_negative"
    )
    conn.execute(
        f"UPDATE russian_roulette_games SET {column} = ?, {flag_column} = ? WHERE id = ?",
        (amount, int(credit), game_id),
    )
    # 컬럼에는 최종값만 남는다. 낮은 쪽으로 통일되면서, 또 무효화(balance_changed) 시
    # NULL로 지워지면서 원래 얼마를 걸었는지가 사라지므로 여기서만 복원할 수 있다.
    log_activity(
        conn,
        "roulette",
        "bet",
        actor_user_id=user_id,
        payload={
            "game_id": game_id,
            "submitted": submitted,
            "amount": amount,
            "balance": balance,
            "credit": credit,
        },
    )
    row = conn.execute(
        "SELECT * FROM russian_roulette_games WHERE id = ?", (game_id,)
    ).fetchone()
    if row["challenger_bet"] is None or row["target_bet"] is None:
        return "waiting", dict(row)

    player_ids = [str(row["challenger_id"]), str(row["target_id"])]
    balances = {
        str(item["user_id"]): int(item["total_score"])
        for item in conn.execute(
            "SELECT user_id, total_score FROM users WHERE user_id IN (?, ?)", player_ids
        ).fetchall()
    }
    submitted_bets = {
        player_ids[0]: int(row["challenger_bet"]),
        player_ids[1]: int(row["target_bet"]),
    }
    credit_flags = {
        player_ids[0]: bool(row["challenger_negative"]),
        player_ids[1]: bool(row["target_negative"]),
    }
    # 낮은 쪽으로 맞춘다. 서로 다른 금액을 걸 수 있게 두면 적게 건 쪽이 항상 이득이라
    # (적게 걸고 상대의 큰 금액을 노림) '무조건 최소액'이 유일한 정답이 돼버린다.
    stake = min(submitted_bets.values())

    # 신용 유저는 애초에 낼 점수가 없어 외상으로 건다. 잔액 검증에서 제외한다.
    # 이 판정은 '베팅을 넣던 시점에 신용 유저였는가'로 고정된다. 그 사이 점수가 올라도
    # 예치 조건과 정산 규칙을 도중에 갈아끼우지 않는다.
    invalid_ids = [
        uid
        for uid in player_ids
        if not credit_flags[uid] and balances.get(uid, 0) < stake
    ]
    if invalid_ids:
        for invalid_id in invalid_ids:
            invalid_column = "challenger_bet" if invalid_id == player_ids[0] else "target_bet"
            conn.execute(
                f"UPDATE russian_roulette_games SET {invalid_column} = NULL WHERE id = ?",
                (game_id,),
            )
        fresh = conn.execute(
            "SELECT * FROM russian_roulette_games WHERE id = ?", (game_id,)
        ).fetchone()
        return "balance_changed", dict(fresh)

    conn.execute(
        """
        UPDATE russian_roulette_games
        SET challenger_bet = ?, target_bet = ?
        WHERE id = ?
        """,
        (stake, stake, game_id),
    )
    add_score(
        conn,
        player_ids[0],
        str(row["challenger_name"]),
        -stake,
        f"러시안룰렛 #{game_id} 베팅 예치",
    )
    add_score(
        conn,
        player_ids[1],
        str(row["target_name"]),
        -stake,
        f"러시안룰렛 #{game_id} 베팅 예치",
    )
    current_player_id = player_ids[secrets.randbelow(2)]
    live_chamber = secrets.randbelow(6)
    turn_deadline = time.time() + ROULETTE_TURN_SECONDS
    conn.execute(
        """
        UPDATE russian_roulette_games
        SET status = 'playing', current_player_id = ?, live_chamber = ?,
            shots_fired = 0, expires_at = ?
        WHERE id = ? AND status = 'betting'
        """,
        (current_player_id, live_chamber, turn_deadline, game_id),
    )
    # live_chamber는 이미 게임 테이블에 평문으로 있다(운영자는 어차피 볼 수 있다).
    # 여기 남겨두면 나중에 '실탄 위치가 고르게 나왔는지' 분포를 검증할 수 있다.
    log_activity(
        conn,
        "roulette",
        "start",
        actor_user_id=player_ids[0],
        target_user_id=player_ids[1],
        payload={
            "game_id": game_id,
            "submitted_bets": submitted_bets,
            "stake": stake,
            "pot": stake * 2,
            "credit": credit_flags,
            "live_chamber": live_chamber,
            "first_player_id": current_player_id,
            # 정산 규칙 일부(신용 유저는 승리해도 예치금만 회수)는 상수가 아니라
            # settle_roulette_game에 하드코딩돼 있어 값으로 뜰 수 없다. bot_version으로
            # 그 커밋을 찾아 당시 규칙을 확인한다.
            "settings": {
                "bot_version": BOT_VERSION,
                "min_bet": ROULETTE_MIN_BET,
                "max_bet": ROULETTE_MAX_BET,
                "turn_seconds": ROULETTE_TURN_SECONDS,
                "daily_limit": ROULETTE_DAILY_LIMIT,
                "pair_daily_limit": ROULETTE_PAIR_DAILY_LIMIT,
                "stack_goal": ROULETTE_STACK_GOAL,
                "stack_reward": ROULETTE_STACK_REWARD,
            },
        },
    )
    fresh = conn.execute(
        "SELECT * FROM russian_roulette_games WHERE id = ?", (game_id,)
    ).fetchone()
    return "started", dict(fresh)
def advance_roulette_stack(
    conn: sqlite3.Connection, user_id: str, display_name: str
) -> int:
    """신용 유저의 연승 스택을 하나 올리고, 목표에 닿으면 보상을 주고 0으로 되돌린다."""
    before = get_roulette_stack(conn, user_id)
    stack = before + 1
    if stack < ROULETTE_STACK_GOAL:
        set_roulette_stack(conn, user_id, stack, "win", before=before)
        return stack

    # 보상 지급 전에 먼저 0으로 내린다. add_score가 신용 상태 탈출을 감지하면
    # 스택을 또 지우려 드는데, 이미 0이면 로그가 두 줄로 갈라지지 않는다.
    set_roulette_stack(conn, user_id, 0, "payout", before=before)
    add_score(
        conn,
        user_id,
        display_name,
        ROULETTE_STACK_REWARD,
        f"러시안룰렛 {ROULETTE_STACK_GOAL}연승 보상",
    )
    return 0
def settle_roulette_game(
    conn: sqlite3.Connection,
    row: sqlite3.Row | dict[str, Any],
    winner_id: str,
    loser_id: str,
    cause: str,
) -> dict[str, Any]:
    """승패가 정해진 뒤의 점수·스택 정산. 실탄 격발과 시간 초과가 함께 쓴다.

    cause: fired | timeout
    """
    game_id = int(row["id"])
    winner_side = "challenger" if winner_id == str(row["challenger_id"]) else "target"
    loser_side = "target" if winner_side == "challenger" else "challenger"
    winner_name = str(row[f"{winner_side}_name"])
    stake = int(row[f"{winner_side}_bet"])
    pot = int(row["challenger_bet"]) + int(row["target_bet"])
    winner_is_credit = bool(row[f"{winner_side}_negative"])

    # 신용 유저는 이겨도 점수가 오르지 않는다. 외상으로 낸 예치금만 돌려받아 순변동이
    # 0이 되고, 진 쪽이 낸 몫은 시스템에서 사라진다.
    payout = stake if winner_is_credit else pot
    add_score(conn, winner_id, winner_name, payout, f"러시안룰렛 #{game_id} 승리")

    stack_after: int | None = None
    if winner_is_credit and cause == "fired":
        # 시간 초과 승리는 스택에 세지 않는다. 실탄 위치는 못 고르지만 '안 누르는 것'은
        # 고를 수 있어서, 짜고 잠수해주면 확률 없이 스택을 채울 수 있다.
        stack_after = advance_roulette_stack(conn, winner_id, winner_name)
    if bool(row[f"{loser_side}_negative"]):
        set_roulette_stack(conn, loser_id, 0, "loss")

    conn.execute(
        """
        UPDATE russian_roulette_games
        SET status = 'finished', shots_fired = shots_fired + ?,
            winner_id = ?, loser_id = ?, finished_at = ?, finish_cause = ?,
            winner_stack_after = ?, expires_at = NULL
        WHERE id = ? AND status = 'playing'
        """,
        (
            1 if cause == "fired" else 0,
            winner_id,
            loser_id,
            now_text(),
            cause,
            stack_after,
            game_id,
        ),
    )
    return {
        "stake": stake,
        "pot": pot,
        "payout": payout,
        "winner_is_credit": winner_is_credit,
        "stack_after": stack_after,
    }
def pull_roulette_trigger(
    conn: sqlite3.Connection, game_id: int, user_id: str
) -> tuple[str, dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM russian_roulette_games WHERE id = ?", (game_id,)
    ).fetchone()
    if not row:
        raise ValueError("존재하지 않는 게임입니다.")
    if row["status"] != "playing":
        raise ValueError("현재 진행 중인 게임이 아닙니다.")
    if str(row["current_player_id"]) != user_id:
        raise ValueError("지금은 상대방의 차례입니다.")

    challenger_id = str(row["challenger_id"])
    target_id = str(row["target_id"])
    if int(row["shots_fired"]) == int(row["live_chamber"]):
        loser_id = user_id
        winner_id = target_id if user_id == challenger_id else challenger_id
        result = settle_roulette_game(conn, row, winner_id, loser_id, "fired")
        log_activity(
            conn,
            "roulette",
            "shot",
            actor_user_id=user_id,
            # 승자를 target으로 걸어야 승자 쪽 타임라인에서도 이 격발이 보인다.
            # (actor/target 어느 쪽이든 걸리게 조회하므로, 비워두면 이긴 사람 기록에서 빠진다)
            target_user_id=winner_id,
            payload={
                "game_id": game_id,
                "shot_index": int(row["shots_fired"]),
                "result": "fired",
                "winner_id": winner_id,
                "loser_id": loser_id,
                **result,
            },
        )
        state = "fired"
    else:
        next_player_id = target_id if user_id == challenger_id else challenger_id
        turn_deadline = time.time() + ROULETTE_TURN_SECONDS
        conn.execute(
            """
            UPDATE russian_roulette_games
            SET shots_fired = shots_fired + 1, current_player_id = ?, expires_at = ?
            WHERE id = ? AND status = 'playing'
            """,
            (next_player_id, turn_deadline, game_id),
        )
        # shots_fired는 카운터라 '누가 몇 번째를 쐈는지'가 남지 않는다. 격발마다 한 줄씩 남긴다.
        log_activity(
            conn,
            "roulette",
            "shot",
            actor_user_id=user_id,
            target_user_id=next_player_id,
            payload={
                "game_id": game_id,
                "shot_index": int(row["shots_fired"]),
                "result": "blank",
            },
        )
        state = "blank"

    fresh = conn.execute(
        "SELECT * FROM russian_roulette_games WHERE id = ?", (game_id,)
    ).fetchone()
    return state, dict(fresh)
def timeout_roulette_turn(
    conn: sqlite3.Connection, game_id: int, shot_index: int
) -> dict[str, Any] | None:
    """제한 시간 안에 방아쇠를 당기지 않은 쪽을 패배 처리한다.

    무승부 환불로 두면 잠수가 지배 전략이 된다. 탄창을 다시 돌리지 않아 차례가 뒤로
    갈수록 확률이 나빠지고, 마지막 차례는 격발하면 100% 죽기 때문이다.

    shot_index는 타이머가 감시하기로 한 차례. 마감 시각으로 식별하면 안 되는데,
    격발 직후 잡히는 다음 마감이 직전 마감과 사실상 같은 값일 수 있어서 낡은 타이머가
    살아 있는 차례를 죽일 수 있다. shots_fired는 차례마다 반드시 하나씩 오른다.
    """
    row = conn.execute(
        "SELECT * FROM russian_roulette_games WHERE id = ? AND status = 'playing'",
        (game_id,),
    ).fetchone()
    if not row:
        return None
    if int(row["shots_fired"]) != shot_index:
        return None

    loser_id = str(row["current_player_id"])
    challenger_id = str(row["challenger_id"])
    winner_id = str(row["target_id"]) if loser_id == challenger_id else challenger_id
    result = settle_roulette_game(conn, row, winner_id, loser_id, "timeout")
    log_activity(
        conn,
        "roulette",
        "timeout",
        actor_user_id=loser_id,
        target_user_id=winner_id,
        payload={
            "game_id": game_id,
            "shot_index": int(row["shots_fired"]),
            "winner_id": winner_id,
            "loser_id": loser_id,
            **result,
        },
    )
    fresh = conn.execute(
        "SELECT * FROM russian_roulette_games WHERE id = ?", (game_id,)
    ).fetchone()
    return dict(fresh)
def roulette_interaction_matches(
    interaction: discord.Interaction,
    game: sqlite3.Row | dict[str, Any],
    *,
    require_message: bool = True,
) -> bool:
    if interaction.guild is None or str(interaction.guild.id) != str(game["guild_id"]):
        return False
    if interaction.channel is None or str(interaction.channel.id) != str(game["channel_id"]):
        return False
    if require_message:
        message = interaction.message
        return message is not None and (
            not game["message_id"] or str(message.id) == str(game["message_id"])
        )
    return True
async def refresh_roulette_message(
    client: discord.Client, game: sqlite3.Row | dict[str, Any]
) -> None:
    channel = client.get_channel(int(game["channel_id"]))
    if channel is None:
        try:
            channel = await client.fetch_channel(int(game["channel_id"]))
        except discord.HTTPException:
            return
    if not hasattr(channel, "fetch_message") or not game["message_id"]:
        return
    try:
        message = await channel.fetch_message(int(game["message_id"]))
        await message.edit(
            content=None if game["status"] != "pending" else message.content,
            embed=build_roulette_embed(game),
            view=roulette_view(game),
        )
    except discord.HTTPException:
        return
async def apply_roulette_bet(
    interaction: discord.Interaction, game_id: int, amount: int
) -> None:
    """베팅 제출의 공통 처리. 모달 입력과 신용 유저의 즉시 확정이 함께 쓴다."""
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM russian_roulette_games WHERE id = ?", (game_id,)
            ).fetchone()
            if not row or not roulette_interaction_matches(
                interaction, row, require_message=False
            ):
                raise ValueError("이 채널의 유효한 결투가 아닙니다.")
            state, game = submit_roulette_bet(
                conn, game_id, str(interaction.user.id), amount
            )
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    side = (
        "challenger"
        if str(interaction.user.id) == str(game["challenger_id"])
        else "target"
    )
    if state == "started":
        message = (
            f"**{game['challenger_bet']}점**으로 예치하고 게임을 시작했습니다. "
            "진행판에서 차례를 확인해주세요."
        )
    elif state == "balance_changed":
        message = (
            "게임 시작 직전 점수가 바뀐 플레이어의 베팅을 취소했습니다. "
            "해당 플레이어가 다시 입력해주세요."
        )
    else:
        message = "베팅을 저장했습니다. 상대방의 입력을 기다립니다."
    if bool(game[f"{side}_negative"]):
        # 클램프됐다는 사실은 본인에게만 알린다. 진행판에는 최소액을 건 다른 플레이어와
        # 똑같이 '입력 완료'로만 뜬다.
        message += (
            f"\n(가진 점수가 {ROULETTE_MIN_BET}점보다 적어 {ROULETTE_MIN_BET}점으로 "
            "자동 처리했습니다. 이겨도 점수는 오르지 않고 연승 스택이 쌓입니다.)"
        )

    await interaction.response.send_message(message, ephemeral=True)
    await refresh_roulette_message(interaction.client, game)

    if state == "started" and game["expires_at"] is not None:
        client = interaction.client
        if isinstance(client, RouletteTimerClient):
            client.schedule_roulette_turn(
                int(game["id"]), float(game["expires_at"]), int(game["shots_fired"])
            )
class RouletteBetModal(discord.ui.Modal):
    def __init__(self, game_id: int) -> None:
        self.game_id = game_id
        super().__init__(title="러시안룰렛 베팅")
        self.amount = discord.ui.TextInput(
            label="걸 점수",
            placeholder=f"{ROULETTE_MIN_BET}~{ROULETTE_MAX_BET} (서로 다르면 낮은 쪽으로 맞춰집니다)",
            min_length=1,
            max_length=2,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = str(self.amount.value).strip()
        if not value.isdigit():
            await interaction.response.send_message(
                "베팅 점수는 양의 정수로 입력해주세요.", ephemeral=True
            )
            return
        await apply_roulette_bet(interaction, self.game_id, int(value))
async def handle_roulette_accept(interaction: discord.Interaction, game_id: int) -> None:
    with db() as conn:
        expire_stale_roulette_games(conn)
        row = conn.execute(
            "SELECT * FROM russian_roulette_games WHERE id = ?", (game_id,)
        ).fetchone()
        if not row or not roulette_interaction_matches(interaction, row):
            await interaction.response.send_message("이 메시지의 유효한 결투가 아닙니다.", ephemeral=True)
            return
        if str(interaction.user.id) != str(row["target_id"]):
            await interaction.response.send_message("지목받은 플레이어만 수락할 수 있습니다.", ephemeral=True)
            return
        if row["status"] != "pending":
            await interaction.response.send_message("이미 처리되었거나 만료된 결투입니다.", ephemeral=True)
            return

        challenger_id = str(row["challenger_id"])
        target_id = str(row["target_id"])

        # 지목당한 쪽은 pending으로 묶이지 않아 여러 건을 동시에 받아둘 수 있다.
        # 수락하는 순간 하나로 좁힌다.
        busy = active_roulette_game_for_user(conn, target_id)
        if busy is not None and int(busy["id"]) != game_id:
            await interaction.response.send_message(
                "이미 진행 중이거나 베팅을 기다리는 러시안룰렛이 있습니다.", ephemeral=True
            )
            return

        if count_roulette_games_today(conn, target_id) >= ROULETTE_DAILY_LIMIT:
            await interaction.response.send_message(
                f"오늘 러시안룰렛을 {ROULETTE_DAILY_LIMIT}번 모두 사용했습니다. "
                "내일 다시 도전해주세요.",
                ephemeral=True,
            )
            return
        if (
            count_roulette_games_with_opponent_today(conn, target_id, challenger_id)
            >= ROULETTE_PAIR_DAILY_LIMIT
        ):
            await interaction.response.send_message(
                "오늘 이 상대와는 이미 겨뤘습니다. 같은 상대와는 하루 "
                f"{ROULETTE_PAIR_DAILY_LIMIT}번까지입니다.",
                ephemeral=True,
            )
            return

        # 신청자 쪽 사정으로 성립하지 않는 경우는 사유를 밝히지 않는다. 밝히면
        # 수락 버튼이 상대의 점수와 잔여 횟수를 캐는 무료 조회 수단이 된다.
        challenger_row = conn.execute(
            "SELECT total_score FROM users WHERE user_id = ?", (challenger_id,)
        ).fetchone()
        challenger_score = int(challenger_row["total_score"]) if challenger_row else 0
        if (
            is_roulette_credit_user(challenger_score)
            or count_roulette_games_today(conn, challenger_id) >= ROULETTE_DAILY_LIMIT
        ):
            conn.execute(
                "UPDATE russian_roulette_games SET status = 'expired', finished_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (now_text(), game_id),
            )
            dead = dict(
                conn.execute(
                    "SELECT * FROM russian_roulette_games WHERE id = ?", (game_id,)
                ).fetchone()
            )
            await interaction.response.edit_message(
                content=None, embed=build_roulette_embed(dead), view=None
            )
            await interaction.followup.send("결투가 성립하지 않았습니다.", ephemeral=True)
            return

        conn.execute(
            """
            UPDATE russian_roulette_games
            SET status = 'betting', accepted_at = ?, expires_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (now_text(), time.time() + ROULETTE_BETTING_SECONDS, game_id),
        )
        log_activity(
            conn,
            "roulette",
            "accept",
            actor_user_id=str(interaction.user.id),
            target_user_id=str(row["challenger_id"]),
            payload={"game_id": game_id},
        )
        game = dict(
            conn.execute(
                "SELECT * FROM russian_roulette_games WHERE id = ?", (game_id,)
            ).fetchone()
        )
    await interaction.response.edit_message(
        content=None,
        embed=build_roulette_embed(game),
        view=roulette_view(game),
    )
async def handle_roulette_decline(interaction: discord.Interaction, game_id: int) -> None:
    with db() as conn:
        expire_stale_roulette_games(conn)
        row = conn.execute(
            "SELECT * FROM russian_roulette_games WHERE id = ?", (game_id,)
        ).fetchone()
        if not row or not roulette_interaction_matches(interaction, row):
            await interaction.response.send_message("이 메시지의 유효한 결투가 아닙니다.", ephemeral=True)
            return
        if str(interaction.user.id) != str(row["target_id"]):
            await interaction.response.send_message("지목받은 플레이어만 거절할 수 있습니다.", ephemeral=True)
            return
        if row["status"] != "pending":
            await interaction.response.send_message("이미 처리되었거나 만료된 결투입니다.", ephemeral=True)
            return
        conn.execute(
            "UPDATE russian_roulette_games SET status = 'declined', finished_at = ? WHERE id = ?",
            (now_text(), game_id),
        )
        log_activity(
            conn,
            "roulette",
            "decline",
            actor_user_id=str(interaction.user.id),
            target_user_id=str(row["challenger_id"]),
            payload={"game_id": game_id},
        )
        game = dict(
            conn.execute(
                "SELECT * FROM russian_roulette_games WHERE id = ?", (game_id,)
            ).fetchone()
        )
    await interaction.response.edit_message(
        content=None,
        embed=build_roulette_embed(game),
        view=None,
    )
async def handle_roulette_bet(interaction: discord.Interaction, game_id: int) -> None:
    with db() as conn:
        expire_stale_roulette_games(conn)
        row = conn.execute(
            "SELECT * FROM russian_roulette_games WHERE id = ?", (game_id,)
        ).fetchone()
        valid = bool(row) and roulette_interaction_matches(interaction, row)
        is_player = bool(row) and str(interaction.user.id) in {
            str(row["challenger_id"]),
            str(row["target_id"]),
        }
        is_betting = bool(row) and row["status"] == "betting"
        balance_row = conn.execute(
            "SELECT total_score FROM users WHERE user_id = ?", (str(interaction.user.id),)
        ).fetchone()
        credit = is_roulette_credit_user(
            int(balance_row["total_score"]) if balance_row else 0
        )
    if not valid:
        await interaction.response.send_message("이 메시지의 유효한 결투가 아닙니다.", ephemeral=True)
        return
    if not is_player:
        await interaction.response.send_message("이 결투의 플레이어만 베팅할 수 있습니다.", ephemeral=True)
        return
    if not is_betting:
        await interaction.response.send_message("베팅 입력 시간이 끝났습니다.", ephemeral=True)
        return
    if credit:
        # 고를 금액이 없으니 모달을 띄우지 않고 바로 확정한다. 누르자마자 '입력 완료'로
        # 바뀌는 게 밖에서 보이는 유일한 차이인데, 그걸 읽어내는 것도 게임의 일부로 둔다.
        await apply_roulette_bet(interaction, game_id, ROULETTE_MIN_BET)
        return
    await interaction.response.send_modal(RouletteBetModal(game_id))
def roulette_player_score_message(
    conn: sqlite3.Connection,
    game: sqlite3.Row | dict[str, Any],
    user_id: str,
) -> str:
    challenger_id = str(game["challenger_id"])
    target_id = str(game["target_id"])
    if user_id not in {challenger_id, target_id}:
        raise ValueError("이 결투의 플레이어만 점수를 확인할 수 있습니다.")

    score_row = conn.execute(
        "SELECT total_score FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    score = int(score_row["total_score"]) if score_row else 0
    message = f"현재 보유 점수: **{score}점**"

    if game["status"] == "playing":
        bet_column = "challenger_bet" if user_id == challenger_id else "target_bet"
        message += f"\n러시안룰렛 예치 점수: **{int(game[bet_column])}점**"
    return message
async def handle_roulette_score(interaction: discord.Interaction, game_id: int) -> None:
    try:
        with db() as conn:
            expire_stale_roulette_games(conn)
            row = conn.execute(
                "SELECT * FROM russian_roulette_games WHERE id = ?", (game_id,)
            ).fetchone()
            if not row or not roulette_interaction_matches(interaction, row):
                raise ValueError("이 메시지의 유효한 결투가 아닙니다.")
            if row["status"] not in {"pending", "betting", "playing"}:
                raise ValueError("이미 종료된 결투입니다.")
            message = roulette_player_score_message(
                conn, row, str(interaction.user.id)
            )
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    await interaction.response.send_message(message, ephemeral=True)
def roulette_settlement_message(
    conn: sqlite3.Connection,
    game: sqlite3.Row | dict[str, Any],
    user_id: str,
) -> str:
    """끝난 판의 개인 정산.

    공개 결과에는 담을 수 없는 것(신용 유저의 실제 점수 변동, 연승 스택)을 본인에게만
    보여준다. 공개 embed는 신용 유저든 아니든 똑같이 '점수를 빼앗았다'로 나간다.
    """
    challenger_id = str(game["challenger_id"])
    target_id = str(game["target_id"])
    if user_id not in {challenger_id, target_id}:
        raise ValueError("이 결투의 플레이어만 정산을 확인할 수 있습니다.")

    side = "challenger" if user_id == challenger_id else "target"
    stake = int(game[f"{side}_bet"] or 0)
    credit = bool(game[f"{side}_negative"])
    won = str(game["winner_id"] or "") == user_id
    timed_out = str(game["finish_cause"] or "") == "timeout"

    lines: list[str] = []
    if won and credit:
        lines.append(f"결과: **승리** — 점수 변동 **없음** (건 {stake}점을 그대로 돌려받음)")
    elif won:
        lines.append(f"결과: **승리** — **+{stake}점** (예치 {stake}점 회수 + 상대 {stake}점)")
    else:
        lines.append(f"결과: **패배** — **-{stake}점**")
    if timed_out:
        lines.append(
            "종료 사유: 제한 시간 초과"
            + ("" if won else " (방아쇠를 당기지 않아 자동 패배)")
        )

    if credit:
        if won and timed_out:
            lines.append("시간 초과로 얻은 승리는 연승 스택에 세지 않습니다.")
        elif won:
            stack_after = game["winner_stack_after"]
            if stack_after == 0:
                lines.append(
                    f"🎉 **{ROULETTE_STACK_GOAL}연승 달성 — 보상 +{ROULETTE_STACK_REWARD}점!** "
                    "스택은 0으로 초기화됐습니다."
                )
            else:
                lines.append(
                    f"연승 스택 **{stack_after}/{ROULETTE_STACK_GOAL}** "
                    f"— {ROULETTE_STACK_GOAL}연승하면 +{ROULETTE_STACK_REWARD}점입니다."
                )
        else:
            lines.append("연승 스택이 초기화됐습니다.")

    score_row = conn.execute(
        "SELECT total_score FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    score = int(score_row["total_score"]) if score_row else 0
    lines.append(f"현재 보유 점수: **{score}점**")
    if is_roulette_credit_user(score):
        lines.append(f"현재 연승 스택: **{get_roulette_stack(conn, user_id)}/{ROULETTE_STACK_GOAL}**")
    return "\n".join(lines)
async def handle_roulette_settle(interaction: discord.Interaction, game_id: int) -> None:
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM russian_roulette_games WHERE id = ?", (game_id,)
            ).fetchone()
            if not row or not roulette_interaction_matches(interaction, row):
                raise ValueError("이 메시지의 유효한 결투가 아닙니다.")
            if row["status"] != "finished":
                raise ValueError("아직 끝나지 않은 결투입니다.")
            message = roulette_settlement_message(conn, row, str(interaction.user.id))
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    await interaction.response.send_message(message, ephemeral=True)
async def handle_roulette_trigger(interaction: discord.Interaction, game_id: int) -> None:
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM russian_roulette_games WHERE id = ?", (game_id,)
            ).fetchone()
            if not row or not roulette_interaction_matches(interaction, row):
                raise ValueError("이 메시지의 유효한 결투가 아닙니다.")
            state, game = pull_roulette_trigger(conn, game_id, str(interaction.user.id))
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    await interaction.response.edit_message(
        content=None,
        embed=build_roulette_embed(game),
        view=roulette_view(game),
    )

    client = interaction.client
    if isinstance(client, RouletteTimerClient):
        # 제때 눌렀으니 이전 차례의 타이머는 취소한다. 취소되면 카운트다운 함수가
        # 자기가 뿌린 예고/카운트다운 메시지를 지우고 나가므로 채널에 흔적이 남지 않는다.
        if state == "blank" and game["expires_at"] is not None:
            client.schedule_roulette_turn(
                game_id, float(game["expires_at"]), int(game["shots_fired"])
            )
        else:
            client.cancel_roulette_turn(game_id)
