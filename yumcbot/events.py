"""이벤트/라운드 생애주기 — `/오픈`·`/start`·`/미니게임`·`/라운드마감`·`/이벤트종료`·`/이벤트리스트`.

`EventBot` 인스턴스가 필요한 자리(타이머 취소·재예약, 임의 채널 조회)는 전부 `runtime`을
거친다 — bot.py를 직접 import 할 수 없어서다(bot.py가 이 모듈을 부른다).

`close_round_record`가 여기 있는 이유: `/라운드마감`이 직접 쓰기도 하고, `EventBot.auto_close_round`
(자동 마감 타이머)도 쓴다. 두 호출부 모두를 만족하려면 어느 한쪽에 종속되지 않는 곳에 둬야 하는데,
이 모듈은 게임 모듈들의 정산 훅(`registry.settler_for`)만 알면 되고 EventBot을 몰라도 되므로 여기가
맞다. bot.py는 `auto_close_round`에서 쓰려고 이 모듈에서 다시 가져간다.
"""

from __future__ import annotations

import json
import time
from typing import Any

import discord

from . import runtime
from .config import (
    BAIL_DEFAULT_EXECUTION_MINUTES,
    BAIL_DEFAULT_NEGOTIATION_MINUTES,
    BASEBALL_IMAGE_FILENAME,
    BASEBALL_IMAGE_PATH,
    BOT_VERSION,
    EVENT_STATUS_LABELS,
    TRUST_REGISTRATION_DEFAULT_MINUTES,
)
from .db import (
    db,
    now_text,
)
from .activity import (
    game_settings_snapshot,
    log_activity,
)
from .rounds import (
    get_open_round,
    main_game_phase,
    round_extra,
    round_game_key,
)
from .ui import (
    close_time_text,
    field_text,
)
from .discussion import (
    archive_round_discussion_thread,
    build_anonymous_guide_embed,
    thread_discussion_view,
)
from .games.registry import (
    GAME_NAMES,
    MAIN_GAME_TYPES,
    MINI_GAME_TYPES,
    CreateContext,
    RoundCreationError,
    SettleContext,
    creator_for,
    settler_for,
)
from .games.pairing import (
    ensure_pair_thread_permissions,
)
from .games import bail, balance, baseball, sand, trust
from .games.baseball import (
    baseball_control_content,
    delete_baseball_control_message,
)
from .games.trust import (
    publish_trust_pair_results,
)
from .games.bail import (
    publish_bail_pair_results,
)
from .games.sand import (
    publish_sand_pair_results,
)


def build_thread_notice_embed(round_id: int, thread: discord.Thread, title: str, guide: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=f"{thread.mention}\n{guide}",
        color=discord.Color.gold(),
    )
    embed.add_field(name="👉 참여 방법", value="위 스레드를 눌러 의견을 남겨주세요.", inline=False)
    return embed


def get_current_event(conn) -> Any:
    return conn.execute(
        """
        SELECT * FROM event_sessions
        WHERE status IN ('recruiting', 'active')
        ORDER BY id DESC LIMIT 1
        """
    ).fetchone()


def force_close_current_event(conn) -> tuple[Any, list[Any]] | None:
    event_row = get_current_event(conn)
    if not event_row:
        return None

    round_rows = conn.execute(
        """
        SELECT * FROM rounds
        WHERE event_session_id = ?
        ORDER BY id ASC
        """,
        (event_row["id"],),
    ).fetchall()
    closed_at = now_text()

    for round_row in round_rows:
        extra = round_extra(round_row)
        is_open = round_row["status"] == "open"
        is_unresolved_cutting = (
            round_row["type"] == "cutting" and "cutting_result_level" not in extra
        )
        if not is_open and not is_unresolved_cutting:
            continue
        extra.update(
            {
                "force_closed": True,
                "force_closed_reason": "development_command",
            }
        )
        if is_open and round_row["type"] == "trust":
            extra["trust_phase"] = "cancelled"
        elif is_open and round_row["type"] == "sand":
            extra["sand_phase"] = "cancelled"
        conn.execute(
            """
            UPDATE rounds
            SET status = 'closed', closed_at = COALESCE(closed_at, ?), extra_json = ?
            WHERE id = ?
            """,
            (closed_at, json.dumps(extra, ensure_ascii=False), round_row["id"]),
        )

    conn.execute(
        """
        UPDATE event_sessions
        SET status = 'closed', closed_at = ?
        WHERE id = ?
        """,
        (closed_at, event_row["id"]),
    )
    # 정상 완료와 강제 마감은 event_sessions에서 똑같이 'closed'로만 보인다.
    # 어느 쪽으로 끝났는지는 이 로그에만 남는다.
    log_activity(
        conn,
        "event",
        "close",
        event_session_id=int(event_row["id"]),
        payload={
            "reason": "force_closed",
            "round_count": len(round_rows),
            "round_ids": [int(row["id"]) for row in round_rows],
        },
    )
    return event_row, round_rows


def event_rounds_are_complete(round_rows: list[Any]) -> bool:
    """이벤트에 예정된 게임이 모두 정산됐는지 확인한다."""
    if not round_rows or any(row["status"] != "closed" for row in round_rows):
        return False

    for index, round_row in enumerate(round_rows):
        extra = round_extra(round_row)
        if extra.get("force_closed"):
            return False
        if round_row["type"] == "cutting" and "cutting_result_level" not in extra:
            return False
        if round_row["type"] not in MAIN_GAME_TYPES:
            continue

        planned_mini_game = str(extra.get("planned_mini_game") or "")
        if planned_mini_game not in MINI_GAME_TYPES:
            continue
        if not any(
            round_game_key(candidate) == planned_mini_game
            and candidate["status"] == "closed"
            and not round_extra(candidate).get("force_closed")
            for candidate in round_rows[index + 1 :]
        ):
            return False
    return True


