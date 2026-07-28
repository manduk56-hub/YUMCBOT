"""가라앉는 배 — 보상금 배분을 협상하고, 노동은 각자 비공개로 제출하는 메인게임.

**협상하는 것은 보상금 배분이고, 노동은 협상 대상이 아니다.** 이 분리가 게임의 전부다:

- 협상에서 정하는 것은 생존 시 받을 보상금 `BAIL_REWARD_POOL`의 배분뿐이다.
  결렬하면 `BAIL_FAILED_REWARD_POOL`로 줄어 각자 절반씩 받는다.
- **누가 몇 번 퍼낼지는 아무도 약속하지 않는다.** 실행 단계에서 각자 비공개로 제출하고,
  배가 뜨는지는 두 제출값의 합만으로 결정된다. 그래서 배신 = 무임승차가 된다.
- **침몰 페널티는 보상 몫에 비례한다**(`BAIL_SINK_PENALTY_HIGH/LOW/EVEN`). 많이 받기로
  한 쪽이 더 크게 잃어야 협상에서 욕심낸 만큼 리스크를 지고, 적게 받은 쪽에 노동을 깎을
  카드가 생긴다. **대칭으로 바꾸면 배신 동기가 사라져 게임이 조율 문제로 붕괴한다.**

단계가 넷이다: `registration → negotiation → execution → scored`. 그래서 자동 마감
타이머가 한 번 더 돌고, 복구 콜백과 `/라운드마감` 양쪽에 전이 분기가 있어야 한다.
협상 → 실행 전이에서 버튼 구성이 바뀌므로 **모든 방의 진행판을 다시 그린다**
(`refresh_all_bail_pair_boards`) — 누른 방만 갱신하는 모래성과 다른 점이다.

**조기 전이는 협상 단계에만 있다. 실행 단계는 제한 시간을 끝까지 쓴다.** 두 사람이 다
제출해도 그 자리에서 정산하지 않는다. 진행판이 제출 인원수를 세지 않는 것
(`bail_pair_status_text`)과 한 묶음이다 — 결과가 뜨는 시점이나 "1/2" 표시 중 하나라도
남으면 상대가 제출했는지가 새고, 비공개 제출이 눈치싸움이 된다. 그래서 퍼내기 제출은
마감 전까지 몇 번이든 덮어쓸 수 있다(`apply_bail_scoop`). **조기 정산이나 "1/2" 표시를
되살리면 번복 허용부터 같이 걷어내야 한다.**

진행판은 모래성과 달리 **제자리에서 `edit`만 한다**(`redraw_bail_pair_board`).
협상·퍼내기가 여러 번 오가는 게임이라 매번 지웠다 붙이면 방이 깜빡여 읽기가 나쁘다.
"""

from __future__ import annotations

import json
import random
import time
import sqlite3
from typing import Any

import discord

