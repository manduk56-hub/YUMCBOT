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

**퍼내기는 번호를 미리 선언하는 게 아니라 실시간 클릭이다.** 방 안 공동 진행 상황
메시지의 `🪣 퍼내기` 버튼을 누른 횟수가 그대로 그 사람의 누적치로 쌓이고
(`apply_bail_scoop_click`), 되돌릴 수 없는 실제 행동이다. 두 사람 누적치의 합이
`BAIL_REQUIRED_SCOOPS`에 도달하면 **그 즉시 그 짝만 조기 정산되어** 방에 결과가
공개되고 방이 잠긴다(`handle_bail_scoop`) — 협상 단계의 방 전체 조기 전이와 달리,
여기서는 짝(방) 단위로 끝난다. 못 채운 방은 제한 시간을 끝까지 쓰고 마감 시점의
`close_round_record`/`autocomplete_bail_pair()`가 확정한다. 마지막 짝까지 정산되면
(`bail_executions_complete`) 라운드 전체도 마감을 기다리지 않고 즉시 끝난다
(`finish_bail_execution_round`).

**둘이 합쳐 몇 번 퍼냈는지는 실시간으로 공개되지만, 누가 몇 번 퍼냈는지는 정산 전까지
감춘다.** 공동 진행 상황 메시지는 문구가 아니라 단계별 이미지를 보여주는 임베드다
(`build_bail_progress_embed`). 개인별 분담이 새면 무임승차 여부가 정산 전에 드러나
배신 유인이 무너지므로, 이 부분은 지킨다.

**조기 정산은 '이 짝의 승패가 확정되고 방에 공개되는 시점'만 앞당길 뿐, 실제 점수
지급은 여전히 라운드 마감에 한 번에 일어난다.** 점수 반영 경로를 둘로 나누면 이중
지급 위험이 생기기 때문이다.

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
    BAIL_NEGOTIATION_MAX_OFFERS,
    BAIL_OUTCOME_LABELS,
    BAIL_PROGRESS_STAGES,
    BAIL_PROGRESS_SUCCESS_IMAGE,
    BAIL_PROGRESS_SUNK_CAPTION,
    BAIL_PROGRESS_SUNK_IMAGE,
    BAIL_PROGRESS_UPDATE_INTERVAL,
    BAIL_REQUIRED_SCOOPS,
    BAIL_REWARD_CHOICES,
    BAIL_REWARD_POOL,
    BAIL_SCOOP_COST,
    BAIL_SINK_PENALTY_EVEN,
    BAIL_SINK_PENALTY_HIGH,
    BAIL_SINK_PENALTY_LOW,
    BASE_DIR,
)
from ..db import (
    db,
    now_text,
)
from ..activity import (
    log_activity,
)
from ..discussion import (
    archive_round_discussion_thread,
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
    update_trust_main_message,
    warn_failed_pair_threads,
)
from .registry import (
    AutoCloseContext,
    CreateContext,
    CreateResult,
    SettleContext,
    register_auto_close_handler,
    register_creator,
    register_settler,
)

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
        f"**1** ─ 배가 뜨려면 두 사람이 합쳐 **{BAIL_REQUIRED_SCOOPS}번**을 퍼내야 하며, "
        "각자 방에 뜨는 **`🪣 퍼내기`** 버튼을 눌러 실시간으로 채웁니다.\n"
        "**2** ─ 둘이 합쳐 몇 번 퍼냈는지는 공동 진행 상황 메시지로 보이지만, "
        "**누가 몇 번 퍼냈는지는 끝나기 전까지 보이지 않습니다.**\n"
        f"**3** ─ 필요한 횟수를 채우면 **그 즉시** 배가 뜨고 결과가 공개됩니다. "
        "못 채운 채 실행 제한 시간이 끝나면 침몰합니다.\n"
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