def build_event_summary_embed(
    conn,
    event_row,
    round_rows: list[Any],
    *,
    force_closed: bool = False,
) -> discord.Embed:
    """종료 시점에 메인 채널에 남길 이벤트 종료 알림. 세부 정산은 각 게임 메시지에 이미 있다."""
    if force_closed:
        return discord.Embed(
            title="⛔ 이벤트 강제 종료",
            description=(
                "남은 게임을 정산하지 않고 이벤트를 종료했습니다.\n"
                "진행 기록과 스레드는 그대로 보관됩니다.\n"
                "수고하셨습니다!"
            ),
            color=discord.Color.orange(),
        )
    return discord.Embed(
        title="🏁 이벤트 종료",
        description=(
            "예정된 게임이 모두 종료되었습니다.\n"
            "세부 결과는 각 게임 메시지와 보관된 스레드에서 확인할 수 있습니다.\n"
            "수고하셨습니다!"
        ),
        color=discord.Color.gold(),
    )


def close_completed_event(event_session_id: int) -> tuple[str, discord.Embed] | None:
    """모든 게임이 끝난 이벤트를 한 번만 종료 상태로 전환하고 요약을 만든다."""
    with db() as conn:
        event_row = conn.execute(
            "SELECT * FROM event_sessions WHERE id = ? AND status != 'closed'",
            (event_session_id,),
        ).fetchone()
        if not event_row:
            return None
        round_rows = conn.execute(
            "SELECT * FROM rounds WHERE event_session_id = ? ORDER BY id ASC",
            (event_session_id,),
        ).fetchall()
        if not event_rounds_are_complete(round_rows):
            return None

        closed_at = now_text()
        cursor = conn.execute(
            """
            UPDATE event_sessions SET status = 'closed', closed_at = ?
            WHERE id = ? AND status != 'closed'
            """,
            (closed_at, event_session_id),
        )
        if cursor.rowcount != 1:
            return None

        participant_count = int(
            conn.execute(
                "SELECT COUNT(*) AS count FROM event_participants WHERE event_session_id = ?",
                (event_session_id,),
            ).fetchone()["count"]
        )
        log_activity(
            conn,
            "event",
            "close",
            event_session_id=event_session_id,
            payload={
                "reason": "completed",
                "participant_count": participant_count,
                # 이벤트 구성(메인->미니 / 미니 단독)을 사후에 한 줄로 알 수 있게 남긴다.
                "rounds": [
                    {
                        "round_id": int(row["id"]),
                        "game_key": round_game_key(row),
                        "title": str(row["title"]),
                    }
                    for row in round_rows
                ],
            },
        )
        event_values = dict(event_row)
        event_values["closed_at"] = closed_at
        return str(event_row["channel_id"]), build_event_summary_embed(
            conn,
            event_values,
            round_rows,
        )


async def publish_event_summary_if_complete(event_session_id: int | None) -> None:
    if not event_session_id:
        return
    summary = close_completed_event(int(event_session_id))
    if not summary:
        return
    channel_id, embed = summary
    channel = await runtime.resolve_text_channel(channel_id)
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


def build_event_open_embed() -> discord.Embed:
    embed = discord.Embed(
        title="오늘의 이벤트가 열렸습니다!",
        description="아래 `참여하기` 버튼을 누른 유저만 이번 이벤트의 모든 게임에 참여할 수 있습니다.",
        color=discord.Color.green(),
    )
    embed.add_field(
        name="🗺️ 진행 흐름",
        value=(
            "> **1** ─ 지금 `참여하기` 버튼으로 오늘의 참가 등록\n"
            "> **2** ─ 메인게임 1개 진행 (딜레마의 섬 또는 모래성 게임)\n"
            "> **3** ─ 메인게임 결과와 점수 정산\n"
            "> **4** ─ 미니게임 1개 진행 후 결과와 점수 정산"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚠️ 꼭 확인해주세요",
        value=(
            "게임이 시작되면 참가 등록이 마감됩니다. 이전 이벤트의 참가 기록은 이어지지 않으며, "
            "이벤트가 다시 열릴 때마다 새로 `참여하기`를 눌러야 합니다."
        ),
        inline=False,
    )
    return embed


def event_join_view(event_session_id: int, participant_count: int = 0) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label=f"참여하기 ({participant_count}명)",
            emoji="🙋",
            style=discord.ButtonStyle.success,
            custom_id=f"eventbot:eventjoin:{event_session_id}",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="참여 취소하기",
            emoji="↩️",
            style=discord.ButtonStyle.danger,
            custom_id=f"eventbot:eventcancel:{event_session_id}",
        )
    )
    return view


def planned_mini_game_view(main_round_id: int) -> discord.ui.View | None:
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND status = 'closed'",
            (main_round_id,),
        ).fetchone()
    if not round_row or round_row["type"] not in MAIN_GAME_TYPES:
        return None

    extra = round_extra(round_row)
    phase = main_game_phase(round_row)
    mini_game = str(extra.get("planned_mini_game") or "")
    if phase != "scored" or mini_game not in MINI_GAME_TYPES:
        return None

    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label=f"{GAME_NAMES[mini_game]} 설정 후 시작",
            style=discord.ButtonStyle.primary,
            custom_id=f"eventbot:startmini:{main_round_id}",
        )
    )
    return view


def parse_duration_minutes(value: str) -> int:
    value = value.strip()
    if not value.isdigit():
        raise ValueError("진행 시간은 숫자로 입력해주세요.")
    minutes = int(value)
    if minutes < 1 or minutes > 1440:
        raise ValueError("진행 시간은 1분부터 1440분 사이로 입력해주세요.")
    return minutes