from .. import runtime
from ..config import (
    BAIL_DEFAULT_EXECUTION_MINUTES,
    BAIL_DEFAULT_NEGOTIATION_MINUTES,
    BAIL_FAILED_REWARD_POOL,
    BAIL_FALLBACK_SCOOPS,
    BAIL_NEGOTIATION_MAX_OFFERS,
    BAIL_OUTCOME_LABELS,
    BAIL_REQUIRED_SCOOPS,
    BAIL_REWARD_CHOICES,
    BAIL_REWARD_POOL,
    BAIL_SCOOP_CHOICES,
    BAIL_SCOOP_COST,
    BAIL_SINK_PENALTY_EVEN,
    BAIL_SINK_PENALTY_HIGH,
    BAIL_SINK_PENALTY_LOW,
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
)
from ..rounds import (
    round_extra,
    round_game_key,
)
from ..ui import (
    close_time_text,
    field_text,
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

# 가라앉는 배 임베드의 공통 색. discord.Color를 쓰므로 config(의존 없는 최하단)에 못 둔다.
BAIL_EMBED_COLOR = discord.Color.from_rgb(60, 120, 180)


def bail_rules_text() -> str:
    """규칙 안내문. 점수 상수를 조정하면 안내문도 따라 바뀌도록 런타임에 만든다."""
    return (
        "**🤝 1차 결정 · 보상금 협상**\n"
        f"**1** ─ 두 사람은 기본 **{BAIL_REWARD_POOL}점**을 가지며, 이를 어떻게 배분할지 협상합니다.\n"
        f"**2** ─ 제안과 수락을 번갈아 **최대 {BAIL_NEGOTIATION_MAX_OFFERS}번** 할 수 있으며, 수락 시 그 배분으로 확정됩니다.\n"
        f"**3** ─ 협상이 결렬될 시 보상금이 **{BAIL_FAILED_REWARD_POOL}점**으로 줄어 "
        f"각각 **{BAIL_FAILED_REWARD_POOL // 2}점**씩 받습니다.\n"
        "\n"
        "**🪣 2차 결정 · 물 퍼내기**\n"
        f"**1** ─ 배가 뜨려면 두 사람이 합쳐 **{BAIL_REQUIRED_SCOOPS}번**을 퍼내야 하며, 횟수를 서로 배분해야 합니다.\n"
        "**2** ─ 이 선택은 **상대에게 보이지 않습니다.** 제출했는지 여부조차 표시되지 않습니다.\n"
        "**3** ─ 마감 전까지 **몇 번이든 다시 고를 수 있고**, 마지막에 누른 값만 반영됩니다.\n"
        "**4** ─ 결과는 실행 마감 시각에 두 방 모두 한꺼번에 공개됩니다.\n"
        "\n"
        f"> 🪣 퍼내기 **1회당 -{BAIL_SCOOP_COST}점**입니다.\n"
        "> ⛵ 퍼내기를 성공하여 배가 뜨면 이전에 배분한 점수를 받습니다.\n"
        "> 🌊 **침몰 시** — 보상금 없이 아래 페널티\n"
        "```\n"
        f"많은 쪽  -{BAIL_SINK_PENALTY_HIGH}점\n"
        f"적은 쪽  -{BAIL_SINK_PENALTY_LOW}점\n"
        f"같을 시  -{BAIL_SINK_PENALTY_EVEN}점\n"
        "```"
    )


def find_bail_pair(extra: dict[str, Any], user_id: str) -> dict[str, Any] | None:
    for pair in extra.get("bail_pairs", []):
        if user_id in {str(pair.get("user_a_id")), str(pair.get("user_b_id"))}:
            return pair
    return None


def bail_pair_thread_matches(pair: dict[str, Any], channel_id: int | None) -> bool:
    """누른 곳이 본인 짝의 비공개 방인지 확인한다."""
    thread_id = str(pair.get("thread_id") or "")
    if not thread_id:
        return True
    return thread_id == str(channel_id)


def bail_side_key(pair: dict[str, Any], user_id: str) -> str:
    return "user_a" if user_id == str(pair["user_a_id"]) else "user_b"


def bail_reward_share(pair: dict[str, Any], side: str) -> int:
    """생존 시 받을 보상 몫. 협상이 결렬됐으면 감액된 보상금을 균등 분배한다."""
    split = pair.get("reward_split")
    if not split:
        return BAIL_FAILED_REWARD_POOL // 2
    return int(split[side])


def bail_new_pair(user_a_id: str, user_b_id: str) -> dict[str, Any]:
    return {
        "user_a_id": user_a_id,
        "user_b_id": user_b_id,
        "phase": "negotiating",
        "proposer_id": random.choice([user_a_id, user_b_id]),
        "offer": None,
        "offer_count": 0,
        "reward_split": None,
        "scoop_a": None,
        "scoop_b": None,
    }


def apply_bail_offer(pair: dict[str, Any], user_id: str, reward: int) -> str:
    """제안권자가 보상금 배분안을 제시한다. 호출 전에 단계·차례 검증이 끝나 있어야 한다."""
    other_reward = BAIL_REWARD_POOL - reward
    pair.update(
        {
            "offer": {"proposer_id": user_id, "reward": reward},
            "offer_count": int(pair.get("offer_count", 0)) + 1,
        }
    )
    return (
        f"제안했습니다 — 내가 **{reward}점**, 상대가 **{other_reward}점**.\n"
        "상대의 수락/거절을 기다립니다."
    )


def apply_bail_response(pair: dict[str, Any], user_id: str, accepted: bool) -> str:
    """대기 중인 제안에 응답한다. 수락이면 배분 확정, 거절이면 제안권이 넘어간다."""
    offer = pair["offer"]
    proposer_id = str(offer["proposer_id"])
    proposer_reward = int(offer["reward"])
    responder_reward = BAIL_REWARD_POOL - proposer_reward

    if accepted:
        pair.update(
            {
                "reward_split": {
                    bail_side_key(pair, proposer_id): proposer_reward,
                    bail_side_key(pair, user_id): responder_reward,
                },
                "offer": None,
                "phase": "submitting",
            }
        )
        return (
            f"보상금 배분을 확정했습니다 — 내가 **{responder_reward}점**, "
            f"상대가 **{proposer_reward}점**.\n"
            "이제 실제로 퍼낼 횟수를 각자 제출합니다. **누가 몇 번 퍼낼지는 약속하지 않았습니다.**"
        )

    exhausted = int(pair.get("offer_count", 0)) >= BAIL_NEGOTIATION_MAX_OFFERS
    pair.update(
        {
            "offer": None,
            "proposer_id": proposer_id if exhausted else user_id,
            "phase": "submitting" if exhausted else "negotiating",
        }
    )
    if exhausted:
        return (
            "거절했습니다. 제안 횟수를 모두 소진해 협상이 **결렬**되었습니다 — "
            f"보상금이 {BAIL_FAILED_REWARD_POOL}점으로 줄어 각자 "
            f"{BAIL_FAILED_REWARD_POOL // 2}점입니다."
        )
    return "거절했습니다. 이제 내가 제안할 차례입니다."


def apply_bail_scoop(pair: dict[str, Any], user_id: str, scoops: int) -> int | None:
    """실행 단계 제출을 적용하고 **직전 값**을 돌려준다 (첫 제출이면 None).

    마감 전까지 몇 번이든 덮어쓸 수 있다. 진행판이 제출 여부를 세지 않고 정산도 마감에만
    일어나므로, 번복을 허용해도 상대가 알아챌 경로가 없다.
    """
    key = "scoop_a" if user_id == str(pair["user_a_id"]) else "scoop_b"
    previous = pair.get(key)
    pair[key] = scoops
    return None if previous is None else int(previous)


def bail_negotiations_complete(extra: dict[str, Any]) -> bool:
    """조기 전이는 협상 단계에만 있다. 실행 단계는 제한 시간을 끝까지 쓰므로 짝(sand의
    sand_games_complete 같은) '모두 끝났나' 판정을 두지 않는다."""
    pairs = extra.get("bail_pairs", [])
    return bool(pairs) and all(
        str(pair.get("phase")) in {"submitting", "resolved"} for pair in pairs
    )


def resolve_bail_pair(pair: dict[str, Any]) -> None:
    """제출된 두 횟수와 확정된 보상 배분으로 생존/침몰과 점수를 확정한다.

    노동에 대한 약속은 애초에 없다. 배가 뜨는지는 오직 제출된 두 횟수의 합으로 결정된다.
    """
    scoop_a = int(pair["scoop_a"])
    scoop_b = int(pair["scoop_b"])
    total = scoop_a + scoop_b
    survived = total >= BAIL_REQUIRED_SCOOPS
    share_a = bail_reward_share(pair, "user_a")
    share_b = bail_reward_share(pair, "user_b")

    if survived:
        score_a = share_a - scoop_a * BAIL_SCOOP_COST
        score_b = share_b - scoop_b * BAIL_SCOOP_COST
    elif share_a > share_b:
        score_a, score_b = -BAIL_SINK_PENALTY_HIGH, -BAIL_SINK_PENALTY_LOW
    elif share_b > share_a:
        score_a, score_b = -BAIL_SINK_PENALTY_LOW, -BAIL_SINK_PENALTY_HIGH
    else:
        score_a = score_b = -BAIL_SINK_PENALTY_EVEN

    pair.update(
        {
            "phase": "resolved",
            "outcome": "survived" if survived else "sunk",
            "result": {
                "total_scoops": total,
                "reward_a": share_a,
                "reward_b": share_b,
                "score_a": score_a,
                "score_b": score_b,
            },
        }
    )


def autocomplete_bail_pair(pair: dict[str, Any]) -> bool:
    """마감 시점에 안 끝난 짝을 자동으로 확정한다. 실제로 자동으로 채운 게 있으면 True.

    노동은 약속 대상이 아니라 기준으로 삼을 합의가 없다. 절반(BAIL_FALLBACK_SCOOPS)으로 채운다 —
    0으로 박으면 잠수 한 명이 상대까지 침몰시키고, 랜덤은 침몰 확률이 지나치게 높다.
    협상 중이었다면 결렬로 처리되어 감액된 보상금을 균등 분배받는다.

    실행 단계는 두 사람이 다 제출해도 마감까지 기다렸다가 여기서 정산한다. 그래서 **정상적으로
    둘 다 제출한 짝도 이 함수를 거친다** — `auto_completed`를 무조건 세우면 모든 방이 '자동 제출'로
    표시되므로, 빈칸을 실제로 메웠을 때만 세운다.
    """
    if str(pair.get("phase")) == "resolved":
        return False

    # 응답 없이 사라지는 제안이 남아 있거나 협상이 안 끝난 채로 마감된 것도 자동 처리다.
    auto_filled = bool(pair.get("offer")) or str(pair.get("phase")) == "negotiating"
    pair["offer"] = None
    for key in ("scoop_a", "scoop_b"):
        if pair.get(key) is None:
            pair[key] = BAIL_FALLBACK_SCOOPS
            auto_filled = True
    if auto_filled:
        pair["auto_completed"] = True
    resolve_bail_pair(pair)
    return auto_filled


def bail_score_text(delta: int) -> str:
    if delta > 0:
        return f"+{delta}점"
    if delta < 0:
        return f"{delta}점"
    return "±0점"


def bail_pair_settlement_text(pair: dict[str, Any]) -> str:
    """짝 하나의 정산 문구. 라운드 요약과 방별 결과 공개가 같은 문구를 쓴다."""
    result = pair.get("result")
    if not result:
        return "점수 변동 없음"
    return (
        f"<@{pair['user_a_id']}> {bail_score_text(int(result['score_a']))} · "
        f"<@{pair['user_b_id']}> {bail_score_text(int(result['score_b']))}"
    )


def bail_split_text(pair: dict[str, Any]) -> str:
    """보상 배분과 실제 퍼낸 횟수를 나란히 보여준다.

    돈은 많이 받고 일은 덜 한 사람에게 ⚠️를 붙인다. 무임승차가 공개 기록으로 남는 것이
    이벤트 전체에 걸친 평판 장치다.
    """
    split = pair.get("reward_split")
    scoop_a = pair.get("scoop_a")
    scoop_b = pair.get("scoop_b")
    share_a = bail_reward_share(pair, "user_a")
    share_b = bail_reward_share(pair, "user_b")

    if split:
        lines = [
            f"보상 배분: <@{pair['user_a_id']}> {share_a}점 · <@{pair['user_b_id']}> {share_b}점"
        ]
    else:
        lines = [f"보상 배분: **결렬** → 각자 {share_a}점"]

    if scoop_a is not None and scoop_b is not None:
        marks = []
        for user_key, share, other_share, scoop, other_scoop in (
            ("user_a_id", share_a, share_b, int(scoop_a), int(scoop_b)),
            ("user_b_id", share_b, share_a, int(scoop_b), int(scoop_a)),
        ):
            # 보상은 더 받으면서 노동은 덜 한 쪽.
            mark = " ⚠️" if share > other_share and scoop < other_scoop else ""
            marks.append(f"<@{pair[user_key]}> {scoop}번{mark}")
        lines.append(f"실제 퍼내기: {' · '.join(marks)}")
    return "\n".join(lines)


def start_bail_negotiation(round_id: int) -> dict[str, Any] | None:
    """참가 모집을 마감하고 짝을 지어 협상 단계를 시작한다."""
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'sand' AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row or round_game_key(round_row) != "bail":
            return None

        extra = round_extra(round_row)
        if extra.get("bail_phase") != "registration":
            return None

        registrations = conn.execute(
            "SELECT user_id FROM trust_registrations WHERE round_id = ? ORDER BY registered_at ASC",
            (round_id,),
        ).fetchall()
        user_ids = [str(row["user_id"]) for row in registrations]
        if len(user_ids) < 2:
            extra.update({"bail_phase": "cancelled", "cancelled_reason": "not_enough_participants"})
            conn.execute(
                "UPDATE rounds SET status = 'closed', closed_at = ?, extra_json = ? WHERE id = ?",
                (now_text(), json.dumps(extra, ensure_ascii=False), round_id),
            )
            embed = discord.Embed(
                title=f"{round_row['title']} · 취소",
                description="참여자가 2명 미만이라 가라앉는 배를 시작하지 못했습니다.",
                color=discord.Color.light_grey(),
            )
            return {
                "state": "cancelled",
                "channel_id": str(round_row["channel_id"]),
                "message_id": str(round_row["message_id"] or ""),
                "embed": embed,
            }

        # 짝 이력은 모래성과 공유한다(같은 type이라 recent_match_pairs가 함께 조회한다).
        recent_pairs = recent_match_pairs(conn, round_id, "sand")
        pairs, unmatched_user_id = build_random_trust_pairs(user_ids, recent_pairs)
        bail_pairs: list[dict[str, Any]] = []
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
                payload={"game_key": "bail"},
            )
            bail_pairs.append(bail_new_pair(user_a_id, user_b_id))
        log_activity(
            conn,
            "match",
            "pairing_complete",
            event_session_id=round_row["event_session_id"],
            round_id=round_id,
            payload={
                "game_key": "bail",
                "pair_count": len(pairs),
                "registered_count": len(user_ids),
                "unmatched_user_id": unmatched_user_id,
            },
        )

        negotiation_seconds = int(
            extra.get("negotiation_seconds") or BAIL_DEFAULT_NEGOTIATION_MINUTES * 60
        )
        close_at = time.time() + negotiation_seconds
        extra.update(
            {
                "bail_phase": "negotiation",
                "bail_pairs": bail_pairs,
                "unmatched_user_id": unmatched_user_id,
                "negotiation_close_at": close_at,
                "auto_close_at": close_at,
            }
        )
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )
        updated_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        return {
            "state": "negotiation",
            "channel_id": str(round_row["channel_id"]),
            "message_id": str(round_row["message_id"] or ""),
            "round_id": round_id,
            "title": str(round_row["title"]),
            "pairs": pairs,
            "embed": build_bail_play_embed(updated_row, extra),
            # 게임 버튼은 짝별 비공개 방 안에만 둔다. 메인 메시지는 진행판 역할만 한다.
            "view": emergency_join_view(round_id) if unmatched_user_id else None,
            "close_at": close_at,
        }