def apply_bail_scoop_click(pair: dict[str, Any], user_id: str) -> int:
    """퍼내기 버튼 클릭 1회 = 그 사람의 누적 횟수 +1. 되돌릴 수 없는 실제 행동이라
    번호를 선언하던 예전과 달리 덮어쓰기 개념이 없다. 갱신된 **본인 누적 횟수**를 돌려준다.
    """
    key = "scoop_a" if user_id == str(pair["user_a_id"]) else "scoop_b"
    pair[key] = int(pair.get(key) or 0) + 1
    return pair[key]


def bail_progress_stage_image(total: int) -> str:
    """공동 진행 상황 이미지 파일명. 필요 횟수에 도달하면 성공 이미지를 돌려준다."""
    if total >= BAIL_REQUIRED_SCOOPS:
        return BAIL_PROGRESS_SUCCESS_IMAGE
    image = BAIL_PROGRESS_STAGES[0][1]
    for threshold, filename in BAIL_PROGRESS_STAGES:
        if total >= threshold:
            image = filename
    return image


def bail_progress_stage_index(total: int) -> int | None:
    """지금 보이는 이미지가 `BAIL_PROGRESS_STAGES`의 몇 번째(1부터)인지. 필요 횟수에 도달해
    성공 이미지로 바뀌었으면(더 이상 '단계'가 아니라 결과이므로) None을 돌려준다."""
    if total >= BAIL_REQUIRED_SCOOPS:
        return None
    index = 1
    for position, (threshold, _filename) in enumerate(BAIL_PROGRESS_STAGES, start=1):
        if total >= threshold:
            index = position
    return index


def bail_progress_image_filename(pair: dict[str, Any]) -> str:
    """짝 하나의 공동 진행 상황 이미지 파일명. 정산된 뒤에는 최종 결과 이미지로 고정된다."""
    if str(pair.get("phase")) == "resolved":
        if str(pair.get("outcome")) == "survived":
            return BAIL_PROGRESS_SUCCESS_IMAGE
        return BAIL_PROGRESS_SUNK_IMAGE
    total = int(pair.get("scoop_a") or 0) + int(pair.get("scoop_b") or 0)
    return bail_progress_stage_image(total)


def build_bail_progress_embed(pair: dict[str, Any]) -> discord.Embed:
    """공동 진행 상황 메시지 임베드.

    문구 대신 단계별 이미지(0/2/4/6/fail)를 크게 보여준다 — 물이 차오르는 급박함이 그림
    한 장으로 한눈에 들어와야 한다. 단계(1/2/3)는 이미지가 아니라 참고용이라 각주로만 작게
    붙인다. 이미지는 URL이 아니라 첨부파일이므로, 호출자가 `bail_progress_image_filename`으로
    같은 파일명을 `discord.File`로 함께 보내야 실제로 뜬다(`write_bail_progress_message`).
    """
    embed = discord.Embed(color=BAIL_EMBED_COLOR)
    embed.set_image(url=f"attachment://{bail_progress_image_filename(pair)}")
    if str(pair.get("phase")) != "resolved":
        total = int(pair.get("scoop_a") or 0) + int(pair.get("scoop_b") or 0)
        stage_index = bail_progress_stage_index(total)
        if stage_index is not None:
            embed.set_footer(text=f"단계 {stage_index}/{len(BAIL_PROGRESS_STAGES)}")
    return embed


def build_bail_progress_embeds(pair: dict[str, Any]) -> list[discord.Embed]:
    """공동 진행 상황 메시지에 실제로 붙일 임베드 목록.

    침몰이면 사진 밑에 큰 문구를 한 줄 더 붙인다 — 임베드 하나로는 이미지 아래에 텍스트를
    둘 수 없어서(설명은 이미지보다 위에 뜬다), 문구만 담은 두 번째 임베드를 이어 붙여
    이미지 바로 아래에 오게 한다.
    """
    embeds = [build_bail_progress_embed(pair)]
    if str(pair.get("phase")) == "resolved" and str(pair.get("outcome")) != "survived":
        embeds.append(
            discord.Embed(description=f"# {BAIL_PROGRESS_SUNK_CAPTION}", color=BAIL_EMBED_COLOR)
        )
    return embeds