def close_round_record(
    round_id: int,
    guild: discord.Guild | None,
    closed_by: str = "수동 마감",
) -> tuple[str, discord.Embed] | None:
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row or round_row["status"] != "open":
            return None

        extra = round_extra(round_row)
        # 이 모드는 1차 집계에서 닫히면 안 된다. 호출자는 먼저 재선택 단계로 전환해야 한다.
        if (
            round_row["type"] == "balance"
            and extra.get("mode") == "minority_balance"
            and extra.get("balance_phase") != "revote"
        ):
            return None

        votes = conn.execute(
            "SELECT user_id, choice FROM votes WHERE round_id = ? ORDER BY updated_at ASC",
            (round_row["id"],),
        ).fetchall()
        conn.execute(
            "UPDATE rounds SET status = 'closed', closed_at = ? WHERE id = ?",
            (now_text(), round_row["id"]),
        )

        # 정산은 전부 게임 모듈이 레지스트리에 올린 훅으로 간다. 여기에 게임별 분기를
        # 다시 넣지 말 것 — 새 게임은 자기 모듈에서 register_settler()로 등록한다.
        settler = settler_for(round_game_key(round_row))
        if settler is not None:
            embed = settler(
                SettleContext(
                    conn=conn,
                    round_row=round_row,
                    extra=extra,
                    votes=votes,
                    guild=guild,
                )
            )
        else:
            # 제거된 게임 타입(예: 과거의 'grid')이 열린 채 남아 있는 경우. 정산 없이 닫는다.
            embed = discord.Embed(
                title=f"결과 · {round_row['title']}",
                description=round_row["title"],
                color=discord.Color.light_grey(),
            )
            embed.add_field(
                name="💰 정산",
                value="더 이상 지원하지 않는 게임이라 점수 없이 마감했습니다.",
                inline=False,
            )

        planned_mini_game = str(extra.get("planned_mini_game") or "")
        if round_row["type"] in MAIN_GAME_TYPES and planned_mini_game in MINI_GAME_TYPES:
            embed.add_field(
                name="🎮 다음 미니게임",
                value=f"**{GAME_NAMES[planned_mini_game]}**",
                inline=False,
            )
        embed.set_footer(text=f"시작: {round_row['created_at']} · 종료: {now_text()}")

        # 정산 요약. 분기마다 settlement 문구 유무가 달라서 그 변수에 기대지 않고,
        # 이 라운드에 실제로 적용된 score_logs를 역산한다. 어떤 경로로 닫혔든 결과가 정확하다.
        awarded = conn.execute(
            """
            SELECT user_id, SUM(delta) AS total
            FROM score_logs WHERE round_id = ?
            GROUP BY user_id ORDER BY total DESC
            """,
            (round_row["id"],),
        ).fetchall()
        vote_distribution: dict[str, int] = {}
        for vote_row in votes:
            key = str(vote_row["choice"])
            vote_distribution[key] = vote_distribution.get(key, 0) + 1

        log_activity(
            conn,
            "score",
            "round_settled",
            event_session_id=round_row["event_session_id"],
            round_id=round_row["id"],
            payload={
                "game_key": round_game_key(round_row),
                "closed_by": closed_by,
                "vote_count": len(votes),
                "vote_distribution": vote_distribution,
                "awarded": {
                    str(row["user_id"]): int(row["total"]) for row in awarded
                },
                "total_delta": sum(int(row["total"]) for row in awarded),
                "planned_mini_game": planned_mini_game or None,
                "settings": game_settings_snapshot(round_game_key(round_row)),
            },
        )

        return str(round_row["channel_id"]), embed


class StartSetupModal(discord.ui.Modal):
    def __init__(
        self,
        game: str,
        planned_mini_game: str | None = None,
        standalone_mini_game: bool = False,
    ) -> None:
        self.game = game
        self.planned_mini_game = planned_mini_game
        self.standalone_mini_game = standalone_mini_game
        super().__init__(title=f"{GAME_NAMES.get(game, '게임')} 설정")

        self.game_title = discord.ui.TextInput(
            label="제목",
            placeholder="게임 제목 또는 질문",
            default="야구" if game == "baseball" else None,
            max_length=120,
        )
        self.add_item(self.game_title)
        if game != "baseball":
            self.duration = discord.ui.TextInput(
                label="매칭 준비 시간(분)" if game in MAIN_GAME_TYPES else "진행 시간(분)",
                placeholder="예: 2" if game in MAIN_GAME_TYPES else "예: 5",
                default=str(TRUST_REGISTRATION_DEFAULT_MINUTES) if game in MAIN_GAME_TYPES else "5",
                max_length=4,
            )
            self.add_item(self.duration)

        # 가라앉는 배는 협상·실행 두 단계라 각 시간을 운영자가 따로 정한다.
        # (딜레마의 섬·모래성은 플레이 시간이 상수로 고정돼 있다.)
        if game == "bail":
            self.negotiation_minutes = discord.ui.TextInput(
                label="협상 시간(분)",
                placeholder=f"예: {BAIL_DEFAULT_NEGOTIATION_MINUTES}",
                default=str(BAIL_DEFAULT_NEGOTIATION_MINUTES),
                max_length=4,
            )
            self.execution_minutes = discord.ui.TextInput(
                label="실행(퍼내기 제출) 시간(분)",
                placeholder=f"예: {BAIL_DEFAULT_EXECUTION_MINUTES}",
                default=str(BAIL_DEFAULT_EXECUTION_MINUTES),
                max_length=4,
            )
            self.add_item(self.negotiation_minutes)
            self.add_item(self.execution_minutes)

        if game in {"balance", "minority_balance"}:
            self.option_a = discord.ui.TextInput(label="A 선택지", max_length=80)
            self.option_b = discord.ui.TextInput(label="B 선택지", max_length=80)
            self.add_item(self.option_a)
            self.add_item(self.option_b)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            duration = (
                0
                if self.game == "baseball"
                else parse_duration_minutes(str(self.duration.value))
            )
            title = str(self.game_title.value).strip()
            if not title:
                raise ValueError("제목을 입력해주세요.")

            if self.game == "bail":
                await create_round_from_setup(
                    interaction,
                    self.game,
                    duration,
                    title,
                    협상시간분=parse_duration_minutes(str(self.negotiation_minutes.value)),
                    실행시간분=parse_duration_minutes(str(self.execution_minutes.value)),
                    예정_미니게임=self.planned_mini_game,
                    단독_미니게임=self.standalone_mini_game,
                )
            elif self.game in {"balance", "minority_balance"}:
                await create_round_from_setup(
                    interaction,
                    self.game,
                    duration,
                    title,
                    선택_a=str(self.option_a.value).strip(),
                    선택_b=str(self.option_b.value).strip(),
                    예정_미니게임=self.planned_mini_game,
                    단독_미니게임=self.standalone_mini_game,
                )
            else:
                await create_round_from_setup(
                    interaction,
                    self.game,
                    duration,
                    title,
                    예정_미니게임=self.planned_mini_game,
                    단독_미니게임=self.standalone_mini_game,
                )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)