def start_bail_execution(round_id: int) -> dict[str, Any] | None:
    """협상을 마감하고 실행(비공개 제출) 단계로 넘긴다.

    협상 중이던 짝은 합의 없이 넘어간다. 모든 짝이 일찍 협상을 마쳐도 이 함수로 전이한다.
    """
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'sand' AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row or round_game_key(round_row) != "bail":
            return None

        extra = round_extra(round_row)
        if extra.get("bail_phase") != "negotiation":
            return None

        for pair in extra.get("bail_pairs", []):
            if str(pair.get("phase")) == "negotiating":
                # 대기 중인 제안은 응답 없이 사라진다. 합의가 없는 채로 실행 단계로 간다.
                pair.update({"offer": None, "phase": "submitting"})

        execution_seconds = int(
            extra.get("execution_seconds") or BAIL_DEFAULT_EXECUTION_MINUTES * 60
        )
        close_at = time.time() + execution_seconds
        extra.update(
            {
                "bail_phase": "execution",
                "execution_close_at": close_at,
                "auto_close_at": close_at,
            }
        )
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )
        updated_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        return {
            "state": "execution",
            "channel_id": str(round_row["channel_id"]),
            "message_id": str(round_row["message_id"] or ""),
            "round_id": round_id,
            "title": str(round_row["title"]),
            "embed": build_bail_play_embed(updated_row, extra),
            "view": emergency_join_view(round_id) if extra.get("unmatched_user_id") else None,
            "close_at": close_at,
        }