def bail_negotiations_complete(extra: dict[str, Any]) -> bool:
    """모든 짝이 협상을 마쳤는가 — 협상 → 실행 조기 전이 판정에 쓴다."""
    pairs = extra.get("bail_pairs", [])
    return bool(pairs) and all(
        str(pair.get("phase")) in {"submitting", "resolved"} for pair in pairs
    )


def bail_executions_complete(extra: dict[str, Any]) -> bool:
    """모든 짝이 정산까지 끝났는가 — 실행 단계 조기 마감 판정에 쓴다.

    협상과 달리 실행은 짝 단위로 조기 정산되므로(`handle_bail_scoop`), 이 판정은 라운드
    전체를 마감할지를 결정한다 — 마지막 짝까지 끝나면 남은 시간을 기다리지 않는다.
    """
    pairs = extra.get("bail_pairs", [])
    return bool(pairs) and all(str(pair.get("phase")) == "resolved" for pair in pairs)


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
    """마감 시점에 필요 횟수를 못 채우고 남은 짝을 자동으로 확정한다. 실제로 자동 처리한 게 있으면 True.

    퍼내기는 이제 실시간 클릭이라 '미제출'이라는 상태가 없다 — 안 누르면 0일 뿐이고 이미
    유효한 값이다. 그래서 예전처럼 절반으로 채우는 자동 제출은 하지 않는다. 여기서 자동 처리할
    대상은 응답 없이 남은 협상뿐이다: 대기 중인 제안이나 끝나지 않은 협상은 결렬로 처리해
    감액된 보상금을 균등 분배받는다.

    필요 횟수에 도달해 이미 조기 정산된 짝(`resolve_bail_pair`가 먼저 불린 경우)은 여기서 다시
    건드리지 않는다 — `auto_completed`를 무조건 세우면 모든 방이 '자동 처리'로 표시되므로,
    실제로 협상을 결렬 처리했을 때만 세운다.
    """
    if str(pair.get("phase")) == "resolved":
        return False

    # 응답 없이 사라지는 제안이 남아 있거나 협상이 안 끝난 채로 마감된 것도 자동 처리다.
    auto_filled = bool(pair.get("offer")) or str(pair.get("phase")) == "negotiating"
    pair["offer"] = None
    for key in ("scoop_a", "scoop_b"):
        if pair.get(key) is None:
            pair[key] = 0
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
    """짝 하나의 정산 문구. 라운드 요약과 방별 결과 공개가 같은 문구를 쓴다.

    이 짝을 소개하는 줄(`<@user_a> ↔ <@user_b>`)이 항상 먼저 나오므로, 여기서는 멘션을
    반복하지 않고 그 줄과 같은 좌/우 순서로 값만 "/"로 나열한다 — 이름을 매 줄 반복하면
    짝이 여럿인 요약에서 가독성이 떨어진다.
    """
    result = pair.get("result")
    if not result:
        return "점수 변동 없음"
    return (
        f"{bail_score_text(int(result['score_a']))} / {bail_score_text(int(result['score_b']))}"
    )


def bail_split_text(pair: dict[str, Any]) -> str:
    """보상 배분과 실제 퍼낸 횟수를 나란히 보여준다. `bail_pair_settlement_text`와 같은 이유로
    멘션을 반복하지 않고 좌(user_a)/우(user_b) 값만 "/"로 나열한다.

    돈은 많이 받고 일은 덜 한 사람의 값에 ⚠️를 붙인다. 무임승차가 공개 기록으로 남는 것이
    이벤트 전체에 걸친 평판 장치다.
    """
    split = pair.get("reward_split")
    scoop_a = pair.get("scoop_a")
    scoop_b = pair.get("scoop_b")
    share_a = bail_reward_share(pair, "user_a")
    share_b = bail_reward_share(pair, "user_b")

    if split:
        lines = [f"🤝 보상 배분 {share_a}점 / {share_b}점"]
    else:
        lines = [f"🤝 보상 배분 **결렬** → {share_a}점 / {share_b}점"]

    if scoop_a is not None and scoop_b is not None:
        scoop_a, scoop_b = int(scoop_a), int(scoop_b)
        # 보상은 더 받으면서 노동은 덜 한 쪽에만 표시한다.
        mark_a = " ⚠️" if share_a > share_b and scoop_a < scoop_b else ""
        mark_b = " ⚠️" if share_b > share_a and scoop_b < scoop_a else ""
        lines.append(f"🪣 실제 퍼내기 {scoop_a}번{mark_a} / {scoop_b}번{mark_b}")
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
            # 퍼내기는 이제 실시간 클릭 누적이라 0부터 시작한다 (None은 "아직 실행 전"일 뿐이다).
            pair["scoop_a"] = 0
            pair["scoop_b"] = 0

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