async def create_round_from_setup(
    interaction: discord.Interaction,
    게임: str,
    진행시간분: int,
    제목: str,
    선택_a: str | None = None,
    선택_b: str | None = None,
    선1: str | None = None,
    선2: str | None = None,
    선3: str | None = None,
    선4: str | None = None,
    선5: str | None = None,
    안전선개수: int = 2,
    안전색: str | None = None,
    힌트: str | None = None,
    협상시간분: int | None = None,
    실행시간분: int | None = None,
    예정_미니게임: str | None = None,
    단독_미니게임: bool = False,
) -> None:
    if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("서버 텍스트 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    with db() as conn:
        initial_event_row = get_current_event(conn)
    if not initial_event_row:
        await interaction.followup.send(
            "현재 열린 이벤트가 없습니다. 먼저 `/오픈`을 실행해주세요.",
            ephemeral=True,
        )
        return

    main_channel_id = str(initial_event_row["channel_id"])
    main_channel = await runtime.resolve_text_channel(main_channel_id)
    if main_channel is None:
        await interaction.followup.send(
            "이벤트를 시작한 메인 텍스트 채널을 찾을 수 없습니다.",
            ephemeral=True,
        )
        return

    if 게임 == "baseball" and not BASEBALL_IMAGE_PATH.is_file():
        await interaction.followup.send(
            f"야구 이미지 자산을 찾을 수 없습니다: `{BASEBALL_IMAGE_PATH.name}`",
            ephemeral=True,
        )
        return

    if 게임 in MAIN_GAME_TYPES and not await ensure_pair_thread_permissions(
        interaction,
        게임,
        main_channel,
    ):
        return

    close_at = time.time() + (int(진행시간분) * 60)
    extra: dict[str, Any] = {"duration_minutes": int(진행시간분)}
    if 게임 != "baseball":
        extra["auto_close_at"] = close_at
    if 게임 in MAIN_GAME_TYPES:
        if 예정_미니게임 not in MINI_GAME_TYPES:
            await interaction.followup.send("시작할 미니게임을 함께 선택해주세요.", ephemeral=True)
            return
        extra["planned_mini_game"] = 예정_미니게임

    with db() as conn:
        if get_open_round(conn):
            await interaction.followup.send("이미 열린 라운드가 있습니다. 먼저 `/라운드마감`을 해주세요.", ephemeral=True)
            return

        event_row = get_current_event(conn)
        if not event_row or int(event_row["id"]) != int(initial_event_row["id"]):
            await interaction.followup.send(
                "이벤트 상태가 바뀌었습니다. 현재 이벤트에서 다시 시도해주세요.",
                ephemeral=True,
            )
            return
        event_session_id = int(event_row["id"])
        participant_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS count FROM event_participants
                WHERE event_session_id = ?
                """,
                (event_session_id,),
            ).fetchone()["count"]
        )
        if 게임 in MAIN_GAME_TYPES and participant_count < 2:
            await interaction.followup.send(
                "메인게임을 시작하려면 `/오픈`의 `참여하기` 등록자가 최소 2명 필요합니다.",
                ephemeral=True,
            )
            return

        previous_round = conn.execute(
            """
            SELECT * FROM rounds
            WHERE event_session_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (event_session_id,),
        ).fetchone()
        if 게임 in MAIN_GAME_TYPES and (event_row["status"] != "recruiting" or previous_round):
            await interaction.followup.send(
                "이번 이벤트의 게임 조합은 이미 확정되었습니다. 다음 이벤트는 `/오픈`부터 다시 시작해주세요.",
                ephemeral=True,
            )
            return
        if previous_round and previous_round["type"] == "cutting":
            previous_extra = round_extra(previous_round)
            if "cutting_result_level" not in previous_extra:
                await interaction.followup.send(
                    "이전 돌 세공 예측의 결과가 아직 확정되지 않았습니다. 먼저 `/세공진행`으로 마무리해주세요.",
                    ephemeral=True,
                )
                return

        if 게임 in MAIN_GAME_TYPES:
            previous_phase = None
            if previous_round:
                previous_extra = round_extra(previous_round)
                previous_phase = (
                    previous_extra.get("trust_phase")
                    if previous_round["type"] == "trust"
                    else previous_extra.get("sand_phase")
                )
            if (
                previous_round
                and previous_round["type"] in MAIN_GAME_TYPES
                and previous_phase not in {"cancelled", None}
            ):
                await interaction.followup.send(
                    "메인게임 다음에는 미니게임 하나를 진행해야 합니다.",
                    ephemeral=True,
                )
                return
        elif not 단독_미니게임:
            previous_extra = round_extra(previous_round) if previous_round else {}
            previous_phase = None
            if previous_round and previous_round["type"] == "trust":
                previous_phase = previous_extra.get("trust_phase")
            elif previous_round and previous_round["type"] == "sand":
                previous_phase = previous_extra.get("sand_phase")
            if (
                not previous_round
                or previous_round["type"] not in MAIN_GAME_TYPES
                or previous_round["status"] != "closed"
                or previous_phase not in {"scored", None}
            ):
                await interaction.followup.send(
                    "미니게임을 시작하려면 참여자 모집·매칭·선택·정산까지 끝난 메인게임이 필요합니다.",
                    ephemeral=True,
                )
                return

        # 라운드 INSERT·embed·view는 각 게임 모듈이 register_creator()로 올린 훅이 만든다.
        # 여기에 게임별 분기를 다시 넣지 말 것 — 컬럼 구성(밸런스만 option_a/b를 쓰는 등)이
        # 게임마다 달라 훅이 직접 INSERT까지 한다.
        creator = creator_for(게임)
        if creator is None:
            await interaction.followup.send(f"'{게임}'은(는) 아직 지원하지 않는 게임입니다.", ephemeral=True)
            return
        try:
            created = creator(
                CreateContext(
                    conn=conn,
                    game_key=게임,
                    title=제목,
                    duration_minutes=int(진행시간분),
                    close_at=close_at,
                    extra=extra,
                    channel_id=main_channel_id,
                    event_session_id=event_session_id,
                    option_a=선택_a,
                    option_b=선택_b,
                    negotiation_minutes=협상시간분,
                    execution_minutes=실행시간분,
                )
            )
        except RoundCreationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        round_id = created.round_id
        embed = created.embed
        view = created.view

        conn.execute(
            "UPDATE rounds SET event_session_id = ? WHERE id = ?",
            (event_session_id, round_id),
        )
        was_recruiting = bool(
            conn.execute(
                "SELECT 1 FROM event_sessions WHERE id = ? AND status = 'recruiting'",
                (event_session_id,),
            ).fetchone()
        )
        conn.execute(
            "UPDATE event_sessions SET status = 'active' WHERE id = ?",
            (event_session_id,),
        )
        # event_sessions에는 created_at/closed_at만 있어 recruiting->active 시각이 남지 않는다.
        # 이벤트 타임라인의 시작점이라 여기서 남겨야 한다. (미니게임 연속 시작 때는 이미 active)
        if was_recruiting:
            participant_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM event_participants WHERE event_session_id = ?",
                    (event_session_id,),
                ).fetchone()["count"]
            )
            log_activity(
                conn,
                "event",
                "start",
                event_session_id=event_session_id,
                actor_user_id=str(interaction.user.id),
                payload={
                    "first_game": 게임,
                    "participant_count": participant_count,
                    "planned_mini_game": extra.get("planned_mini_game"),
                    "bot_version": BOT_VERSION,
                },
            )

        log_activity(
            conn,
            "round",
            "create",
            event_session_id=event_session_id,
            round_id=round_id,
            actor_user_id=str(interaction.user.id),
            payload={
                "game_key": 게임,
                "game_name": GAME_NAMES.get(게임, 게임),
                "title": 제목,
                "duration_minutes": int(진행시간분),
                "is_main_game": 게임 in MAIN_GAME_TYPES,
                # 규칙이 바뀌어도 이 라운드를 당시 기준으로 해석하기 위한 스냅샷
                "settings": game_settings_snapshot(게임),
            },
        )
        if 게임 in MAIN_GAME_TYPES:
            conn.execute(
                """
                INSERT OR IGNORE INTO trust_registrations (round_id, user_id, registered_at)
                SELECT ?, user_id, joined_at
                FROM event_participants
                WHERE event_session_id = ?
                """,
                (round_id, event_session_id),
            )

    # 메인게임 등록 단계에서는 시작 시각만 알린다. 규칙은 임베드 본문에 이미 있고,
    # 다음 미니게임이 뭔지는 참가자가 미리 알 필요가 없다.
    if 게임 in MAIN_GAME_TYPES:
        if 게임 == "bail":
            embed.add_field(
                name="게임 시작",
                value=(
                    f"{진행시간분}분 후 1:1 매칭과 함께 협상이 시작됩니다.\n"
                    f"시작 예정: {close_time_text(close_at)}\n"
                    f"협상 {협상시간분 or BAIL_DEFAULT_NEGOTIATION_MINUTES}분 → "
                    f"실행 {실행시간분 or BAIL_DEFAULT_EXECUTION_MINUTES}분"
                ),
                inline=False,
            )
        elif 게임 == "sand":
            embed.add_field(
                name="게임 시작",
                value=(
                    f"{진행시간분}분 후 1:1 매칭과 함께 게임이 시작됩니다.\n"
                    f"시작 예정: {close_time_text(close_at)}"
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="랜덤 매칭 시작",
                value=(
                    f"오늘의 이벤트 참가 명단으로 {진행시간분}분 후 랜덤 매칭을 시작합니다.\n"
                    f"예정 시각: {close_time_text(close_at)}\n"
                    "매칭 후에는 신뢰/배신 선택 시간이 3분 주어집니다."
                ),
                inline=False,
            )
    elif 게임 != "baseball":
        embed.add_field(
            name="자동 마감",
            value=f"{진행시간분}분 후 자동으로 종료됩니다.\n예정 시각: {close_time_text(close_at)}",
            inline=False,
        )
    send_kwargs: dict[str, Any] = {"embed": embed, "view": view}
    if 게임 == "baseball":
        send_kwargs["file"] = discord.File(
            BASEBALL_IMAGE_PATH,
            filename=BASEBALL_IMAGE_FILENAME,
        )
    message = await main_channel.send(**send_kwargs)
    if 게임 == "baseball":
        control_message = await main_channel.send(
            content=baseball_control_content(1, "waiting_prepare"),
            view=None,
        )
        extra["baseball_control_message_id"] = str(control_message.id)

    discussion_thread_id = None
    if 게임 in {"balance", "minority_balance", "defuse4", "unique_number"}:
        try:
            thread_name = f"{제목} | 토론방"[:100]
            # 보관되면 일반 유저의 버튼 클릭이 막힌다. 라운드가 끝날 때까지 살아 있도록 넉넉히 잡는다.
            thread = await message.create_thread(name=thread_name, auto_archive_duration=1440)
            button_message = await thread.send(
                embed=build_anonymous_guide_embed(0, close_at),
                view=thread_discussion_view(round_id, 게임),
            )
            # 발언이 쌓여도 버튼이 항상 맨 아래에 오도록, 발언마다 이 메시지를 지우고 다시 보낸다.
            extra["anon_button_message_id"] = str(button_message.id)
            if 게임 in {"balance", "minority_balance"}:
                notice_embed = build_thread_notice_embed(
                    round_id,
                    thread,
                    "토론방이 열렸습니다",
                    "버튼을 눌러 익명으로 설득, 블러핑, 진실 말하기를 할 수 있습니다. 횟수 제한은 없습니다.",
                )
            elif 게임 == "unique_number":
                notice_embed = build_thread_notice_embed(
                    round_id,
                    thread,
                    "토론방이 열렸습니다",
                    "버튼을 눌러 익명으로 심리전과 눈치싸움을 펼쳐보세요. 블러핑과 유도 모두 가능합니다. 횟수 제한은 없습니다.",
                )
            else:
                notice_embed = build_thread_notice_embed(
                    round_id,
                    thread,
                    "폭탄 토론방이 열렸습니다",
                    "버튼을 눌러 익명으로 표를 분산하거나 상대를 한 선으로 유도해보세요. 블러핑과 역추리 모두 가능합니다.",
                )
            # 스레드를 잠그면 `스레드 관리` 권한이 없는 유저의 버튼 클릭까지 디스코드가 막는다.
            # 익명성은 on_message의 일반 메시지 삭제로만 지킨다.
            await main_channel.send(embed=notice_embed)
            discussion_thread_id = str(thread.id)
        except discord.HTTPException:
            discussion_thread_id = None

    with db() as conn:
        if 게임 == "baseball":
            log_activity(
                conn,
                "round",
                "baseball_control_published",
                event_session_id=event_session_id,
                round_id=round_id,
                actor_user_id=str(interaction.user.id),
                payload={
                    "pitch_number": 1,
                    "control_message_id": extra.get("baseball_control_message_id"),
                },
            )
        conn.execute(
            """
            UPDATE rounds
            SET message_id = ?, discussion_thread_id = ?, extra_json = ?
            WHERE id = ?
            """,
            (
                str(message.id),
                discussion_thread_id,
                json.dumps(extra, ensure_ascii=False),
                round_id,
            ),
        )

    if 게임 != "baseball":
        runtime.auto_close_scheduler().schedule_auto_close(round_id, close_at)
    await interaction.followup.send(
        f"게임 메시지를 <#{main_channel_id}>에 게시했습니다.",
        ephemeral=True,
    )


async def open_event_command(interaction: discord.Interaction) -> None:
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message(
            "이벤트 메인 채널로 사용할 서버 텍스트 채널에서 실행해주세요. 스레드에서는 열 수 없습니다.",
            ephemeral=True,
        )
        return

    with db() as conn:
        if get_open_round(conn):
            await interaction.response.send_message(
                "진행 중인 라운드가 있습니다. 먼저 `/라운드마감`으로 마무리해주세요.",
                ephemeral=True,
            )
            return

        conn.execute(
            """
            UPDATE event_sessions
            SET status = 'closed', closed_at = ?
            WHERE status IN ('recruiting', 'active')
            """,
            (now_text(),),
        )
        cursor = conn.execute(
            """
            INSERT INTO event_sessions (status, channel_id, created_at)
            VALUES ('recruiting', ?, ?)
            """,
            (str(interaction.channel.id), now_text()),
        )
        event_session_id = int(cursor.lastrowid)
        log_activity(
            conn,
            "event",
            "open",
            event_session_id=event_session_id,
            actor_user_id=str(interaction.user.id),
            payload={
                "channel_id": str(interaction.channel.id),
                "bot_version": BOT_VERSION,
            },
        )

    try:
        await interaction.response.send_message(
            embed=build_event_open_embed(),
            view=event_join_view(event_session_id),
        )
        message = await interaction.original_response()
    except discord.HTTPException:
        with db() as conn:
            conn.execute(
                "UPDATE event_sessions SET status = 'closed', closed_at = ? WHERE id = ?",
                (now_text(), event_session_id),
            )
        raise

    with db() as conn:
        conn.execute(
            "UPDATE event_sessions SET message_id = ? WHERE id = ?",
            (str(message.id), event_session_id),
        )


async def start_command(interaction: discord.Interaction, 메인게임: str, 미니게임: str) -> None:
    with db() as conn:
        event_row = get_current_event(conn)
        if not event_row:
            await interaction.response.send_message(
                "먼저 `/오픈`으로 오늘의 이벤트 참가 모집을 열어주세요.",
                ephemeral=True,
            )
            return
        if event_row["status"] != "recruiting":
            await interaction.response.send_message(
                "이번 이벤트의 게임 조합은 이미 확정되었습니다. 다음 이벤트는 `/오픈`부터 다시 시작해주세요.",
                ephemeral=True,
            )
            return
    await interaction.response.send_modal(StartSetupModal(메인게임, planned_mini_game=미니게임))


async def start_mini_game_command(interaction: discord.Interaction, 게임: str) -> None:
    with db() as conn:
        event_row = get_current_event(conn)
        if not event_row:
            await interaction.response.send_message(
                "먼저 `/오픈`으로 오늘의 이벤트 참가 모집을 열어주세요.",
                ephemeral=True,
            )
            return
        if get_open_round(conn):
            await interaction.response.send_message(
                "이미 열린 라운드가 있습니다. 먼저 `/라운드마감`을 해주세요.",
                ephemeral=True,
            )
            return
    await interaction.response.send_modal(StartSetupModal(게임, standalone_mini_game=True))


async def close_round_command(interaction: discord.Interaction) -> None:
    with db() as conn:
        round_row = get_open_round(conn)
    if not round_row:
        await interaction.response.send_message("현재 열린 라운드가 없습니다.", ephemeral=True)
        return

    # 트러스트/샌드/베일/소수파 밸런스는 games.<module>.advance_* 헬퍼가 등록 마감
    # → 다음 단계 시작까지 한 번에 처리한다. 자동 마감(EventBot.auto_close_round)도
    # 같은 헬퍼를 쓰므로 두 경로가 갈라지지 않는다.
    if round_row["type"] == "trust" and round_extra(round_row).get("trust_phase") == "registration":
        await interaction.response.defer(ephemeral=True)
        transition = await trust.advance_trust_registration(int(round_row["id"]))
        if not transition:
            await interaction.followup.send("참여자 모집을 마감하지 못했습니다.", ephemeral=True)
            return
        runtime.auto_close_scheduler().cancel_auto_close(int(round_row["id"]))
        if transition["state"] == "decision":
            runtime.auto_close_scheduler().schedule_auto_close(
                int(round_row["id"]), float(transition["close_at"])
            )
            await interaction.followup.send("참여자 모집을 마감하고 랜덤 매칭 및 3분 선택을 시작했습니다.", ephemeral=True)
        else:
            await interaction.followup.send("참여자가 부족해 딜레마의 섬을 취소했습니다.", ephemeral=True)
        return

    if round_row["type"] == "sand" and round_extra(round_row).get("sand_phase") == "registration":
        # 짝마다 비공개 방을 여느라 API 호출이 여러 번이다. 3초 응답 제한을 넘기므로 먼저 defer한다.
        await interaction.response.defer(ephemeral=True)
        transition = await sand.advance_sand_registration(int(round_row["id"]))
        if not transition:
            await interaction.followup.send("참여자 모집을 마감하지 못했습니다.", ephemeral=True)
            return
        runtime.auto_close_scheduler().cancel_auto_close(int(round_row["id"]))
        if transition["state"] == "playing":
            runtime.auto_close_scheduler().schedule_auto_close(
                int(round_row["id"]), float(transition["close_at"])
            )
            await interaction.followup.send("참여자 모집을 마감하고 1:1 모래성 게임을 시작했습니다.", ephemeral=True)
        else:
            await interaction.followup.send("참여자가 부족해 모래성 게임을 취소했습니다.", ephemeral=True)
        return

    if round_game_key(round_row) == "bail" and main_game_phase(round_row) in {
        "registration",
        "negotiation",
    }:
        # 짝마다 비공개 방을 여느라 API 호출이 여러 번이다. 3초 응답 제한을 넘기므로 먼저 defer한다.
        await interaction.response.defer(ephemeral=True)
        round_id = int(round_row["id"])
        registration = main_game_phase(round_row) == "registration"
        transition = (
            await bail.advance_bail_registration(round_id)
            if registration
            else await bail.advance_bail_negotiation(round_id)
        )
        if not transition:
            await interaction.followup.send("현재 단계를 마감하지 못했습니다.", ephemeral=True)
            return
        runtime.auto_close_scheduler().cancel_auto_close(round_id)
        if transition["state"] == "cancelled":
            await interaction.followup.send("참여자가 부족해 가라앉는 배를 취소했습니다.", ephemeral=True)
            return
        if registration:
            await interaction.followup.send(
                "참여자 모집을 마감하고 1:1 협상 단계를 시작했습니다.", ephemeral=True
            )
        else:
            await interaction.followup.send("협상을 마감하고 실행 단계를 시작했습니다.", ephemeral=True)
        runtime.auto_close_scheduler().schedule_auto_close(round_id, float(transition["close_at"]))
        return

    round_extra_row = round_extra(round_row)
    if (
        round_row["type"] == "balance"
        and round_extra_row.get("mode") == "minority_balance"
        and round_extra_row.get("balance_phase") == "initial"
    ):
        await interaction.response.defer(ephemeral=True)
        transition = await balance.advance_minority_balance_initial(int(round_row["id"]))
        if not transition:
            await interaction.followup.send("1차 집계를 마감하지 못했습니다.", ephemeral=True)
            return
        runtime.auto_close_scheduler().cancel_auto_close(int(round_row["id"]))
        runtime.auto_close_scheduler().schedule_auto_close(
            int(round_row["id"]), float(transition["close_at"])
        )
        if transition["published"]:
            await interaction.followup.send(
                "1차 인원수를 공개하고 같은 선택지의 재선택을 시작했습니다.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "재선택 단계로 전환했지만 이벤트 메인 채널에 안내를 게시하지 못했습니다.",
                ephemeral=True,
            )
        return

    # 결과 메시지는 명령을 실행한 위치와 무관하게 이벤트가 시작된 메인 채널에만 게시한다.
    is_trust_decision = (
        round_row["type"] == "trust" and round_extra_row.get("trust_phase") == "decision"
    )
    is_sand_playing = (
        round_row["type"] == "sand"
        and round_extra_row.get("mode") != "bail"
        and round_extra_row.get("sand_phase") == "playing"
    )
    is_bail_execution = (
        round_game_key(round_row) == "bail" and round_extra_row.get("bail_phase") == "execution"
    )
    publishes_pair_results = is_trust_decision or is_sand_playing or is_bail_execution
    await interaction.response.defer(ephemeral=True)

    result = close_round_record(int(round_row["id"]), interaction.guild)
    if not result:
        await interaction.followup.send("현재 열린 라운드가 없습니다.", ephemeral=True)
        return

    runtime.auto_close_scheduler().cancel_auto_close(int(round_row["id"]))
    baseball.cancel_baseball_timers(int(round_row["id"]))
    await delete_baseball_control_message(round_row)

    channel_id, embed = result
    if publishes_pair_results:
        if is_trust_decision:
            await publish_trust_pair_results(int(round_row["id"]))
        elif is_bail_execution:
            await publish_bail_pair_results(int(round_row["id"]))
        else:
            await publish_sand_pair_results(int(round_row["id"]))
    await archive_round_discussion_thread(round_row, reason="라운드 종료 후 영구 보관")

    channel = await runtime.resolve_text_channel(channel_id)
    if channel is None:
        await interaction.followup.send(
            "라운드는 마감했지만 이벤트 메인 채널을 찾지 못해 결과를 게시하지 못했습니다.",
            ephemeral=True,
        )
        return
    await channel.send(
        embed=embed,
        view=planned_mini_game_view(int(round_row["id"])),
    )
    await publish_event_summary_if_complete(round_row["event_session_id"])
    await interaction.followup.send(
        f"라운드 결과를 <#{channel_id}>에 게시했습니다.",
        ephemeral=True,
    )


async def force_end_event_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    with db() as conn:
        result = force_close_current_event(conn)
        force_summary: tuple[str, discord.Embed] | None = None
        if result:
            original_event_row, _ = result
            closed_event_row = conn.execute(
                "SELECT * FROM event_sessions WHERE id = ?",
                (int(original_event_row["id"]),),
            ).fetchone()
            closed_round_rows = conn.execute(
                "SELECT * FROM rounds WHERE event_session_id = ? ORDER BY id ASC",
                (int(original_event_row["id"]),),
            ).fetchall()
            force_summary = (
                str(closed_event_row["channel_id"]),
                build_event_summary_embed(
                    conn,
                    closed_event_row,
                    closed_round_rows,
                    force_closed=True,
                ),
            )
    if not result:
        await interaction.followup.send("현재 열려 있는 이벤트가 없습니다.", ephemeral=True)
        return

    event_row, round_rows = result
    open_round_count = sum(1 for row in round_rows if row["status"] == "open")
    client = runtime.get_client()

    for round_row in round_rows:
        runtime.auto_close_scheduler().cancel_auto_close(int(round_row["id"]))
        baseball.cancel_baseball_timers(int(round_row["id"]))
        await delete_baseball_control_message(round_row)

        channel_id = round_row["channel_id"]
        message_id = round_row["message_id"]
        if channel_id and message_id:
            try:
                channel = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
                if hasattr(channel, "fetch_message"):
                    message = await channel.fetch_message(int(message_id))
                    await message.edit(view=None)
            except (discord.HTTPException, ValueError):
                pass

        thread_id = round_row["discussion_thread_id"]
        if thread_id:
            try:
                thread = client.get_channel(int(thread_id)) or await client.fetch_channel(int(thread_id))
                if isinstance(thread, discord.Thread):
                    # archive+lock은 삭제가 아니다. 활성 목록에서만 빠지고 보관 목록에 영구히 남는다.
                    await thread.edit(
                        archived=True,
                        locked=True,
                        reason="개발용 이벤트 강제 종료",
                    )
            except (discord.HTTPException, ValueError):
                pass

        # 딜레마의 섬·모래성은 짝마다 비공개 방을 연다. 강제 종료 시 전부 닫는다.
        if round_row["type"] in MAIN_GAME_TYPES:
            with db() as conn:
                pair_threads = conn.execute(
                    """
                    SELECT discussion_thread_id FROM trust_pairings
                    WHERE round_id = ? AND discussion_thread_id IS NOT NULL
                    """,
                    (int(round_row["id"]),),
                ).fetchall()
            for pair_thread in pair_threads:
                try:
                    pair_thread_id = int(pair_thread["discussion_thread_id"])
                    thread = client.get_channel(pair_thread_id) or await client.fetch_channel(pair_thread_id)
                    if isinstance(thread, discord.Thread):
                        # 강제 종료 때도 스레드를 지우지 않고 읽기 전용 보관 상태로만 전환한다.
                        await thread.edit(
                            archived=True,
                            locked=True,
                            reason="개발용 이벤트 강제 종료",
                        )
                except (discord.HTTPException, ValueError):
                    pass

    if event_row["message_id"]:
        try:
            channel = client.get_channel(int(event_row["channel_id"])) or await client.fetch_channel(
                int(event_row["channel_id"])
            )
            if hasattr(channel, "fetch_message"):
                message = await channel.fetch_message(int(event_row["message_id"]))
                await message.edit(view=None)
        except (discord.HTTPException, ValueError):
            pass

    if force_summary:
        channel_id, summary_embed = force_summary
        channel = await runtime.resolve_text_channel(channel_id)
        if channel is not None:
            try:
                await channel.send(embed=summary_embed)
            except discord.HTTPException:
                pass

    await interaction.followup.send(
        f"이벤트 #{event_row['id']}을 강제로 종료했습니다. "
        f"미완료 라운드 {open_round_count}개는 점수 정산 없이 취소했습니다.",
        ephemeral=True,
    )


async def list_events_command(interaction: discord.Interaction) -> None:
    with db() as conn:
        event_row = get_current_event(conn)
        if not event_row:
            await interaction.response.send_message("진행 중인 이벤트가 없습니다.", ephemeral=True)
            return
        event_session_id = int(event_row["id"])
        participant_count = int(
            conn.execute(
                "SELECT COUNT(*) AS count FROM event_participants WHERE event_session_id = ?",
                (event_session_id,),
            ).fetchone()["count"]
        )
        round_rows = conn.execute(
            "SELECT * FROM rounds WHERE event_session_id = ? ORDER BY id ASC",
            (event_session_id,),
        ).fetchall()

    status = str(event_row["status"])
    embed = discord.Embed(
        title=f"진행 중인 이벤트 #{event_session_id}",
        description=f"<#{event_row['channel_id']}>에서 진행 중입니다.",
        color=discord.Color.green(),
    )
    embed.add_field(
        name="상태",
        value=(
            f"{EVENT_STATUS_LABELS.get(status, status)} · 참가자 {participant_count}명\n"
            f"시작: {event_row['created_at']}"
        ),
        inline=False,
    )

    lines: list[str] = []
    for round_row in round_rows:
        extra = round_extra(round_row)
        game_key = round_game_key(round_row)
        # 메인게임은 main_game_phase가 모아두고, 단계가 있는 미니게임은 여기서 붙인다.
        phase = main_game_phase(round_row) or extra.get("baseball_phase")
        round_status = "진행 중" if round_row["status"] == "open" else "마감"
        lines.append(
            f"#{round_row['id']} {GAME_NAMES.get(game_key, game_key)} · {round_status}"
            f"{f' ({phase})' if phase else ''} · {round_row['question']}"
        )
    embed.add_field(
        name="라운드",
        value=field_text(lines, empty="아직 시작한 라운드가 없습니다."),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