def bail_pair_intro(user_a_id: str, user_b_id: str) -> str:
    return (
        f"<@{user_a_id}>님과 <@{user_b_id}>님의 **1:1 구조 협상방**입니다.\n"
        f"> **1** ─ 구조 보상금 {BAIL_REWARD_POOL}점을 **어떻게 나눌지** 협상하세요. "
        "수락하면 확정됩니다.\n"
        f"> **2** ─ 배가 뜨려면 둘이 합쳐 {BAIL_REQUIRED_SCOOPS}번 퍼내야 하는데, "
        "**누가 몇 번 퍼낼지는 약속하지 않습니다** — 각자 비공개로 제출합니다."
    )


async def create_bail_pair_threads(transition: dict[str, Any]) -> None:
    """짝마다 비공개 방을 열고 그 안에 협상 버튼을 둔다."""
    if transition.get("state") != "negotiation":
        return

    channel = await runtime.resolve_text_channel(str(transition["channel_id"]))
    if channel is None:
        return

    round_id = int(transition["round_id"])
    title = str(transition["title"])

    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
    if not round_row:
        return
    extra = round_extra(round_row)

    created_threads: dict[tuple[str, str], tuple[str, str]] = {}
    failed_pairs = 0
    for pair_number, pair in enumerate(extra.get("bail_pairs", []), start=1):
        user_a_id = str(pair["user_a_id"])
        user_b_id = str(pair["user_b_id"])
        created = await create_pair_thread(
            channel,
            f"{title} · {pair_number}번 구조선",
            [user_a_id, user_b_id],
            f"가라앉는 배 #{round_id} 1:1 비공개 방",
            bail_pair_view(round_id, pair, extra),
            content=bail_pair_intro(user_a_id, user_b_id),
            embed=build_bail_pair_embed(title, pair, extra),
        )
        if created is None:
            failed_pairs += 1
            continue
        thread, message = created
        save_pair_thread_id(round_id, user_a_id, user_b_id, str(thread.id))
        created_threads[(user_a_id, user_b_id)] = (str(thread.id), str(message.id))

    if created_threads:
        store_bail_thread_ids(round_id, created_threads)
    await warn_failed_pair_threads(channel, round_id, failed_pairs)


def store_bail_thread_ids(
    round_id: int,
    created_threads: dict[tuple[str, str], tuple[str, str]],
) -> None:
    """스레드/메시지 id를 bail_pairs에 심는다. 재시작 후에도 방별 진행판을 갱신할 수 있어야 한다."""
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row:
            return
        extra = round_extra(round_row)
        for pair in extra.get("bail_pairs", []):
            key = (str(pair["user_a_id"]), str(pair["user_b_id"]))
            if key in created_threads:
                pair["thread_id"], pair["play_message_id"] = created_threads[key]
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )


def save_bail_pair_message_id(round_id: int, pair: dict[str, Any], message_id: str) -> None:
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row:
            return
        extra = round_extra(round_row)
        target = find_bail_pair(extra, str(pair["user_a_id"]))
        if not target:
            return
        target["play_message_id"] = message_id
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )


async def redraw_bail_pair_board(
    round_id: int,
    thread: discord.Thread,
    pair_user_id: str,
) -> None:
    """방 하나의 진행판을 **제자리에서 고쳐 쓴다.**

    모래성처럼 지웠다 맨 아래에 다시 붙이면 상호작용마다 진행판이 사라졌다 나타나 읽기가 어렵다.
    그래서 가라앉는 배는 협상·퍼내기 모두 같은 메시지를 `edit`만 한다 — ephemeral이 쌓여 진행판이
    위로 밀리더라도 위치가 고정되는 쪽을 택했다. 새로 보내는 건 진행판을 잃어버린 경우뿐이다.

    두 사람이 동시에 누르면 서로의 edit이 겹치므로, 락을 잡고 DB에서 현재 상태를 다시 읽는다.
    (락 dict는 모래성과 공유한다 — 키가 thread_id라 서로 충돌하지 않는다.)
    """
    async with pairing.board_client().sand_board_lock(thread.id):
        with db() as conn:
            round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row:
            return
        extra = round_extra(round_row)
        pair = find_bail_pair(extra, pair_user_id)
        if not pair:
            return

        embed = build_bail_pair_embed(str(round_row["title"]), pair, extra)
        view = bail_pair_view(round_id, pair, extra)

        board_message_id = str(pair.get("play_message_id") or "")
        if board_message_id:
            try:
                message = await thread.fetch_message(int(board_message_id))
                await message.edit(embed=embed, view=view)
                return
            except (discord.NotFound, discord.HTTPException, ValueError):
                pass

        try:
            new_board = await thread.send(embed=embed, view=view)
        except discord.HTTPException:
            return
        save_bail_pair_message_id(round_id, pair, str(new_board.id))


async def refresh_bail_pair_board(interaction: discord.Interaction, round_id: int) -> None:
    """누른 사람의 방 진행판만 갱신한다. 다른 짝의 상황은 새어 나가지 않는다."""
    channel = interaction.channel
    if not isinstance(channel, discord.Thread):
        return
    await redraw_bail_pair_board(round_id, channel, str(interaction.user.id))


async def bail_pair_threads(round_id: int) -> list[tuple[dict[str, Any], discord.Thread]]:
    """짝별 비공개 방 스레드를 모아 온다. 진행판 일괄 갱신과 마감 카운트다운이 함께 쓴다."""
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
    if not round_row:
        return []

    found: list[tuple[dict[str, Any], discord.Thread]] = []
    for pair in round_extra(round_row).get("bail_pairs", []):
        thread_id = str(pair.get("thread_id") or "")
        if not thread_id:
            continue
        thread = await runtime.resolve_thread(thread_id)
        if thread is not None:
            found.append((pair, thread))
    return found


async def refresh_all_bail_pair_boards(round_id: int) -> None:
    """모든 방의 진행판을 다시 그린다. 협상 → 실행 전이처럼 버튼 구성이 바뀔 때 쓴다."""
    for pair, thread in await bail_pair_threads(round_id):
        await redraw_bail_pair_board(round_id, thread, str(pair["user_a_id"]))


async def refresh_main_bail_board(round_id: int) -> None:
    """메인 채널의 실시간 진행판을 다시 그린다. 게임 버튼은 각 비공개 방에만 있다."""
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
    if not round_row or not round_row["message_id"]:
        return

    extra = round_extra(round_row)
    channel = await runtime.resolve_text_channel(str(round_row["channel_id"]))
    if channel is None:
        return
    try:
        message = await channel.fetch_message(int(round_row["message_id"]))
        await message.edit(
            embed=build_bail_play_embed(round_row, extra),
            view=emergency_join_view(round_id) if extra.get("unmatched_user_id") else None,
        )
    except (discord.HTTPException, ValueError):
        pass