async def advance_bail_registration(round_id: int) -> dict[str, Any] | None:
    """참여자 모집을 마감하고 매칭·협상 단계 시작까지 한 번에 진행한다. 실패하면 `None`."""
    transition = start_bail_negotiation(round_id)
    if not transition:
        return None
    await create_bail_pair_threads(transition)
    await update_trust_main_message(transition)
    return transition


async def advance_bail_negotiation(round_id: int) -> dict[str, Any] | None:
    """협상을 마감하고 실행 단계 시작까지 한 번에 진행한다. 실패하면 `None`.

    협상 → 실행 전이는 버튼 구성이 바뀌므로 모든 방의 진행판을 다시 그린다
    (누른 방만 갱신하는 모래성과 다른 점이다).
    """
    transition = start_bail_execution(round_id)
    if not transition:
        return None
    await refresh_all_bail_pair_boards(round_id)
    await refresh_all_bail_progress_boards(round_id)
    await refresh_main_bail_board(round_id)
    return transition


async def handle_bail_auto_close(ctx: AutoCloseContext) -> bool:
    """모집·협상 단계 마감 타이머만 처리한다. 실행 단계는 제한 시간을 끝까지 쓴다."""
    bail_phase = round_extra(ctx.row).get("bail_phase")
    if bail_phase == "registration":
        transition = await advance_bail_registration(ctx.round_id)
        if transition and transition["state"] == "negotiation":
            pairing.board_client().schedule_auto_close(ctx.round_id, float(transition["close_at"]))
        return True
    if bail_phase == "negotiation":
        transition = await advance_bail_negotiation(ctx.round_id)
        if not transition:
            return True
        pairing.board_client().schedule_auto_close(ctx.round_id, float(transition["close_at"]))
        if hasattr(ctx.channel, "send"):
            try:
                await ctx.channel.send(
                    "🪣 협상 시간이 끝났습니다. **실행 단계**를 시작합니다 — "
                    "각자 실제로 퍼낼 횟수를 비공개로 제출하세요. "
                    f"(마감 {close_time_text(transition['close_at'])})"
                )
            except discord.HTTPException:
                pass
        return True
    return False


register_auto_close_handler("bail", handle_bail_auto_close)


def bail_pair_intro(user_a_id: str, user_b_id: str) -> str:
    return (
        f"<@{user_a_id}>님과 <@{user_b_id}>님의 **1:1 구조 협상방**입니다.\n"
        f"> **1** ─ 구조 보상금 {BAIL_REWARD_POOL}점을 **어떻게 나눌지** 협상하세요. "
        "수락하면 확정됩니다.\n"
        f"> **2** ─ 배가 뜨려면 둘이 합쳐 {BAIL_REQUIRED_SCOOPS}번 퍼내야 하는데, "
        "**누가 몇 번 퍼낼지는 약속하지 않습니다** — 각자 버튼을 눌러 실시간으로 채우고, "
        "다 채우면 그 자리에서 바로 정산됩니다."
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


def save_bail_pair_progress_message_id(round_id: int, pair: dict[str, Any], message_id: str) -> None:
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row:
            return
        extra = round_extra(round_row)
        target = find_bail_pair(extra, str(pair["user_a_id"]))
        if not target:
            return
        target["progress_message_id"] = message_id
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )


async def write_bail_progress_message(
    round_id: int,
    thread: discord.Thread,
    pair: dict[str, Any],
) -> None:
    """공동 진행 상황 메시지를 실제로 fetch/edit하거나 새로 보낸다.

    호출자가 이미 `pairing.board_client().sand_board_lock(thread.id)`를 잡고 있어야 한다
    (락은 재진입이 안 되므로 여기서 다시 잡지 않는다) — `handle_bail_scoop`가 상태 변경과
    같은 락 구간에서 바로 호출한다.
    """
    embeds = build_bail_progress_embeds(pair)
    view = bail_progress_view(round_id, pair)
    image_filename = bail_progress_image_filename(pair)
    image_path = BASE_DIR / "assets" / image_filename

    message_id = str(pair.get("progress_message_id") or "")
    if message_id:
        try:
            message = await thread.fetch_message(int(message_id))
            # attachments로 첨부 목록을 통째로 바꿔야 이전 단계 이미지가 새 이미지로 실제 교체된다.
            # content=None으로 예전에 plain text로 보냈을 수도 있는 메시지의 잔여 텍스트도 지운다.
            await message.edit(
                content=None,
                embeds=embeds,
                view=view,
                attachments=[discord.File(image_path, filename=image_filename)],
            )
            return
        except (discord.NotFound, discord.HTTPException, ValueError):
            pass

    try:
        new_message = await thread.send(
            embeds=embeds,
            view=view,
            file=discord.File(image_path, filename=image_filename),
        )
    except discord.HTTPException:
        return
    save_bail_pair_progress_message_id(round_id, pair, str(new_message.id))


async def redraw_bail_progress_board(
    round_id: int,
    thread: discord.Thread,
    pair_user_id: str,
) -> None:
    """실행 단계 시작 시 짝별 공동 진행 상황 메시지를 만든다(이미 있으면 제자리에서 고쳐 쓴다).

    두 사람이 동시에 클릭할 수 있으므로 락을 잡고 DB에서 현재 상태를 다시 읽는다.
    (락 dict는 `redraw_bail_pair_board`/모래성과 공유한다 — 키가 thread_id라 서로 충돌하지 않는다.)
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
        await write_bail_progress_message(round_id, thread, pair)


async def refresh_all_bail_progress_boards(round_id: int) -> None:
    """실행 단계로 전이할 때 모든 방에 공동 진행 상황 메시지를 새로 띄운다."""
    for pair, thread in await bail_pair_threads(round_id):
        await redraw_bail_progress_board(round_id, thread, str(pair["user_a_id"]))


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
            # 필요 횟수를 채워 조기 정산된 방은 그 자리에서 이미 결과를 공개하고 잠갔다
            # (handle_bail_scoop). 여기서 또 보내면 결과가 두 번 뜨므로 건너뛴다.
            if pair.get("result_published"):
                continue
            # 마감까지 못 채운 방은 여기서 처음 확정되므로, 결과를 보내기 전에 공동 진행
            # 상황 메시지를 최종 문구(성공/침몰, 버튼 제거)로 먼저 갱신한다.
            await write_bail_progress_message(round_id, thread, pair)
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
            await refresh_all_bail_progress_boards(round_id)
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

    # 아직 이 방(또는 다른 방)의 협상이 안 끝났다 — 진행판만 갱신하고 기다린다.
    # (실행 단계의 퍼내기는 이 함수를 거치지 않는다. `handle_bail_scoop`가 따로 처리한다.)
    await refresh_bail_pair_board(interaction, round_id)
    await refresh_main_bail_board(round_id)


async def finish_bail_execution_round(round_id: int, guild: discord.Guild | None) -> None:
    """모든 짝이 정산을 마쳤으면 마감 시각을 기다리지 않고 라운드 전체를 즉시 마감한다.

    `auto_close_round`가 자연 마감 때 하는 마무리 절차와 동일한 순서를 따른다. 정산은
    `close_round_record`가 해야 하는데 그쪽이 레지스트리를 거쳐 다시 이 모듈을 부르므로
    직접 부를 수 없다 — `runtime.round_services()`를 거친다(야구의 3구 조기 종료와 같은 패턴).
    예약된 자동 마감 타이머가 있으면 먼저 취소해 불필요한 카운트다운을 막는다.
    """
    runtime.auto_close_scheduler().cancel_auto_close(round_id)

    services = runtime.round_services()
    result = services.close_round(round_id, guild, "전원 완료")
    if not result:
        return

    channel_id, embed = result
    await publish_bail_pair_results(round_id)
    with db() as conn:
        row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
    if not row:
        return
    await archive_round_discussion_thread(row, reason="라운드 종료 후 영구 보관")
    channel = await runtime.resolve_text_channel(channel_id)
    if channel is not None:
        try:
            await channel.send(embed=embed, view=services.next_mini_game_view(round_id))
        except discord.HTTPException:
            pass
    await services.publish_event_summary(row["event_session_id"])


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


async def handle_bail_scoop(interaction: discord.Interaction, round_id: int) -> None:
    """퍼내기 버튼 클릭 1회 = 이 사람의 누적 횟수 +1.

    번호를 미리 선언하던 예전과 달리 실시간으로 쌓이고, 두 사람 합이 필요 횟수에 도달하면
    **그 자리에서 바로** 이 짝을 정산해 방에 결과를 공개한다. 다만 실제 점수 지급(`add_score`)은
    지금도 라운드 마감(`close_round_record`) 시점에 다른 짝들과 함께 한 번에 일어난다 — 조기
    정산은 '이 짝의 승패가 확정되고 방에 공개되는 시점'만 앞당길 뿐이다. 점수 반영 경로를
    둘로 나누면 이중 지급 위험이 생기므로, 여기서는 `resolve_bail_pair`로 결과만 확정하고
    `result_published`만 남겨 `close_round_record`/`publish_bail_pair_results`가 그대로 이어받게 한다.

    두 사람이 거의 동시에 눌러도 누적이 씹히지 않도록, 그리고 정산 직후의 진행 상황/결과
    메시지가 겹쳐 쓰이지 않도록 클릭 처리부터 결과 공개까지를
    `pairing.board_client().sand_board_lock`으로 직렬화한다.
    """
    user_id = str(interaction.user.id)
    thread = interaction.channel
    if not isinstance(thread, discord.Thread):
        await interaction.response.send_message(
            "자신에게 배정된 1:1 방에서만 진행할 수 있습니다.", ephemeral=True
        )
        return

    async with pairing.board_client().sand_board_lock(thread.id):
        with db() as conn:
            loaded = load_open_bail_pair(conn, round_id, user_id, interaction.channel_id)
            if isinstance(loaded, str):
                await interaction.response.send_message(loaded, ephemeral=True)
                return
            round_row, extra, pair = loaded

            if extra.get("bail_phase") != "execution":
                await interaction.response.send_message(
                    "아직 실행 단계가 아닙니다. 협상이 모두 끝나면 퍼낼 수 있습니다.",
                    ephemeral=True,
                )
                return
            if str(pair.get("phase")) == "resolved":
                await interaction.response.send_message("이 1:1 게임은 이미 끝났습니다.", ephemeral=True)
                return

            my_count = apply_bail_scoop_click(pair, user_id)
            total = int(pair.get("scoop_a") or 0) + int(pair.get("scoop_b") or 0)
            just_resolved = total >= BAIL_REQUIRED_SCOOPS

            log_activity(
                conn,
                "bail",
                "scoop_click",
                event_session_id=round_row["event_session_id"],
                round_id=round_id,
                actor_user_id=user_id,
                payload={
                    "game_key": "bail",
                    "my_total": my_count,
                    "combined_total": total,
                    "required": BAIL_REQUIRED_SCOOPS,
                },
            )
            if just_resolved:
                resolve_bail_pair(pair)
                pair["result_published"] = True
                log_activity(
                    conn,
                    "bail",
                    "pair_resolved_early",
                    event_session_id=round_row["event_session_id"],
                    round_id=round_id,
                    payload={
                        "game_key": "bail",
                        "outcome": pair.get("outcome"),
                        "total_scoops": total,
                    },
                )
            conn.execute(
                "UPDATE rounds SET extra_json = ? WHERE id = ?",
                (json.dumps(extra, ensure_ascii=False), round_id),
            )
            title = str(round_row["title"])

        # 몇 번째 퍼내기인지는 본인에게만 안내한다 — 상대는 합계 진행 상황판으로만 짐작할 수 있다.
        await interaction.response.send_message(
            f"**{my_count}번째** 퍼내기입니다.", ephemeral=True
        )

        # 공동 진행 상황 메시지는 합계가 갱신 주기를 지날 때만 고쳐 쓴다.
        # 다만 정산된 순간만은 지체 없이 바로 최종 문구로 반영한다.
        if just_resolved or total % BAIL_PROGRESS_UPDATE_INTERVAL == 0:
            await write_bail_progress_message(round_id, thread, pair)

        if just_resolved:
            try:
                await thread.send(embed=build_bail_pair_result_embed(title, pair))
                # 이 방의 게임이 끝나 버튼이 필요 없어졌으니 여기서 바로 잠근다.
                await thread.edit(archived=True, locked=True, reason="가라앉는 배 결과 공개 완료")
            except (discord.Forbidden, discord.HTTPException):
                pass
            # 규칙까지 포함한 방 진행판은 이 시점엔 더 볼 이유가 없다 — 결과는 위 임베드로,
            # 최종 집계는 어차피 메인 채널 요약으로 확인하므로 여기서 다시 그리지 않는다.

    if just_resolved:
        if bail_executions_complete(extra):
            # 모든 짝이 정산을 마쳤다 — 남은 시간을 기다리지 않고 라운드 전체를 즉시 마감한다.
            await finish_bail_execution_round(round_id, interaction.guild)
        else:
            await refresh_main_bail_board(round_id)


def build_bail_registration_embed(round_id: int, title: str, registration_minutes: int) -> discord.Embed:
    embed = discord.Embed(title=title, description=title, color=BAIL_EMBED_COLOR)
    embed.add_field(name="🎮 진행 게임", value="가라앉는 배", inline=False)
    embed.add_field(name="📜 메인 규칙", value=bail_rules_text(), inline=False)
    return embed


def create_bail_round(ctx: CreateContext) -> CreateResult:
    """rounds.type CHECK 제약을 못 고쳐서 type은 sand를 재사용하고 mode로 갈라 쓴다."""
    ctx.extra.update(
        {
            "mode": "bail",
            "bail_phase": "registration",
            "registration_minutes": ctx.duration_minutes,
            "registration_close_at": ctx.close_at,
            "negotiation_seconds": int(
                ctx.negotiation_minutes or BAIL_DEFAULT_NEGOTIATION_MINUTES
            ) * 60,
            "execution_seconds": int(ctx.execution_minutes or BAIL_DEFAULT_EXECUTION_MINUTES) * 60,
        }
    )
    cursor = ctx.conn.execute(
        """
        INSERT INTO rounds
            (type, title, question, status, channel_id, extra_json, created_at)
        VALUES ('sand', ?, ?, 'open', ?, ?, ?)
        """,
        (
            ctx.title,
            ctx.title,
            ctx.channel_id,
            json.dumps(ctx.extra, ensure_ascii=False),
            now_text(),
        ),
    )
    round_id = int(cursor.lastrowid)
    return CreateResult(
        round_id=round_id,
        embed=build_bail_registration_embed(round_id, ctx.title, ctx.duration_minutes),
        view=discord.utils.MISSING,
    )


register_creator("bail", create_bail_round)


def bail_pair_status_text(pair: dict[str, Any], extra: dict[str, Any]) -> str:
    """짝 하나의 진행 상황. 누가 몇 번 퍼냈는지(개인별 분담)는 정산 전까지 드러내지 않는다."""
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
    # 둘이 합쳐 몇 번 퍼냈는지는 방 안 공동 진행 상황 메시지로 이미 보이므로 여기서 감출 필요는
    # 없지만, 개인별로 몫을 얼마나 채웠는지는 정산 전까지 감춘다 — 무임승차 여부가 새면 안 된다.
    return "물 퍼내는 중 · 아래 진행 상황 메시지에서 진행도를 확인하세요"


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
                "아래 **별도로 올라오는 공동 진행 상황 메시지**의 `🪣 퍼내기` 버튼을 눌러 물을 퍼내세요.\n"
                f"> 버튼을 누른 횟수가 그대로 쌓이고, 두 사람 합이 {BAIL_REQUIRED_SCOOPS}번에 도달하면 "
                "**그 즉시 정산**되어 결과가 공개됩니다.\n"
                "> 지금까지 내가 몇 번 퍼냈는지는 버튼을 누를 때 나오는 안내로만 확인됩니다 — "
                "**누가 몇 번 퍼냈는지는 끝나기 전까지 상대에게 보이지 않습니다.**\n"
                "> 내 점수는 **보상 몫 − 내가 퍼낸 횟수**입니다."
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
            "> 필요한 만큼 채우면 **그 즉시 정산**되어 결과가 뜨고, 다 못 채운 채 제한 시간이 끝나면 "
            "**침몰**합니다."
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
            "**2단계 · 실행** — 각자 방의 버튼으로 실시간 퍼내기\n"
            "> 두 사람 합이 필요 횟수에 도달한 방은 그 즉시 정산되고, 그렇지 않으면 "
            "제한 시간에 함께 마감됩니다."
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
    """방 안 진행판(규칙 안내)에 붙는 버튼. 단계마다 버튼 구성이 완전히 달라서 phase로 갈라 만든다.

    버튼은 한 줄에 5개, 뷰 하나에 5줄이 상한이다. 그래서 한 줄에 4개씩 끊는다 —
    협상은 0~10 열한 개(4+4+3)에 수락/거절 줄까지 4줄이다.
    실행 단계의 `🪣 퍼내기` 버튼은 여기가 아니라 별도의 공동 진행 상황 메시지
    (`bail_progress_view`)에 있다 — 이 진행판은 그 단계에서 버튼 없이 규칙만 보여준다.
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

    # 실행 단계(퍼내기)든, 협상은 끝났지만 아직 실행 전인 대기 상태든 이 진행판에는 버튼이 없다.
    return view