async def publish_bail_pair_results(round_id: int) -> None:
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'sand'",
            (round_id,),
        ).fetchone()
    if not round_row or round_game_key(round_row) != "bail":
        return

    title = str(round_row["title"])
    extra = round_extra(round_row)
    for pair in extra.get("bail_pairs", []):
        thread_id = str(pair.get("thread_id") or "")
        if not thread_id:
            continue
        pairing.board_client().release_board_lock(int(thread_id))
        thread = await runtime.resolve_thread(thread_id)
        if thread is None:
            continue
        try:
            if thread.archived:
                await thread.edit(archived=False, reason="가라앉는 배 결과 공개")
            await thread.send(embed=build_bail_pair_result_embed(title, pair))
            # 라운드가 끝나 버튼이 필요 없어진 뒤에만 잠근다.
            await thread.edit(archived=True, locked=True, reason="가라앉는 배 결과 공개 완료")
        except (discord.Forbidden, discord.HTTPException, ValueError):
            continue


async def advance_or_finish_bail_round(
    interaction: discord.Interaction,
    round_id: int,
    private_message: str | None = None,
    public_message: str | None = None,
) -> None:
    """버튼 처리 후 응답을 보내고, 단계 전이/정산이 필요하면 이어서 처리한다."""
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row:
            await interaction.response.send_message("존재하지 않는 라운드입니다.", ephemeral=True)
            return
        channel_id = str(round_row["channel_id"])
        extra = round_extra(round_row)
        phase = str(extra.get("bail_phase") or "")
        negotiations_done = bail_negotiations_complete(extra)

    # 방 전체에 보여야 하는 안내는 공개로, 개인 선택 내용은 본인만 보게 보낸다.
    if public_message:
        await interaction.response.send_message(public_message)
        if private_message:
            await interaction.followup.send(private_message, ephemeral=True)
    elif private_message:
        await interaction.response.send_message(private_message, ephemeral=True)
    else:
        await interaction.response.defer()

    # 모든 짝이 협상을 마쳤으면 남은 시간을 기다리지 않고 실행 단계로 넘긴다.
    if phase == "negotiation" and negotiations_done:
        transition = start_bail_execution(round_id)
        if transition:
            pairing.board_client().schedule_auto_close(round_id, float(transition["close_at"]))
            await refresh_all_bail_pair_boards(round_id)
            await refresh_main_bail_board(round_id)
            channel = await runtime.resolve_text_channel(channel_id)
            if channel is not None:
                try:
                    await channel.send(
                        "🪣 모든 방의 협상이 끝났습니다. **실행 단계**를 시작합니다 — "
                        f"각자 실제로 퍼낼 횟수를 비공개로 제출하세요. (마감 {close_time_text(transition['close_at'])})"
                    )
                except discord.HTTPException:
                    pass
            return

    # 실행 단계는 조기 종료가 없다. 두 사람이 다 제출해도 마감 시각까지 기다렸다가
    # auto_close_round(또는 /라운드마감)가 모든 방을 한꺼번에 정산한다 — 여기서 일찍 닫으면
    # 결과가 뜨는 시점만으로 상대의 제출 여부가 드러나 비공개 제출이 깨진다.
    await refresh_bail_pair_board(interaction, round_id)
    await refresh_main_bail_board(round_id)


def load_open_bail_pair(
    conn: sqlite3.Connection,
    round_id: int,
    user_id: str,
    channel_id: int | None,
) -> tuple[sqlite3.Row, dict[str, Any], dict[str, Any]] | str:
    """열린 bail 라운드에서 이 유저의 짝을 찾는다. 문제가 있으면 안내 문구를 돌려준다."""
    round_row = conn.execute(
        "SELECT * FROM rounds WHERE id = ? AND type = 'sand' AND status = 'open'",
        (round_id,),
    ).fetchone()
    if not round_row or round_game_key(round_row) != "bail":
        return "이미 끝난 게임입니다."
    extra = round_extra(round_row)
    pair = find_bail_pair(extra, user_id)
    if not pair:
        return "이 라운드에 매칭된 참가자가 아닙니다."
    if not bail_pair_thread_matches(pair, channel_id):
        return "자신에게 배정된 1:1 방에서만 진행할 수 있습니다."
    return round_row, extra, pair


async def handle_bail_offer(
    interaction: discord.Interaction,
    round_id: int,
    reward: int,
) -> None:
    user_id = str(interaction.user.id)
    with db() as conn:
        loaded = load_open_bail_pair(conn, round_id, user_id, interaction.channel_id)
        if isinstance(loaded, str):
            await interaction.response.send_message(loaded, ephemeral=True)
            return
        round_row, extra, pair = loaded

        if extra.get("bail_phase") != "negotiation":
            await interaction.response.send_message("지금은 협상 단계가 아닙니다.", ephemeral=True)
            return
        if str(pair.get("phase")) != "negotiating":
            await interaction.response.send_message("이 방의 협상은 이미 끝났습니다.", ephemeral=True)
            return
        if pair.get("offer"):
            await interaction.response.send_message(
                "상대가 아직 내 제안에 답하지 않았습니다.", ephemeral=True
            )
            return
        if user_id != str(pair.get("proposer_id")):
            await interaction.response.send_message("지금은 상대가 제안할 차례입니다.", ephemeral=True)
            return

        private_message = apply_bail_offer(pair, user_id, reward)
        log_activity(
            conn,
            "bail",
            "offer",
            event_session_id=round_row["event_session_id"],
            round_id=round_id,
            actor_user_id=user_id,
            payload={
                "game_key": "bail",
                "proposer_reward": reward,
                "responder_reward": BAIL_REWARD_POOL - reward,
                "offer_count": int(pair.get("offer_count", 0)),
            },
        )
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )

    # 제안 내용은 상대가 봐야 하므로 방에 공개한다.
    await advance_or_finish_bail_round(
        interaction,
        round_id,
        public_message=(
            f"<@{user_id}>님의 제안 — 본인 **{reward}점** / 상대 "
            f"**{BAIL_REWARD_POOL - reward}점**"
        ),
        private_message=private_message,
    )