def bail_progress_view(round_id: int, pair: dict[str, Any]) -> discord.ui.View:
    """공동 진행 상황 메시지에 붙는 단일 버튼. 정산되면 버튼이 사라진다."""
    view = discord.ui.View(timeout=None)
    if str(pair.get("phase")) != "resolved":
        view.add_item(
            discord.ui.Button(
                label="🪣 퍼내기",
                style=discord.ButtonStyle.primary,
                custom_id=f"eventbot:bailscoop:{round_id}",
            )
        )
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
        # 아래 상세 줄들은 이름을 반복하지 않는다 — 첫 줄의 좌(user_a)/우(user_b)
        # 순서를 그대로 따르는 "/" 표기라, 짝이 여럿이어도 한눈에 읽힌다.
        detail_lines = "\n".join(
            f"　{line}" for line in bail_split_text(pair).splitlines()
        )
        bail_lines.append(
            f"**{len(bail_lines) + 1}.** <@{user_a_id}> ↔ <@{user_b_id}>\n"
            f"　⚓ **{BAIL_OUTCOME_LABELS.get(outcome, '미완료')}** "
            f"(합계 {int(pair_result['total_scoops'])}/{BAIL_REQUIRED_SCOOPS}번) · "
            f"💰 정산 {bail_pair_settlement_text(pair)}{auto_mark}\n"
            f"{detail_lines}"
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