async def handle_bail_response(
    interaction: discord.Interaction,
    round_id: int,
    accepted: bool,
) -> None:
    user_id = str(interaction.user.id)
    with db() as conn:
        loaded = load_open_bail_pair(conn, round_id, user_id, interaction.channel_id)
        if isinstance(loaded, str):
            await interaction.response.send_message(loaded, ephemeral=True)
            return
        round_row, extra, pair = loaded

        if extra.get("bail_phase") != "negotiation":
            await interaction.response.send_message("지금은 협상 단계가 아닙니다.", ephemeral=True)
            return
        offer = pair.get("offer")
        if not offer:
            await interaction.response.send_message("응답할 제안이 없습니다.", ephemeral=True)
            return
        if user_id == str(offer["proposer_id"]):
            await interaction.response.send_message(
                "자신의 제안에는 응답할 수 없습니다. 상대의 응답을 기다려주세요.",
                ephemeral=True,
            )
            return

        proposer_id = str(offer["proposer_id"])
        proposer_reward = int(offer["reward"])
        private_message = apply_bail_response(pair, user_id, accepted)
        log_activity(
            conn,
            "bail",
            "offer_accepted" if accepted else "offer_rejected",
            event_session_id=round_row["event_session_id"],
            round_id=round_id,
            actor_user_id=user_id,
            target_user_id=proposer_id,
            payload={
                "game_key": "bail",
                "proposer_reward": proposer_reward,
                "responder_reward": BAIL_REWARD_POOL - proposer_reward,
                "offer_count": int(pair.get("offer_count", 0)),
                "negotiation_closed": str(pair.get("phase")) == "submitting",
            },
        )
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )

    if accepted:
        public_message = (
            f"🤝 보상금 배분 확정 — <@{proposer_id}> **{proposer_reward}점** / "
            f"<@{user_id}> **{BAIL_REWARD_POOL - proposer_reward}점**\n"
            f"> 배가 뜨려면 둘이 합쳐 {BAIL_REQUIRED_SCOOPS}번 퍼내야 합니다. "
            "누가 몇 번 퍼낼지는 각자 비공개로 제출합니다."
        )
    elif str(pair.get("phase")) == "submitting":
        public_message = (
            f"❌ <@{user_id}>님이 거절했습니다. 제안 횟수를 모두 소진해 협상이 **결렬**되었습니다 — "
            f"보상금이 {BAIL_FAILED_REWARD_POOL}점으로 줄어 각자 "
            f"{BAIL_FAILED_REWARD_POOL // 2}점입니다."
        )
    else:
        public_message = f"❌ <@{user_id}>님이 거절했습니다. 이제 <@{user_id}>님의 제안 차례입니다."
    await advance_or_finish_bail_round(
        interaction,
        round_id,
        public_message=public_message,
        private_message=private_message,
    )


async def handle_bail_scoop(
    interaction: discord.Interaction,
    round_id: int,
    scoops: int,
) -> None:
    user_id = str(interaction.user.id)
    with db() as conn:
        loaded = load_open_bail_pair(conn, round_id, user_id, interaction.channel_id)
        if isinstance(loaded, str):
            await interaction.response.send_message(loaded, ephemeral=True)
            return
        round_row, extra, pair = loaded

        if extra.get("bail_phase") != "execution":
            await interaction.response.send_message(
                "아직 실행 단계가 아닙니다. 협상이 모두 끝나면 제출할 수 있습니다.",
                ephemeral=True,
            )
            return
        if str(pair.get("phase")) == "resolved":
            await interaction.response.send_message("이 1:1 게임은 이미 끝났습니다.", ephemeral=True)
            return

        # 둘 다 제출해도 여기서 정산하지 않는다. 실행 단계는 제한 시간을 끝까지 쓰고
        # 마감 시점의 close_round_record가 한꺼번에 확정한다 — 조기 정산을 하면 결과 공개
        # 타이밍만으로 "상대가 이미 제출했다"가 드러나 제출 여부 비공개가 깨진다.
        # 정산이 마감에만 일어나는 덕에 그때까지 몇 번이든 다시 고를 수 있다.
        previous = apply_bail_scoop(pair, user_id, scoops)
        # extra_json은 통째로 교체되므로 덮어쓴 값은 여기 남기지 않으면 사라진다.
        log_activity(
            conn,
            "bail",
            "scoop_submitted",
            event_session_id=round_row["event_session_id"],
            round_id=round_id,
            actor_user_id=user_id,
            payload={
                "game_key": "bail",
                "scoops": scoops,
                "previous_scoops": previous,
                "reward_share": bail_reward_share(pair, bail_side_key(pair, user_id)),
                "negotiation_failed": not pair.get("reward_split"),
            },
        )
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )

    # 상대가 제출했는지는 본인에게도 알리지 않는다. 알려주면 남은 시간 동안 눈치싸움의
    # 재료가 되고, 두 사람이 서로에게 말해버리면 비공개 제출이 무의미해진다.
    # 내가 지금 몇 번으로 걸어뒀는지는 진행판이 아니라 이 ephemeral로만 확인한다.
    headline = (
        f"**{scoops}번** 퍼내기로 제출했습니다."
        if previous is None
        else f"**{previous}번 → {scoops}번**으로 바꿨습니다."
    )
    await advance_or_finish_bail_round(
        interaction,
        round_id,
        private_message=(
            f"{headline} 마감 전까지 다시 고를 수 있고, **마지막에 누른 값**만 반영됩니다.\n"
            "> 결과는 **실행 마감 시각**에 두 방 모두 한꺼번에 공개됩니다."
        ),
    )


def build_bail_registration_embed(round_id: int, title: str, registration_minutes: int) -> discord.Embed:
    embed = discord.Embed(title=title, description=title, color=BAIL_EMBED_COLOR)
    embed.add_field(name="🎮 진행 게임", value="가라앉는 배", inline=False)
    embed.add_field(name="📜 메인 규칙", value=bail_rules_text(), inline=False)
    return embed


def bail_pair_status_text(pair: dict[str, Any], extra: dict[str, Any]) -> str:
    """짝 하나의 진행 상황. 상대의 제출 내용은 절대 드러내지 않는다."""
    phase = str(pair.get("phase"))
    if phase == "resolved":
        outcome = BAIL_OUTCOME_LABELS.get(str(pair.get("outcome")), "종료")
        total = (pair.get("result") or {}).get("total_scoops")
        return f"{outcome} (합계 {total}번)" if total is not None else outcome

    if phase == "negotiating":
        offer = pair.get("offer")
        remaining = BAIL_NEGOTIATION_MAX_OFFERS - int(pair.get("offer_count", 0))
        if offer:
            responder_reward = BAIL_REWARD_POOL - int(offer["reward"])
            return (
                f"<@{offer['proposer_id']}>님의 배분 제안 대기 중 — "
                f"제안자 {int(offer['reward'])}점 / 상대 {responder_reward}점\n"
                f"　└ 상대가 수락/거절을 고릅니다 (남은 제안 {remaining}회)"
            )
        return f"<@{pair['proposer_id']}> 제안 차례 (남은 제안 {remaining}회)"

    # submitting
    if str(extra.get("bail_phase")) != "execution":
        if pair.get("reward_split"):
            return "배분 확정 · 다른 방의 협상을 기다립니다"
        return "협상 결렬 · 다른 방의 협상을 기다립니다"
    # 제출 인원수(0/2·1/2)는 그 자체가 정보다. 방 안에서는 상대의 제출 여부가 되고,
    # 메인 채널에서는 다른 방의 진행 속도가 새어 나간다. 마감 전까지는 아무것도 세지 않는다.
    return "퍼낼 횟수 제출 중 (제출 여부 비공개)"


def build_bail_pair_embed(title: str, pair: dict[str, Any], extra: dict[str, Any]) -> discord.Embed:
    """짝 하나의 비공개 방에 걸리는 진행판. 다른 짝의 정보는 담지 않는다."""
    embed = discord.Embed(
        title=f"{title} · 1:1 구조선",
        description=f"<@{pair['user_a_id']}> ↔ <@{pair['user_b_id']}>",
        color=BAIL_EMBED_COLOR,
    )
    embed.add_field(name="⚓ 진행 상황", value=bail_pair_status_text(pair, extra), inline=False)

    round_phase = str(extra.get("bail_phase") or "")
    pair_phase = str(pair.get("phase") or "")
    if pair.get("reward_split"):
        embed.add_field(
            name="🤝 확정된 보상금 배분",
            value=(
                f"<@{pair['user_a_id']}> {bail_reward_share(pair, 'user_a')}점 · "
                f"<@{pair['user_b_id']}> {bail_reward_share(pair, 'user_b')}점"
            ),
            inline=False,
        )
    elif pair_phase != "negotiating":
        embed.add_field(
            name="🤝 보상금 배분",
            value=(
                f"**협상 결렬** → 감액된 {BAIL_FAILED_REWARD_POOL}점을 나눠 각자 "
                f"{BAIL_FAILED_REWARD_POOL // 2}점"
            ),
            inline=False,
        )

    if round_phase == "negotiation" and pair_phase == "negotiating":
        embed.add_field(
            name="🗣️ 협상",
            value=(
                f"버튼의 숫자는 **내가 가져갈 보상금**입니다. 상대는 {BAIL_REWARD_POOL}에서 "
                "그만큼 뺀 몫을 받습니다.\n"
                "> 수락하면 배분이 **확정**됩니다. 다만 **누가 몇 번 퍼낼지는 여기서 정하지 않습니다** — "
                "말로 약속해도 구속력이 없습니다."
            ),
            inline=False,
        )
    elif round_phase == "execution" and pair_phase != "resolved":
        embed.add_field(
            name="🪣 실행",
            value=(
                "실제로 퍼낼 횟수를 고르세요. 선택은 상대에게 공개되지 않습니다.\n"
                "> **마감 전까지 몇 번이든 바꿀 수 있고, 마지막에 누른 값만 반영됩니다.** "
                "지금 내가 걸어둔 값은 버튼을 누를 때 나오는 안내로만 확인됩니다.\n"
                "> **누가 제출했는지도 표시되지 않습니다.** 결과는 실행 마감 시각에 한꺼번에 공개됩니다.\n"
                f"> 두 사람 합이 {BAIL_REQUIRED_SCOOPS}번 이상이면 배가 뜨고, "
                "내 점수는 **보상 몫 − 내가 퍼낸 횟수**입니다."
            ),
            inline=False,
        )

    embed.add_field(name="📜 규칙", value=bail_rules_text(), inline=False)
    if pair_phase != "resolved":
        close_at = (
            extra.get("execution_close_at")
            if round_phase == "execution"
            else extra.get("negotiation_close_at")
        )
        label = "실행 마감" if round_phase == "execution" else "협상 마감"
        note = (
            f"> 제한 시간은 **끝까지 씁니다.** 미제출은 {BAIL_FALLBACK_SCOOPS}번으로 자동 제출되고, "
            "마감 시각에 모든 방이 함께 정산됩니다."
            if round_phase == "execution"
            else "> 협상을 못 끝내면 결렬됩니다. 모든 방이 협상을 마치면 남은 시간을 기다리지 않고 넘어갑니다."
        )
        embed.add_field(
            name="⏳ 제한 시간",
            value=f"{label}: **{close_time_text(close_at)}**\n{note}",
            inline=False,
        )
    return embed


def build_bail_pair_result_embed(title: str, pair: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title=f"1:1 결과 · {title}",
        description=f"<@{pair['user_a_id']}> ↔ <@{pair['user_b_id']}>",
        color=BAIL_EMBED_COLOR,
    )
    result = pair.get("result") or {}
    total = result.get("total_scoops")
    embed.add_field(
        name="🪣 보상 배분과 실제 노동",
        value=bail_split_text(pair),
        inline=False,
    )
    embed.add_field(
        name="🏁 결과",
        value=(
            f"**{BAIL_OUTCOME_LABELS.get(str(pair.get('outcome')), '미완료')}** · "
            f"합계 **{total}번** / 필요 {BAIL_REQUIRED_SCOOPS}번"
            if total is not None
            else "**미완료**"
        ),
        inline=False,
    )
    embed.add_field(name="💰 정산", value=bail_pair_settlement_text(pair), inline=False)
    if pair.get("auto_completed"):
        embed.add_field(
            name="⏱️ 자동 제출",
            value="제한 시간이 끝나 미제출분이 합의한 몫(또는 절반)으로 자동 제출되었습니다.",
            inline=False,
        )
    return embed


def build_bail_play_embed(round_row: sqlite3.Row, extra: dict[str, Any]) -> discord.Embed:
    round_phase = str(extra.get("bail_phase") or "")
    embed = discord.Embed(
        title=str(round_row["title"]),
        description=(
            "각자 초대된 1:1 비공개 방에서 진행하세요. 협상 내용과 제출 횟수는 그 방 밖으로 나가지 않습니다."
        ),
        color=BAIL_EMBED_COLOR,
    )
    embed.add_field(
        name="🧭 현재 단계",
        value=(
            "**2단계 · 실행** — 각자 실제로 퍼낼 횟수를 비공개 제출\n"
            "> 제출 여부는 공개되지 않으며, 제한 시간이 끝나야 모든 방이 함께 정산됩니다."
            if round_phase == "execution"
            else "**1단계 · 협상** — 구조 보상금의 배분을 교대로 제안"
        ),
        inline=False,
    )
    lines = [
        f"**{index}.** <@{pair['user_a_id']}> ↔ <@{pair['user_b_id']}>\n"
        f"　└ {bail_pair_status_text(pair, extra)}"
        for index, pair in enumerate(extra.get("bail_pairs", []), start=1)
    ]
    embed.add_field(name="⚓ 1:1 진행 현황", value=field_text(lines), inline=False)

    unmatched_user_id = str(extra.get("unmatched_user_id") or "")
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
    emergency_user_id = str(extra.get("emergency_user_id") or "")
    if emergency_user_id:
        embed.add_field(name="🚨 긴급 투입", value=emergency_join_notice(emergency_user_id), inline=False)

    close_at = (
        extra.get("execution_close_at")
        if round_phase == "execution"
        else extra.get("negotiation_close_at")
    )
    embed.add_field(
        name="⏳ 제한 시간",
        value=(
            f"{'실행' if round_phase == 'execution' else '협상'} 마감: "
            f"**{close_time_text(close_at)}**\n"
            "> 모든 방이 먼저 끝나면 남은 시간을 기다리지 않고 다음 단계로 넘어갑니다."
        ),
        inline=False,
    )
    return embed


def bail_pair_view(
    round_id: int,
    pair: dict[str, Any],
    extra: dict[str, Any],
) -> discord.ui.View:
    """방 안 진행판에 붙는 버튼. 단계마다 버튼 구성이 완전히 달라서 phase로 갈라 만든다.

    버튼은 한 줄에 5개, 뷰 하나에 5줄이 상한이다. 그래서 한 줄에 4개씩 끊는다 —
    협상은 0~10 열한 개(4+4+3)에 수락/거절 줄까지 4줄, 실행은 0~6 일곱 개(4+3)로 2줄이다.
    """
    view = discord.ui.View(timeout=None)
    round_phase = str(extra.get("bail_phase") or "")
    pair_phase = str(pair.get("phase") or "")

    if pair_phase == "resolved":
        return view

    if round_phase == "negotiation" and pair_phase == "negotiating":
        awaiting_response = bool(pair.get("offer"))
        for index, value in enumerate(BAIL_REWARD_CHOICES):
            view.add_item(
                discord.ui.Button(
                    label=f"내가 {value}점",
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"eventbot:bailoffer:{round_id}:{value}",
                    disabled=awaiting_response,
                    row=index // 4,
                )
            )
        response_row = (len(BAIL_REWARD_CHOICES) + 3) // 4
        view.add_item(
            discord.ui.Button(
                label="수락",
                emoji="🤝",
                style=discord.ButtonStyle.success,
                custom_id=f"eventbot:bailrespond:{round_id}:accept",
                disabled=not awaiting_response,
                row=response_row,
            )
        )
        view.add_item(
            discord.ui.Button(
                label="거절",
                emoji="✋",
                style=discord.ButtonStyle.danger,
                custom_id=f"eventbot:bailrespond:{round_id}:reject",
                disabled=not awaiting_response,
                row=response_row,
            )
        )
        return view

    if round_phase == "execution":
        for index, value in enumerate(BAIL_SCOOP_CHOICES):
            view.add_item(
                discord.ui.Button(
                    label=f"{value}번 퍼내기",
                    style=discord.ButtonStyle.primary,
                    custom_id=f"eventbot:bailscoop:{round_id}:{value}",
                    row=index // 4,
                )
            )
        return view

    # 협상은 끝났지만 아직 실행 단계가 아닌 대기 상태. 누를 버튼이 없다.
    return view


def settle_bail(ctx: SettleContext) -> discord.Embed:
    """협상에서 확정된 배분(reward_split)과 실제 노동(퍼내기 합)을 나란히 정산한다.

    생존은 두 제출값의 합만으로 결정되고 보상은 협상에서 정한 배분을 따른다 —
    이 분리가 무임승차를 가능하게 하는 지점이다. 침몰 페널티는 보상 몫에 비례해서,
    많이 받기로 한 쪽이 더 크게 잃는다(대칭으로 바꾸면 배신 동기가 사라진다).
    """
    conn, round_row, extra, guild = ctx.conn, ctx.round_row, ctx.extra, ctx.guild
    # 생존은 제출된 퍼내기 합만으로 결정되고, 보상은 협상에서 확정된 배분을 따른다.
    bail_lines: list[str] = []
    # 실행 단계는 조기 정산을 하지 않으므로 정상 제출한 짝도 여기서 확정된다.
    # 자동 제출로 세는 것은 실제로 빈칸이 메워진 방뿐이다.
    bail_auto_count = 0
    for pair in extra.get("bail_pairs", []):
        if autocomplete_bail_pair(pair):
            bail_auto_count += 1

    for pair in extra.get("bail_pairs", []):
        user_a_id = str(pair["user_a_id"])
        user_b_id = str(pair["user_b_id"])
        pair_result = pair.get("result") or {}
        outcome = str(pair.get("outcome") or "")
        survived = outcome == "survived"
        for user_id, score_key, scoop_key, reward_key in (
            (user_a_id, "score_a", "scoop_a", "reward_a"),
            (user_b_id, "score_b", "scoop_b", "reward_b"),
        ):
            reason = (
                f"라운드 #{round_row['id']} 가라앉는 배 생존"
                f"(보상 {int(pair_result[reward_key])}점 − {int(pair[scoop_key])}번 퍼내기)"
                if survived
                else (
                    f"라운드 #{round_row['id']} 가라앉는 배 침몰"
                    f"(보상 몫 {int(pair_result[reward_key])}점)"
                )
            )
            add_score(
                conn,
                user_id,
                member_name(guild, user_id),
                int(pair_result[score_key]),
                reason,
                round_row["id"],
            )
        auto_mark = " · 자동 제출" if pair.get("auto_completed") else ""
        bail_lines.append(
            f"**{len(bail_lines) + 1}.** <@{user_a_id}> ↔ <@{user_b_id}>\n"
            f"　└ **{BAIL_OUTCOME_LABELS.get(outcome, '미완료')}** "
            f"(합계 {int(pair_result['total_scoops'])}/{BAIL_REQUIRED_SCOOPS}번) · "
            f"{bail_pair_settlement_text(pair)}{auto_mark}\n"
            f"　└ {bail_split_text(pair)}"
        )

    extra.update({"bail_phase": "scored", "bail_scored_at": now_text()})
    conn.execute(
        "UPDATE rounds SET extra_json = ? WHERE id = ?",
        (json.dumps(extra, ensure_ascii=False), round_row["id"]),
    )
    embed = discord.Embed(
        title=f"결과 · {round_row['title']}",
        description=round_row["title"],
        color=BAIL_EMBED_COLOR,
    )
    embed.add_field(name="⚓ 짝별 결과", value=field_text(bail_lines), inline=False)
    unmatched_user_id = str(extra.get("unmatched_user_id") or "")
    if unmatched_user_id:
        embed.add_field(name="⏳ 매칭 대기", value=f"<@{unmatched_user_id}> (0점)", inline=False)
    if bail_auto_count:
        embed.add_field(
            name="⏱️ 자동 제출",
            value=f"제한 시간이 끝나 **{bail_auto_count}개 방**의 미제출분이 자동 제출되었습니다.",
            inline=False,
        )
    return embed

register_settler("bail", settle_bail)
