from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "event_game.sqlite3"
KST_LABEL = "Asia/Seoul"
DEFUSE_COLORS = [
    {"id": "red", "name": "빨강", "emoji": "🟥"},
    {"id": "blue", "name": "파랑", "emoji": "🟦"},
    {"id": "black", "name": "검정", "emoji": "⬛"},
    {"id": "white", "name": "흰색", "emoji": "⬜"},
]
DEFUSE_COLOR_BY_ID = {color["id"]: color for color in DEFUSE_COLORS}
DEFUSE_COLOR_ALIASES = {
    "빨강": "red",
    "빨간": "red",
    "빨간색": "red",
    "빨강색": "red",
    "red": "red",
    "파랑": "blue",
    "파란": "blue",
    "파란색": "blue",
    "파랑색": "blue",
    "blue": "blue",
    "검정": "black",
    "검은": "black",
    "검은색": "black",
    "검정색": "black",
    "black": "black",
    "흰색": "white",
    "하양": "white",
    "하얀": "white",
    "하얀색": "white",
    "흰": "white",
    "white": "white",
}
GRID_TILE_CHOICES = [str(index) for index in range(1, 10)]
GRID_REWARD_POINTS = 3
CUTTING_LEVEL_CHOICES = [str(index) for index in range(0, 7)]
CUTTING_REWARD_POINTS = {
    0: 1,
    1: 1,
    2: 1,
    3: 2,
    4: 2,
    5: 3,
    6: 4,
}
CUTTING_SUCCESS_RATES = {
    0: 0.8,
    1: 0.8,
    2: 0.6,
    3: 0.6,
    4: 0.5,
    5: 0.3,
}
TOP3_NONE_CHOICE = "__none__"
TRUST_REGISTRATION_DEFAULT_MINUTES = 2
TRUST_DECISION_SECONDS = 180
TRUST_MATCH_HISTORY_ROUNDS = 3
TRUST_OUTCOME_LABELS = {
    "mutual": "상호 신뢰",
    "single": "단독 배신",
    "double": "동시 배신",
}
MAIN_GAME_TYPES = {"trust", "sand"}
MINI_GAME_TYPES = {"balance", "defuse4", "grid", "cutting", "top3"}
GAME_NAMES = {
    "balance": "일반 밸런스",
    "defuse4": "4선 폭탄 해체",
    "grid": "관리자 시점 타일",
    "cutting": "돌 세공 예측",
    "top3": "상위 3명 견제",
    "trust": "신뢰 거래",
    "sand": "모래성 게임",
}
SAND_NUMBER_CHOICES = [str(index) for index in range(1, 11)]
SAND_PLAY_SECONDS = 600
SAND_RULES_TEXT = (
    "1. 각 참여자가 1~10 중 모래성이 무너질 횟수를 정합니다.\n"
    "2. 실제 무너질 횟수는 정한 횟수끼리 뺀 값(두 수의 차이)으로 결정됩니다.\n"
    "3. 참여자는 번갈아가며 `포기하기` 또는 `모래 덜기`를 결정합니다.\n\n"
    "- 둘 다 포기 선택 시: 서로 -1점\n"
    "- 한 명이 포기(A), 다른 한 명이 모래 덜기(B) 선택 시:\n"
    "  - 덜기 성공: A는 -1점, B는 +2점\n"
    "  - 무너짐: B는 -1점, A는 +1점\n"
    "- 서로 덜기를 하다 무너진 경우: 그 턴의 사람이 -1점"
)
SAND_OUTCOME_LABELS = {
    "mutual_quit": "둘 다 포기",
    "quit_success": "포기 후 상대 덜기 성공",
    "quit_collapse": "포기 후 상대 모래성 붕괴",
    "plain_collapse": "일반 모래성 붕괴",
}


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def cutting_success_rate(current_level: int) -> float:
    return CUTTING_SUCCESS_RATES[current_level]


@contextmanager
def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                total_score INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL CHECK (type IN ('balance', 'bomb', 'grid', 'cutting', 'top3', 'trust', 'sand')),
                title TEXT NOT NULL,
                question TEXT NOT NULL,
                option_a TEXT,
                option_b TEXT,
                status TEXT NOT NULL CHECK (status IN ('open', 'closed')) DEFAULT 'open',
                channel_id TEXT,
                message_id TEXT,
                discussion_thread_id TEXT,
                extra_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                closed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS event_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL CHECK (status IN ('recruiting', 'active', 'closed')),
                channel_id TEXT NOT NULL,
                message_id TEXT,
                created_at TEXT NOT NULL,
                closed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS event_participants (
                event_session_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                joined_at TEXT NOT NULL,
                PRIMARY KEY (event_session_id, user_id),
                FOREIGN KEY (event_session_id) REFERENCES event_sessions(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS votes (
                round_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                choice TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (round_id, user_id),
                FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS discussion_posts (
                round_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (round_id, user_id),
                FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS trust_registrations (
                round_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                registered_at TEXT NOT NULL,
                PRIMARY KEY (round_id, user_id),
                FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS trust_pairings (
                round_id INTEGER NOT NULL,
                user_a_id TEXT NOT NULL,
                user_b_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (round_id, user_a_id),
                UNIQUE (round_id, user_b_id),
                FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS score_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id INTEGER,
                user_id TEXT NOT NULL,
                delta INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE SET NULL
            );
            """
        )
        migrate_round_type_check(conn)
        ensure_event_session_schema(conn)


def migrate_round_type_check(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'rounds'"
    ).fetchone()
    if not row or "sand" in row["sql"]:
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        CREATE TABLE rounds_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK (type IN ('balance', 'bomb', 'grid', 'cutting', 'top3', 'trust', 'sand')),
            title TEXT NOT NULL,
            question TEXT NOT NULL,
            option_a TEXT,
            option_b TEXT,
            status TEXT NOT NULL CHECK (status IN ('open', 'closed')) DEFAULT 'open',
            channel_id TEXT,
            message_id TEXT,
            discussion_thread_id TEXT,
            extra_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            closed_at TEXT
        );

        INSERT INTO rounds_new
            (id, type, title, question, option_a, option_b, status, channel_id,
             message_id, discussion_thread_id, extra_json, created_at, closed_at)
        SELECT
            id, type, title, question, option_a, option_b, status, channel_id,
            message_id, discussion_thread_id, extra_json, created_at, closed_at
        FROM rounds;

        DROP TABLE rounds;
        ALTER TABLE rounds_new RENAME TO rounds;
        """
    )
    conn.execute("PRAGMA foreign_keys = ON")


def ensure_event_session_schema(conn: sqlite3.Connection) -> None:
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(rounds)").fetchall()
    }
    if "event_session_id" not in columns:
        conn.execute("ALTER TABLE rounds ADD COLUMN event_session_id INTEGER")


def ensure_user(conn: sqlite3.Connection, user: discord.abc.User) -> None:
    display_name = getattr(user, "display_name", None) or user.name
    ts = now_text()
    conn.execute(
        """
        INSERT INTO users (user_id, display_name, total_score, created_at, updated_at)
        VALUES (?, ?, 0, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            display_name = excluded.display_name,
            updated_at = excluded.updated_at
        """,
        (str(user.id), display_name, ts, ts),
    )


def add_score(
    conn: sqlite3.Connection,
    user_id: str,
    display_name: str,
    delta: int,
    reason: str,
    round_id: int | None = None,
) -> None:
    ts = now_text()
    conn.execute(
        """
        INSERT INTO users (user_id, display_name, total_score, created_at, updated_at)
        VALUES (?, ?, 0, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            display_name = excluded.display_name,
            updated_at = excluded.updated_at
        """,
        (user_id, display_name, ts, ts),
    )
    conn.execute(
        "UPDATE users SET total_score = total_score + ?, updated_at = ? WHERE user_id = ?",
        (delta, ts, user_id),
    )
    conn.execute(
        """
        INSERT INTO score_logs (round_id, user_id, delta, reason, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (round_id, user_id, delta, reason, ts),
    )


def get_open_round(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM rounds WHERE status = 'open' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def get_current_event(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM event_sessions
        WHERE status IN ('recruiting', 'active')
        ORDER BY id DESC LIMIT 1
        """
    ).fetchone()


def force_close_current_event(
    conn: sqlite3.Connection,
) -> tuple[sqlite3.Row, list[sqlite3.Row]] | None:
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
    return event_row, round_rows


def is_event_participant(
    conn: sqlite3.Connection,
    event_session_id: int,
    user_id: str,
) -> bool:
    return conn.execute(
        """
        SELECT 1 FROM event_participants
        WHERE event_session_id = ? AND user_id = ?
        """,
        (event_session_id, user_id),
    ).fetchone() is not None


def can_join_round(conn: sqlite3.Connection, round_id: int, user_id: str) -> bool:
    round_row = conn.execute(
        "SELECT event_session_id FROM rounds WHERE id = ?",
        (round_id,),
    ).fetchone()
    if not round_row:
        return False
    event_session_id = round_row["event_session_id"]
    # 기존 DB에서 기능 추가 전에 생성된 라운드는 종전 동작을 유지합니다.
    if event_session_id is None:
        return True
    return is_event_participant(conn, int(event_session_id), user_id)


def mention_list(user_ids: list[str], limit: int = 40) -> str:
    if not user_ids:
        return "없음"
    mentions = [f"<@{user_id}>" for user_id in user_ids[:limit]]
    suffix = f"\n외 {len(user_ids) - limit}명" if len(user_ids) > limit else ""
    return ", ".join(mentions) + suffix


def format_score_message(display_name: str, total_score: int) -> str:
    safe_name = discord.utils.escape_mentions(display_name)
    return f"{safe_name} 현재 점수: **{total_score}점**"


def fetch_ranking_rows(conn: sqlite3.Connection, current_bot_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT users.user_id, users.display_name, users.total_score
        FROM users
        WHERE users.user_id != ?
          AND (
              EXISTS (
                  SELECT 1 FROM votes
                  WHERE votes.user_id = users.user_id
              )
              OR EXISTS (
                  SELECT 1 FROM trust_registrations
                  WHERE trust_registrations.user_id = users.user_id
              )
          )
        ORDER BY users.total_score DESC, users.updated_at ASC
        """,
        (current_bot_id,),
    ).fetchall()


def shuffle_tied_ranking_rows(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    shuffled_rows = list(rows)
    group_start = 0
    while group_start < len(shuffled_rows):
        group_end = group_start + 1
        score = shuffled_rows[group_start]["total_score"]
        while group_end < len(shuffled_rows) and shuffled_rows[group_end]["total_score"] == score:
            group_end += 1
        if group_end - group_start > 1:
            tied_rows = shuffled_rows[group_start:group_end]
            random.shuffle(tied_rows)
            shuffled_rows[group_start:group_end] = tied_rows
        group_start = group_end
    return shuffled_rows


def format_ranking_lines(rows: list[sqlite3.Row]) -> list[str]:
    return [f"{index}등 - **{row['total_score']}점**" for index, row in enumerate(rows, start=1)]


async def current_guild_member(guild: discord.Guild, user_id: str) -> discord.Member | None:
    try:
        return await guild.fetch_member(int(user_id))
    except discord.NotFound:
        return None


def field_text(lines: list[str], empty: str = "없음", limit: int = 1024) -> str:
    text = "\n".join(lines) if lines else empty
    return text if len(text) <= limit else text[: limit - 3] + "..."


def member_name(guild: discord.Guild | None, user_id: str) -> str:
    if guild:
        member = guild.get_member(int(user_id))
        if member:
            return member.display_name
    return f"User {user_id}"


def choice_display(choice: str) -> str:
    if choice in DEFUSE_COLOR_BY_ID:
        color = DEFUSE_COLOR_BY_ID[choice]
        return f"{color['emoji']} {color['name']}"
    if choice == TOP3_NONE_CHOICE:
        return "선택하지 않는다"
    if choice in CUTTING_LEVEL_CHOICES:
        return f"{choice}강화"
    return choice


def operator_role_ids() -> set[int]:
    role_ids: set[int] = set()
    for value in os.getenv("OPERATOR_ROLE_IDS", "").replace(";", ",").split(","):
        value = value.strip()
        if value.isdigit():
            role_ids.add(int(value))
    return role_ids


def is_operator_user(user: discord.abc.User) -> bool:
    permissions = getattr(user, "guild_permissions", None)
    if permissions and (permissions.administrator or permissions.manage_guild):
        return True

    allowed_role_ids = operator_role_ids()
    user_roles = getattr(user, "roles", [])
    return bool(allowed_role_ids and any(role.id in allowed_role_ids for role in user_roles))


def admin_only() -> app_commands.check:
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_operator_user(interaction.user):
            return True

        raise app_commands.CheckFailure("이 명령어는 관리자 권한 또는 운영자 역할이 필요합니다.")

    return app_commands.check(predicate)


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


def build_event_open_embed() -> discord.Embed:
    embed = discord.Embed(
        title="오늘의 이벤트가 열렸습니다!",
        description="아래 `참여하기` 버튼을 누른 유저만 이번 이벤트의 모든 게임에 참여할 수 있습니다.",
        color=discord.Color.green(),
    )
    embed.add_field(
        name="진행 흐름",
        value=(
            "1. 지금 `참여하기` 버튼으로 오늘의 참가 등록\n"
            "2. 메인게임 1개 진행 (신뢰 거래 또는 모래성 게임)\n"
            "3. 메인게임 결과와 점수 정산\n"
            "4. 미니게임 1개 진행 후 결과와 점수 정산"
        ),
        inline=False,
    )
    embed.add_field(
        name="꼭 확인해주세요",
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
    phase = extra.get("trust_phase") if round_row["type"] == "trust" else extra.get("sand_phase")
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


async def handle_planned_mini_game(interaction: discord.Interaction, main_round_id: int) -> None:
    if not is_operator_user(interaction.user):
        await interaction.response.send_message(
            "이 버튼은 관리자 권한 또는 운영자 역할이 필요합니다.",
            ephemeral=True,
        )
        return

    with db() as conn:
        main_round = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND status = 'closed'",
            (main_round_id,),
        ).fetchone()
        if not main_round or main_round["type"] not in MAIN_GAME_TYPES:
            await interaction.response.send_message("완료된 메인게임을 찾을 수 없습니다.", ephemeral=True)
            return
        event_row = conn.execute(
            "SELECT status FROM event_sessions WHERE id = ?",
            (main_round["event_session_id"],),
        ).fetchone()
        if not event_row or event_row["status"] == "closed":
            await interaction.response.send_message("이미 종료된 이벤트입니다.", ephemeral=True)
            return

        extra = round_extra(main_round)
        phase = extra.get("trust_phase") if main_round["type"] == "trust" else extra.get("sand_phase")
        mini_game = str(extra.get("planned_mini_game") or "")
        if phase != "scored" or mini_game not in MINI_GAME_TYPES:
            await interaction.response.send_message("시작할 미니게임이 지정되어 있지 않습니다.", ephemeral=True)
            return
        if get_open_round(conn):
            await interaction.response.send_message(
                "이미 열린 라운드가 있습니다. 먼저 `/라운드마감`을 해주세요.",
                ephemeral=True,
            )
            return
        latest_round = conn.execute(
            """
            SELECT id FROM rounds
            WHERE event_session_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (main_round["event_session_id"],),
        ).fetchone()
        if not latest_round or int(latest_round["id"]) != main_round_id:
            await interaction.response.send_message("이 이벤트의 미니게임은 이미 시작되었습니다.", ephemeral=True)
            return

    await interaction.response.send_modal(StartSetupModal(mini_game))


async def handle_event_join(interaction: discord.Interaction, event_session_id: int) -> None:
    if interaction.user.bot:
        await interaction.response.send_message("봇 계정은 이벤트에 참여할 수 없습니다.", ephemeral=True)
        return

    with db() as conn:
        event_row = conn.execute(
            "SELECT status FROM event_sessions WHERE id = ?",
            (event_session_id,),
        ).fetchone()
        if not event_row or event_row["status"] != "recruiting":
            await interaction.response.send_message(
                "이 이벤트의 참가 등록은 이미 마감되었습니다. 다음 오픈을 기다려주세요.",
                ephemeral=True,
            )
            return

        ensure_user(conn, interaction.user)
        existing = conn.execute(
            """
            SELECT 1 FROM event_participants
            WHERE event_session_id = ? AND user_id = ?
            """,
            (event_session_id, str(interaction.user.id)),
        ).fetchone()
        if existing:
            await interaction.response.send_message(
                "이미 이번 이벤트에 참여 등록되어 있습니다.",
                ephemeral=True,
            )
            return

        conn.execute(
            """
            INSERT INTO event_participants (event_session_id, user_id, joined_at)
            VALUES (?, ?, ?)
            """,
            (event_session_id, str(interaction.user.id), now_text()),
        )
        participant_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS count FROM event_participants
                WHERE event_session_id = ?
                """,
                (event_session_id,),
            ).fetchone()["count"]
        )

    await interaction.response.edit_message(
        view=event_join_view(event_session_id, participant_count),
    )
    await interaction.followup.send(
        "참여 등록 완료! 이번 이벤트의 메인게임과 미니게임에 참여할 수 있습니다.",
        ephemeral=True,
    )


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
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("토론 스레드에서만 발언할 수 있습니다.", ephemeral=True)
            return

        content = self.content_input.value.strip()
        if not content:
            await interaction.response.send_message("발언 내용을 입력해주세요.", ephemeral=True)
            return

        with db() as conn:
            round_row = conn.execute(
                """
                SELECT id FROM rounds
                WHERE id = ? AND status = 'open' AND discussion_thread_id = ?
                """,
                (self.round_id, str(interaction.channel.id)),
            ).fetchone()
            if not round_row:
                await interaction.response.send_message("이미 마감되었거나 유효하지 않은 토론방입니다.", ephemeral=True)
                return
            if not can_join_round(conn, self.round_id, str(interaction.user.id)):
                await interaction.response.send_message(
                    "이번 이벤트에서 `참여하기`를 누른 유저만 토론에 참여할 수 있습니다.",
                    ephemeral=True,
                )
                return

            already = conn.execute(
                "SELECT 1 FROM discussion_posts WHERE round_id = ? AND user_id = ?",
                (self.round_id, str(interaction.user.id)),
            ).fetchone()
            if already:
                await interaction.response.send_message("이 라운드에는 이미 발언을 남겼습니다.", ephemeral=True)
                return

            ensure_user(conn, interaction.user)
            # Reserve the one allowed post before sending, so double-clicks cannot post twice.
            conn.execute(
                """
                INSERT INTO discussion_posts (round_id, user_id, message_id, content, created_at)
                VALUES (?, ?, 'pending', ?, ?)
                """,
                (self.round_id, str(interaction.user.id), content, now_text()),
            )

        try:
            # 익명 토론방은 평소 보관+잠금 상태여서 일반 유저의 채팅 입력이 막혀 있습니다.
            # 봇이 익명 발언을 올릴 때만 잠시 열고, 전송 직후 다시 닫습니다.
            await interaction.channel.edit(
                archived=False,
                locked=False,
                reason="익명 미니게임 발언 게시",
            )
            posted_message = await interaction.channel.send(
                f"**익명 참가자**\n{content}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            with db() as conn:
                conn.execute(
                    """
                    DELETE FROM discussion_posts
                    WHERE round_id = ? AND user_id = ? AND message_id = 'pending'
                    """,
                    (self.round_id, str(interaction.user.id)),
                )
            await interaction.response.send_message("익명 발언을 올리지 못했습니다. 다시 시도해주세요.", ephemeral=True)
            return
        finally:
            try:
                await interaction.channel.edit(
                    archived=True,
                    locked=True,
                    reason="익명 미니게임 토론방: 일반 채팅 차단",
                )
            except discord.HTTPException:
                pass

        with db() as conn:
            conn.execute(
                """
                UPDATE discussion_posts SET message_id = ?
                WHERE round_id = ? AND user_id = ?
                """,
                (str(posted_message.id), self.round_id, str(interaction.user.id)),
            )

        await interaction.response.send_message("익명으로 발언을 올렸습니다.", ephemeral=True)


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


def start_trust_decision(round_id: int) -> dict[str, Any] | None:
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'trust' AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row:
            return None

        extra = round_extra(round_row)
        if extra.get("trust_phase") != "registration":
            return None

        registrations = conn.execute(
            "SELECT user_id FROM trust_registrations WHERE round_id = ? ORDER BY registered_at ASC",
            (round_id,),
        ).fetchall()
        user_ids = [str(row["user_id"]) for row in registrations]
        if len(user_ids) < 2:
            extra.update({"trust_phase": "cancelled", "cancelled_reason": "not_enough_participants"})
            conn.execute(
                "UPDATE rounds SET status = 'closed', closed_at = ?, extra_json = ? WHERE id = ?",
                (now_text(), json.dumps(extra, ensure_ascii=False), round_id),
            )
            embed = discord.Embed(
                title=f"{round_row['title']} · 취소",
                description="참여자가 2명 미만이라 오늘의 메인 콘텐츠를 시작하지 못했습니다.",
                color=discord.Color.light_grey(),
            )
            return {
                "state": "cancelled",
                "channel_id": str(round_row["channel_id"]),
                "message_id": str(round_row["message_id"] or ""),
                "embed": embed,
            }

        history_rounds = conn.execute(
            """
            SELECT id FROM rounds
            WHERE type = 'trust' AND id < ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (round_id, TRUST_MATCH_HISTORY_ROUNDS),
        ).fetchall()
        history_ids = [int(row["id"]) for row in history_rounds]
        recent_pairs: set[tuple[str, str]] = set()
        if history_ids:
            placeholders = ", ".join("?" for _ in history_ids)
            history_pairs = conn.execute(
                f"SELECT user_a_id, user_b_id FROM trust_pairings WHERE round_id IN ({placeholders})",
                tuple(history_ids),
            ).fetchall()
            recent_pairs = {
                tuple(sorted((str(row["user_a_id"]), str(row["user_b_id"]))))
                for row in history_pairs
            }

        pairs, unmatched_user_id = build_random_trust_pairs(user_ids, recent_pairs)
        for user_a_id, user_b_id in pairs:
            conn.execute(
                """
                INSERT INTO trust_pairings (round_id, user_a_id, user_b_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (round_id, user_a_id, user_b_id, now_text()),
            )

        decision_close_at = time.time() + TRUST_DECISION_SECONDS
        extra.update(
            {
                "trust_phase": "decision",
                "unmatched_user_id": unmatched_user_id,
                "decision_close_at": decision_close_at,
                "auto_close_at": decision_close_at,
                "decision_duration_seconds": TRUST_DECISION_SECONDS,
            }
        )
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )

        embed = build_trust_decision_embed(round_id, str(round_row["title"]), pairs, unmatched_user_id)
        return {
            "state": "decision",
            "channel_id": str(round_row["channel_id"]),
            "message_id": str(round_row["message_id"] or ""),
            "embed": embed,
            "view": trust_decision_view(round_id),
            "close_at": decision_close_at,
        }


async def update_trust_main_message(transition: dict[str, Any]) -> None:
    try:
        channel = bot.get_channel(int(transition["channel_id"]))
        if channel is None:
            channel = await bot.fetch_channel(int(transition["channel_id"]))
        message_id = transition.get("message_id")
        if message_id:
            message = await channel.fetch_message(int(message_id))
            await message.edit(embed=transition["embed"], view=transition.get("view"))
        elif hasattr(channel, "send"):
            await channel.send(embed=transition["embed"], view=transition.get("view"))
    except discord.HTTPException:
        return


async def handle_trust_join(interaction: discord.Interaction, round_id: int) -> None:
    if interaction.user.bot:
        await interaction.response.send_message("봇 계정은 참여 체크할 수 없습니다.", ephemeral=True)
        return

    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'trust' AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row or round_extra(round_row).get("trust_phase") != "registration":
            await interaction.response.send_message("참여자 모집이 이미 끝났습니다.", ephemeral=True)
            return
        if not can_join_round(conn, round_id, str(interaction.user.id)):
            await interaction.response.send_message(
                "이벤트 오픈 때 `참여하기`를 누른 유저만 게임에 참여할 수 있습니다.",
                ephemeral=True,
            )
            return

        existing = conn.execute(
            "SELECT 1 FROM trust_registrations WHERE round_id = ? AND user_id = ?",
            (round_id, str(interaction.user.id)),
        ).fetchone()
        if existing:
            await interaction.response.send_message("이미 오늘의 신뢰 거래에 참여 등록했습니다.", ephemeral=True)
            return

        ensure_user(conn, interaction.user)
        conn.execute(
            "INSERT INTO trust_registrations (round_id, user_id, registered_at) VALUES (?, ?, ?)",
            (round_id, str(interaction.user.id), now_text()),
        )
    await interaction.response.send_message("참여 체크 완료! 모집 마감 후 무작위로 2명씩 매칭됩니다.", ephemeral=True)


async def handle_trust_decision(
    interaction: discord.Interaction,
    round_id: int,
    choice: str,
) -> None:
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'trust' AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row or round_extra(round_row).get("trust_phase") != "decision":
            await interaction.response.send_message("현재는 선택할 수 있는 시간이 아닙니다.", ephemeral=True)
            return

        pairing = conn.execute(
            """
            SELECT 1 FROM trust_pairings
            WHERE round_id = ? AND (user_a_id = ? OR user_b_id = ?)
            """,
            (round_id, str(interaction.user.id), str(interaction.user.id)),
        ).fetchone()
        if not pairing:
            await interaction.response.send_message("랜덤 매칭된 참가자만 신뢰 또는 배신을 선택할 수 있습니다.", ephemeral=True)
            return

        existing = conn.execute(
            "SELECT 1 FROM votes WHERE round_id = ? AND user_id = ?",
            (round_id, str(interaction.user.id)),
        ).fetchone()
        if existing:
            await interaction.response.send_message("선택을 이미 확정했습니다. 변경할 수 없습니다.", ephemeral=True)
            return

        ensure_user(conn, interaction.user)
        conn.execute(
            """
            INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (round_id, str(interaction.user.id), choice, now_text(), now_text()),
        )
    await interaction.response.send_message("선택을 비공개로 확정했습니다.", ephemeral=True)


def start_sand_game(round_id: int) -> dict[str, Any] | None:
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'sand' AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row:
            return None

        extra = round_extra(round_row)
        if extra.get("sand_phase") != "registration":
            return None

        registrations = conn.execute(
            "SELECT user_id FROM trust_registrations WHERE round_id = ? ORDER BY registered_at ASC",
            (round_id,),
        ).fetchall()
        user_ids = [str(row["user_id"]) for row in registrations]
        if len(user_ids) < 2:
            extra.update({"sand_phase": "cancelled", "cancelled_reason": "not_enough_participants"})
            conn.execute(
                "UPDATE rounds SET status = 'closed', closed_at = ?, extra_json = ? WHERE id = ?",
                (now_text(), json.dumps(extra, ensure_ascii=False), round_id),
            )
            embed = discord.Embed(
                title=f"{round_row['title']} · 취소",
                description="참여자가 2명 미만이라 모래성 게임을 시작하지 못했습니다.",
                color=discord.Color.light_grey(),
            )
            return {
                "state": "cancelled",
                "channel_id": str(round_row["channel_id"]),
                "message_id": str(round_row["message_id"] or ""),
                "embed": embed,
            }

        pairs, unmatched_user_id = build_random_trust_pairs(user_ids, set())
        sand_pairs: list[dict[str, Any]] = []
        for user_a_id, user_b_id in pairs:
            conn.execute(
                """
                INSERT INTO trust_pairings (round_id, user_a_id, user_b_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (round_id, user_a_id, user_b_id, now_text()),
            )
            sand_pairs.append(
                {
                    "user_a_id": user_a_id,
                    "user_b_id": user_b_id,
                    "number_a": None,
                    "number_b": None,
                    "phase": "number_selection",
                    "current_user_id": random.choice([user_a_id, user_b_id]),
                    "successful_removals": 0,
                }
            )

        play_close_at = time.time() + SAND_PLAY_SECONDS
        extra.update(
            {
                "sand_phase": "playing",
                "sand_pairs": sand_pairs,
                "unmatched_user_id": unmatched_user_id,
                "play_close_at": play_close_at,
                "auto_close_at": play_close_at,
                "play_duration_seconds": SAND_PLAY_SECONDS,
            }
        )
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )
        updated_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        return {
            "state": "playing",
            "channel_id": str(round_row["channel_id"]),
            "message_id": str(round_row["message_id"] or ""),
            "embed": build_sand_play_embed(updated_row, extra),
            "view": sand_play_view(round_id),
            "close_at": play_close_at,
        }


async def handle_sand_join(interaction: discord.Interaction, round_id: int) -> None:
    if interaction.user.bot:
        await interaction.response.send_message("봇 계정은 참여할 수 없습니다.", ephemeral=True)
        return
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'sand' AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row or round_extra(round_row).get("sand_phase") != "registration":
            await interaction.response.send_message("참여자 모집이 이미 끝났습니다.", ephemeral=True)
            return
        if not can_join_round(conn, round_id, str(interaction.user.id)):
            await interaction.response.send_message(
                "이벤트 오픈 때 `참여하기`를 누른 유저만 게임에 참여할 수 있습니다.",
                ephemeral=True,
            )
            return
        existing = conn.execute(
            "SELECT 1 FROM trust_registrations WHERE round_id = ? AND user_id = ?",
            (round_id, str(interaction.user.id)),
        ).fetchone()
        if existing:
            await interaction.response.send_message("이미 모래성 게임에 참여 등록했습니다.", ephemeral=True)
            return
        ensure_user(conn, interaction.user)
        conn.execute(
            "INSERT INTO trust_registrations (round_id, user_id, registered_at) VALUES (?, ?, ?)",
            (round_id, str(interaction.user.id), now_text()),
        )
    await interaction.response.send_message("참여 체크 완료! 모집 마감 후 1:1로 무작위 매칭됩니다.", ephemeral=True)


def find_sand_pair(extra: dict[str, Any], user_id: str) -> dict[str, Any] | None:
    for pair in extra.get("sand_pairs", []):
        if user_id in {str(pair.get("user_a_id")), str(pair.get("user_b_id"))}:
            return pair
    return None


def sand_pair_threshold(pair: dict[str, Any]) -> tuple[int, int]:
    raw_difference = abs(int(pair["number_a"]) - int(pair["number_b"]))
    return raw_difference, max(1, raw_difference)


def sand_games_complete(extra: dict[str, Any]) -> bool:
    pairs = extra.get("sand_pairs", [])
    return bool(pairs) and all(pair.get("phase") == "resolved" for pair in pairs)


async def refresh_or_finish_sand_round(
    interaction: discord.Interaction,
    round_id: int,
    private_message: str,
) -> None:
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row:
            await interaction.response.send_message("존재하지 않는 라운드입니다.", ephemeral=True)
            return
        extra = round_extra(round_row)
        complete = sand_games_complete(extra)
        progress_embed = build_sand_play_embed(round_row, extra)

    await interaction.response.send_message(private_message, ephemeral=True)
    if interaction.message:
        try:
            await interaction.message.edit(
                embed=progress_embed,
                view=sand_play_view(round_id, disabled=complete),
            )
        except discord.HTTPException:
            pass

    if not complete:
        return
    result = close_round_record(round_id, interaction.guild, closed_by="모든 1:1 게임 종료")
    if not result:
        return
    task = bot.auto_close_tasks.pop(round_id, None)
    if task:
        task.cancel()
    _, result_embed = result
    await interaction.followup.send(embed=result_embed, view=planned_mini_game_view(round_id))


async def handle_sand_number(
    interaction: discord.Interaction,
    round_id: int,
    number: int,
) -> None:
    user_id = str(interaction.user.id)
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'sand' AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row:
            await interaction.response.send_message("이미 끝난 모래성 게임입니다.", ephemeral=True)
            return
        extra = round_extra(round_row)
        if extra.get("sand_phase") != "playing":
            await interaction.response.send_message("아직 숫자를 정할 수 없습니다.", ephemeral=True)
            return
        pair = find_sand_pair(extra, user_id)
        if not pair:
            await interaction.response.send_message("이 라운드에 매칭된 참가자가 아닙니다.", ephemeral=True)
            return
        if pair.get("phase") != "number_selection":
            await interaction.response.send_message("숫자 선택 단계가 이미 끝났습니다.", ephemeral=True)
            return
        key = "number_a" if user_id == str(pair["user_a_id"]) else "number_b"
        if pair.get(key) is not None:
            await interaction.response.send_message("숫자를 이미 확정했습니다. 변경할 수 없습니다.", ephemeral=True)
            return
        pair[key] = number
        both_selected = pair.get("number_a") is not None and pair.get("number_b") is not None
        if both_selected:
            pair["phase"] = "active"
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )
        current_user_id = str(pair["current_user_id"])

    suffix = (
        f" 두 사람의 선택이 끝났습니다. {'내 차례입니다.' if current_user_id == user_id else f'<@{current_user_id}>님의 차례입니다.'}"
        if both_selected
        else " 상대의 비공개 선택을 기다립니다."
    )
    await refresh_or_finish_sand_round(interaction, round_id, f"`{number}`을 비공개로 확정했습니다.{suffix}")


async def handle_sand_action(
    interaction: discord.Interaction,
    round_id: int,
    action: str,
) -> None:
    user_id = str(interaction.user.id)
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'sand' AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row:
            await interaction.response.send_message("이미 끝난 모래성 게임입니다.", ephemeral=True)
            return
        extra = round_extra(round_row)
        pair = find_sand_pair(extra, user_id)
        if not pair:
            await interaction.response.send_message("이 라운드에 매칭된 참가자가 아닙니다.", ephemeral=True)
            return
        phase = str(pair.get("phase"))
        if phase == "number_selection":
            await interaction.response.send_message("두 참가자 모두 먼저 1~10 중 숫자를 확정해야 합니다.", ephemeral=True)
            return
        if phase == "resolved":
            await interaction.response.send_message("이 1:1 게임은 이미 끝났습니다.", ephemeral=True)
            return
        if user_id != str(pair.get("current_user_id")):
            await interaction.response.send_message("지금은 상대의 차례입니다.", ephemeral=True)
            return

        user_a_id = str(pair["user_a_id"])
        user_b_id = str(pair["user_b_id"])
        other_user_id = user_b_id if user_id == user_a_id else user_a_id
        raw_difference, collapse_attempt = sand_pair_threshold(pair)
        private_message: str

        if action == "quit":
            if phase == "awaiting_quit_response":
                quitter_id = str(pair["quitter_id"])
                pair.update(
                    {
                        "phase": "resolved",
                        "outcome": "mutual_quit",
                        "result": {"losers": [quitter_id, user_id]},
                    }
                )
                private_message = "상대의 포기에 함께 포기했습니다. 두 사람 모두 -1점입니다."
            else:
                pair.update(
                    {
                        "phase": "awaiting_quit_response",
                        "quitter_id": user_id,
                        "current_user_id": other_user_id,
                    }
                )
                private_message = "포기했습니다. 상대가 포기할지, 마지막으로 모래를 덜지 결정합니다."
        else:
            attempt = int(pair.get("successful_removals", 0)) + 1
            collapsed = attempt >= collapse_attempt
            if collapsed:
                if phase == "awaiting_quit_response":
                    quitter_id = str(pair["quitter_id"])
                    pair.update(
                        {
                            "phase": "resolved",
                            "outcome": "quit_collapse",
                            "result": {"winner": quitter_id, "collapsed_user": user_id},
                            "collapse_attempt": attempt,
                            "raw_difference": raw_difference,
                        }
                    )
                    private_message = "모래를 덜다가 무너졌습니다. 나는 -1점, 먼저 포기한 상대는 +1점입니다."
                else:
                    pair.update(
                        {
                            "phase": "resolved",
                            "outcome": "plain_collapse",
                            "result": {"collapsed_user": user_id},
                            "collapse_attempt": attempt,
                            "raw_difference": raw_difference,
                        }
                    )
                    private_message = "모래를 덜다가 무너졌습니다. 무너진 차례의 참가자는 -1점입니다."
            elif phase == "awaiting_quit_response":
                quitter_id = str(pair["quitter_id"])
                pair.update(
                    {
                        "phase": "resolved",
                        "outcome": "quit_success",
                        "successful_removals": attempt,
                        "result": {"winner": user_id, "quitter": quitter_id},
                        "raw_difference": raw_difference,
                    }
                )
                private_message = "마지막 모래 덜기에 성공했습니다. 나는 +2점, 먼저 포기한 상대는 -1점입니다."
            else:
                pair.update(
                    {
                        "successful_removals": attempt,
                        "current_user_id": other_user_id,
                    }
                )
                private_message = f"모래 덜기 성공! 이제 <@{other_user_id}>님의 차례입니다."

        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )

    await refresh_or_finish_sand_round(interaction, round_id, private_message)


class EventBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.auto_close_tasks: dict[int, asyncio.Task[None]] = {}
        self.auto_close_restored = False

    async def setup_hook(self) -> None:
        guild_id = os.getenv("GUILD_ID", "").strip()
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"Slash commands synced to guild {guild_id}")
        else:
            await self.tree.sync()
            print("Slash commands synced globally. It can take a while to appear.")

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user} ({self.user.id if self.user else 'unknown'})")
        print(f"Database: {DB_PATH}")
        if not self.auto_close_restored:
            self.auto_close_restored = True
            self.restore_auto_close_tasks()

    def schedule_auto_close(self, round_id: int, close_at: float) -> None:
        existing = self.auto_close_tasks.pop(round_id, None)
        if existing:
            existing.cancel()
        self.auto_close_tasks[round_id] = asyncio.create_task(
            self.auto_close_round(round_id, close_at)
        )

    def restore_auto_close_tasks(self) -> None:
        with db() as conn:
            rows = conn.execute(
                "SELECT id, extra_json FROM rounds WHERE status = 'open'"
            ).fetchall()

        for row in rows:
            try:
                extra: dict[str, Any] = json.loads(row["extra_json"])
                close_at = float(extra.get("auto_close_at", 0))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if close_at > 0:
                self.schedule_auto_close(int(row["id"]), close_at)

    async def auto_close_round(self, round_id: int, close_at: float) -> None:
        try:
            await asyncio.sleep(max(0, close_at - time.time()))
            with db() as conn:
                row = conn.execute(
                    "SELECT * FROM rounds WHERE id = ? AND status = 'open'",
                    (round_id,),
                ).fetchone()
            if not row:
                return

            channel_id = str(row["channel_id"])
            channel = self.get_channel(int(channel_id))
            if channel is None:
                try:
                    channel = await self.fetch_channel(int(channel_id))
                except discord.HTTPException:
                    return
            guild = getattr(channel, "guild", None)

            if row["type"] == "trust" and round_extra(row).get("trust_phase") == "registration":
                transition = start_trust_decision(round_id)
                if not transition:
                    return
                await update_trust_main_message(transition)
                if transition["state"] == "decision":
                    self.schedule_auto_close(round_id, float(transition["close_at"]))
                return

            if row["type"] == "sand" and round_extra(row).get("sand_phase") == "registration":
                transition = start_sand_game(round_id)
                if not transition:
                    return
                await update_trust_main_message(transition)
                if transition["state"] == "playing":
                    self.schedule_auto_close(round_id, float(transition["close_at"]))
                return

            result = close_round_record(round_id, guild, closed_by="자동 마감")
            if not result:
                return

            _, embed = result
            if hasattr(channel, "send"):
                await channel.send(embed=embed, view=planned_mini_game_view(round_id))
        except asyncio.CancelledError:
            raise
        finally:
            if self.auto_close_tasks.get(round_id) is asyncio.current_task():
                self.auto_close_tasks.pop(round_id, None)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith("eventbot:"):
            return

        parts = custom_id.split(":")
        if len(parts) < 3:
            await interaction.response.send_message("알 수 없는 버튼입니다.", ephemeral=True)
            return

        _, action = parts[:2]
        if action == "eventjoin":
            try:
                event_session_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 이벤트 번호입니다.", ephemeral=True)
                return
            await handle_event_join(interaction, event_session_id)
            return

        if action == "startmini":
            try:
                main_round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 메인게임 번호입니다.", ephemeral=True)
                return
            await handle_planned_mini_game(interaction, main_round_id)
            return

        if action == "sandjoin":
            try:
                round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return
            await handle_sand_join(interaction, round_id)
            return

        if action == "sandnumber":
            if len(parts) != 4 or parts[3] not in SAND_NUMBER_CHOICES:
                await interaction.response.send_message("잘못된 모래성 숫자입니다.", ephemeral=True)
                return
            try:
                round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return
            await handle_sand_number(interaction, round_id, int(parts[3]))
            return

        if action == "sandaction":
            if len(parts) != 4 or parts[3] not in {"quit", "remove"}:
                await interaction.response.send_message("잘못된 모래성 행동입니다.", ephemeral=True)
                return
            try:
                round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return
            await handle_sand_action(interaction, round_id, parts[3])
            return

        if action == "trustjoin":
            try:
                round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return
            await handle_trust_join(interaction, round_id)
            return

        if action == "trustdecision":
            if len(parts) != 4:
                await interaction.response.send_message("잘못된 신뢰 거래 선택입니다.", ephemeral=True)
                return
            _, _, round_id_text, choice = parts
            try:
                round_id = int(round_id_text)
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return
            if choice not in {"trust", "betray"}:
                await interaction.response.send_message("잘못된 선택입니다.", ephemeral=True)
                return
            await handle_trust_decision(interaction, round_id, choice)
            return

        if action in {"contractaccept", "contractdecline"}:
            await interaction.response.send_message(
                "거래 제안 방식은 랜덤 매칭 방식으로 변경되었습니다. 새 신뢰 거래에 참여해주세요.",
                ephemeral=True,
            )
            return

        if action == "trustcontract":
            await interaction.response.send_message(
                "거래 제안 방식은 랜덤 매칭 방식으로 변경되었습니다. 새 신뢰 거래에 참여해주세요.",
                ephemeral=True,
            )
            return

        if action in {"trustself", "trust"}:
            await interaction.response.send_message(
                "직접 점수 투표 방식은 랜덤 매칭 신뢰/배신 방식으로 변경되었습니다. 새 신뢰 거래에 참여해주세요.",
                ephemeral=True,
            )
            return

        if action == "anonymous":
            try:
                round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return

            if not isinstance(interaction.channel, discord.Thread):
                await interaction.response.send_message("토론 스레드에서만 사용할 수 있는 버튼입니다.", ephemeral=True)
                return

            with db() as conn:
                round_row = conn.execute(
                    """
                    SELECT id FROM rounds
                    WHERE id = ? AND status = 'open' AND discussion_thread_id = ?
                    """,
                    (round_id, str(interaction.channel.id)),
                ).fetchone()
            if not round_row:
                await interaction.response.send_message("이미 마감되었거나 유효하지 않은 토론방입니다.", ephemeral=True)
                return
            if not can_join_round(conn, round_id, str(interaction.user.id)):
                await interaction.response.send_message(
                    "이번 이벤트에서 `참여하기`를 누른 유저만 토론에 참여할 수 있습니다.",
                    ephemeral=True,
                )
                return

            await interaction.response.send_modal(AnonymousDiscussionModal(round_id))
            return

        if action == "cutting":
            try:
                round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return

            await handle_cutting_button(interaction, round_id)
            return

        if len(parts) < 4:
            await interaction.response.send_message("알 수 없는 버튼입니다.", ephemeral=True)
            return

        _, action, round_id_text, choice = parts[:4]
        if action != "vote":
            await interaction.response.send_message("알 수 없는 동작입니다.", ephemeral=True)
            return

        try:
            round_id = int(round_id_text)
        except ValueError:
            await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
            return

        with db() as conn:
            round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
            if not round_row:
                await interaction.response.send_message("존재하지 않는 라운드입니다.", ephemeral=True)
                return
            if round_row["status"] != "open":
                await interaction.response.send_message("이미 마감된 라운드입니다.", ephemeral=True)
                return
            if not can_join_round(conn, round_id, str(interaction.user.id)):
                await interaction.response.send_message(
                    "이번 이벤트에서 `참여하기`를 누른 유저만 게임에 참여할 수 있습니다.",
                    ephemeral=True,
                )
                return

            ensure_user(conn, interaction.user)
            ts = now_text()
            conn.execute(
                """
                INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(round_id, user_id) DO UPDATE SET
                    choice = excluded.choice,
                    updated_at = excluded.updated_at
                """,
                (round_id, str(interaction.user.id), choice, ts, ts),
            )

        await interaction.response.send_message(
            f"`{choice_display(choice)}` 선택을 저장했습니다. 마감 전까지 버튼을 다시 누르면 선택이 바뀝니다.",
            ephemeral=True,
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

        with db() as conn:
            round_row = conn.execute(
                """
                SELECT * FROM rounds
                WHERE status = 'open' AND discussion_thread_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (str(message.channel.id),),
            ).fetchone()

            if not round_row:
                return

            if round_row["type"] == "trust":
                # The main game's negotiation is intentionally public and not limited to one post.
                return

            # Direct messages reveal their author before the bot can remove them.
            # Discussion posts must therefore go through the anonymous button/modal.
            try:
                await message.delete()
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass


bot = EventBot()


def build_balance_embed(round_id: int, question: str, option_a: str, option_b: str) -> discord.Embed:
    embed = discord.Embed(
        title=question,
        description=question,
        color=discord.Color.blurple(),
    )
    embed.add_field(name="A", value=option_a, inline=True)
    embed.add_field(name="B", value=option_b, inline=True)
    embed.add_field(
        name="규칙",
        value=(
            "투표 현황은 마감 전까지 비공개입니다.\n"
            "토론 스레드에서는 1인 1회만 발언할 수 있습니다.\n"
            "마감 후 다수파 선택자에게 +1점이 지급됩니다."
        ),
        inline=False,
    )
    return embed


def build_defuse_embed(round_id: int, title: str, hint: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=title,
        color=discord.Color.dark_red(),
    )
    embed.add_field(
        name="해체 가능한 버튼",
        value=" ".join(f"{color['emoji']} {color['name']}" for color in DEFUSE_COLORS),
        inline=False,
    )
    embed.add_field(name="관리자의 힌트", value=hint, inline=False)
    embed.add_field(
        name="규칙",
        value=(
            "4개의 색 버튼 중 단 하나만 안전한 버튼입니다.\n"
            "관리자가 정한 안전 색은 마감 전까지 공개되지 않습니다.\n"
            "해체 토론방에서 한 번씩 심리전과 블러핑을 할 수 있습니다.\n"
            "마감 후 안전한 버튼을 고른 사람에게만 +1점이 지급됩니다."
        ),
        inline=False,
    )
    return embed


def build_grid_embed(round_id: int, title: str, admin_clue: str, reward_points: int) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=title,
        color=discord.Color.purple(),
    )
    embed.add_field(name="관리자의 선택 이유", value=admin_clue, inline=False)
    embed.add_field(
        name="타일",
        value="1 | 2 | 3\n4 | 5 | 6\n7 | 8 | 9",
        inline=False,
    )
    embed.add_field(
        name="규칙",
        value=(
            "관리자가 자기 관점에서 왜 그 칸을 골랐는지 추리하는 게임입니다.\n"
            "토론방에서 한 번 의견을 남길 수 있으며, 분탕과 블러핑으로 다른 유저를 흔들 수 있습니다.\n"
            f"마감 후 정답 칸을 고른 사람에게 +{reward_points}점이 지급됩니다."
        ),
        inline=False,
    )
    return embed


def build_cutting_embed(round_id: int, title: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=title,
        color=discord.Color.teal(),
    )
    embed.add_field(
        name="예측 단계",
        value="0강화 | 1강화 | 2강화 | 3강화 | 4강화 | 5강화 | 6강화",
        inline=False,
    )
    embed.add_field(
        name="규칙",
        value=(
            "강화 성공 확률: 0→1·1→2 80% | 2→3·3→4 60% | 4→5 50% | 5→6 30%\n"
            "처음 실패하기 직전의 강화 단계를 맞히는 게임입니다.\n"
            "정답 보상: 0~2강화 +1점 | 3~4강화 +2점 | 5강화 +3점 | 6강화 +4점\n"
            "투표 집계가 끝나면 운영자가 `/세공진행`으로 직접 세공을 진행합니다."
        ),
        inline=False,
    )
    return embed


def build_thread_notice_embed(round_id: int, thread: discord.Thread, title: str, guide: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=f"{thread.mention}\n{guide}",
        color=discord.Color.gold(),
    )
    embed.add_field(name="참여 방법", value="위 스레드를 눌러 의견을 남겨주세요.", inline=False)
    return embed


def build_top3_embed(round_id: int, title: str, candidates: list[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=title,
        color=discord.Color.orange(),
    )
    candidate_lines = [
        f"{index}. <@{candidate['user_id']}> - {candidate['score']}점"
        for index, candidate in enumerate(candidates, start=1)
    ]
    embed.add_field(name="후보", value="\n".join(candidate_lines), inline=False)
    embed.add_field(
        name="규칙",
        value=(
            "현재 점수가 가장 높은 3명과 `선택하지 않는다` 중 하나에 투표합니다.\n"
            "`선택하지 않는다` 득표수가 세 후보 득표수 합보다 많으면 상위 3명을 제외한 모두가 +1점입니다.\n"
            "그렇지 않으면 세 후보 중 가장 많이 선택된 1명이 -1점입니다."
        ),
        inline=False,
    )
    return embed


def build_trust_registration_embed(round_id: int, title: str, registration_minutes: int) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=title,
        color=discord.Color.dark_gold(),
    )
    embed.add_field(
        name="진행 순서",
        value=(
            f"매칭 준비 {registration_minutes}분 → 무작위 2인 1세트 매칭 → 신뢰/배신 선택 3분 → 점수 정산 → 미니게임"
        ),
        inline=False,
    )
    embed.add_field(
        name="메인 규칙",
        value=(
            "이벤트 오픈 때 `참여하기`를 누른 유저가 자동으로 참가합니다. 준비 시간이 끝나면 봇이 참가자를 무작위로 둘씩 매칭합니다.\n"
            "참여자가 홀수면 한 명은 이번 회차 매칭 대기이며, 나머지만 2명씩 게임을 진행합니다. 상호 신뢰는 각 +1점, 단독 배신은 배신자 +2점·상대 0점, 동시 배신은 각 -1점입니다."
        ),
        inline=False,
    )
    return embed


def vote_view(round_id: int, choices: list[tuple[str, str]]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for label, value in choices:
        view.add_item(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary,
                custom_id=f"eventbot:vote:{round_id}:{value}",
            )
        )
    return view


def trust_registration_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="참여 체크하기",
            emoji="✋",
            style=discord.ButtonStyle.success,
            custom_id=f"eventbot:trustjoin:{round_id}",
        )
    )
    return view


def build_trust_decision_embed(
    round_id: int,
    title: str,
    pairs: list[tuple[str, str]],
    unmatched_user_id: str | None,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=title,
        color=discord.Color.gold(),
    )
    pair_lines = [f"<@{user_a_id}> ↔ <@{user_b_id}>" for user_a_id, user_b_id in pairs]
    embed.add_field(name="랜덤 매칭", value=field_text(pair_lines), inline=False)
    if unmatched_user_id:
        embed.add_field(
            name="매칭 대기",
            value=f"<@{unmatched_user_id}>님은 참여자가 홀수여서 이번 회차에는 짝이 배정되지 않았습니다.",
            inline=False,
        )
    embed.add_field(
        name="선택 규칙 (3분)",
        value="짝과 공개적으로 협상한 뒤, 아래 버튼으로 `신뢰` 또는 `배신`을 한 번만 확정하세요.",
        inline=False,
    )
    return embed


def trust_decision_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="상대를 신뢰",
            emoji="🤝",
            style=discord.ButtonStyle.success,
            custom_id=f"eventbot:trustdecision:{round_id}:trust",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="상대를 배신",
            emoji="🗡️",
            style=discord.ButtonStyle.danger,
            custom_id=f"eventbot:trustdecision:{round_id}:betray",
        )
    )
    return view


def build_sand_registration_embed(round_id: int, title: str, registration_minutes: int) -> discord.Embed:
    embed = discord.Embed(title=title, description=title, color=discord.Color.from_rgb(218, 165, 90))
    embed.add_field(
        name="진행 순서",
        value=(
            f"매칭 준비 {registration_minutes}분 → 무작위 1:1 매칭 → 각자 비공개 숫자 선택 → "
            "번갈아 포기/모래 덜기 → 정산 → 미니게임"
        ),
        inline=False,
    )
    embed.add_field(
        name="메인 규칙",
        value=SAND_RULES_TEXT,
        inline=False,
    )
    return embed


def sand_registration_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="참여 체크하기",
            emoji="🏖️",
            style=discord.ButtonStyle.success,
            custom_id=f"eventbot:sandjoin:{round_id}",
        )
    )
    return view


def build_sand_play_embed(round_row: sqlite3.Row, extra: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title=str(round_row["title"]),
        description="아래 숫자 버튼과 행동 버튼은 매칭된 본인에게만 적용되며, 선택 내용은 비공개입니다.",
        color=discord.Color.from_rgb(218, 165, 90),
    )
    lines: list[str] = []
    for index, pair in enumerate(extra.get("sand_pairs", []), start=1):
        user_a_id = str(pair["user_a_id"])
        user_b_id = str(pair["user_b_id"])
        phase = str(pair.get("phase"))
        if phase == "number_selection":
            selected = int(pair.get("number_a") is not None) + int(pair.get("number_b") is not None)
            status = f"비공개 숫자 선택 {selected}/2"
        elif phase == "active":
            status = (
                f"<@{pair['current_user_id']}> 차례 · 성공한 모래 덜기 "
                f"{int(pair.get('successful_removals', 0))}회"
            )
        elif phase == "awaiting_quit_response":
            status = f"<@{pair['current_user_id']}>님이 함께 포기/마지막 덜기 선택 중"
        else:
            status = SAND_OUTCOME_LABELS.get(str(pair.get("outcome")), "종료")
        lines.append(f"{index}. <@{user_a_id}> ↔ <@{user_b_id}> · {status}")
    embed.add_field(name="1:1 진행 현황", value=field_text(lines), inline=False)
    unmatched_user_id = str(extra.get("unmatched_user_id") or "")
    if unmatched_user_id:
        embed.add_field(
            name="매칭 대기",
            value=f"<@{unmatched_user_id}>님은 참여자가 홀수여서 이번 회차에 짝이 배정되지 않았습니다.",
            inline=False,
        )
    embed.add_field(
        name="선택 제한 시간",
        value=f"10분 안에 모든 1:1 게임을 끝내주세요. 예정 마감: {close_time_text(extra.get('play_close_at'))}",
        inline=False,
    )
    return embed


def sand_play_view(round_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for index, value in enumerate(SAND_NUMBER_CHOICES):
        view.add_item(
            discord.ui.Button(
                label=value,
                style=discord.ButtonStyle.secondary,
                custom_id=f"eventbot:sandnumber:{round_id}:{value}",
                disabled=disabled,
                row=index // 5,
            )
        )
    view.add_item(
        discord.ui.Button(
            label="포기하기",
            emoji="🏳️",
            style=discord.ButtonStyle.danger,
            custom_id=f"eventbot:sandaction:{round_id}:quit",
            disabled=disabled,
            row=2,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="모래 덜기",
            emoji="🏖️",
            style=discord.ButtonStyle.primary,
            custom_id=f"eventbot:sandaction:{round_id}:remove",
            disabled=disabled,
            row=2,
        )
    )
    return view


def defuse_vote_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for color in DEFUSE_COLORS:
        view.add_item(
            discord.ui.Button(
                emoji=color["emoji"],
                style=discord.ButtonStyle.secondary,
                custom_id=f"eventbot:vote:{round_id}:{color['id']}",
            )
        )
    return view


def top3_vote_view(round_id: int, candidates: list[dict[str, Any]]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for index, candidate in enumerate(candidates, start=1):
        label = f"{index}위 {candidate['display_name']}"
        if len(label) > 80:
            label = label[:77] + "..."
        view.add_item(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.danger,
                custom_id=f"eventbot:vote:{round_id}:{candidate['user_id']}",
            )
        )
    view.add_item(
        discord.ui.Button(
            label="선택하지 않는다",
            style=discord.ButtonStyle.secondary,
            custom_id=f"eventbot:vote:{round_id}:{TOP3_NONE_CHOICE}",
        )
    )
    return view


def cutting_vote_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for index, value in enumerate(CUTTING_LEVEL_CHOICES):
        view.add_item(
            discord.ui.Button(
                label=f"{value}강화",
                style=discord.ButtonStyle.secondary,
                custom_id=f"eventbot:vote:{round_id}:{value}",
                row=0 if index < 4 else 1,
            )
        )
    return view


def cutting_action_view(round_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="세공 시도",
            style=discord.ButtonStyle.primary,
            custom_id=f"eventbot:cutting:{round_id}",
            disabled=disabled,
        )
    )
    return view


def grid_vote_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for index, value in enumerate(GRID_TILE_CHOICES):
        row = index // 3
        view.add_item(
            discord.ui.Button(
                label=value,
                style=discord.ButtonStyle.secondary,
                custom_id=f"eventbot:vote:{round_id}:{value}",
                row=row,
            )
        )
    return view


def close_time_text(close_at: float | None) -> str:
    if not close_at:
        return "수동 마감"
    return datetime.fromtimestamp(close_at).strftime("%Y-%m-%d %H:%M:%S")


def round_extra(row: sqlite3.Row) -> dict[str, Any]:
    try:
        data = json.loads(row["extra_json"])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def build_cutting_progress_embed(round_row: sqlite3.Row, extra: dict[str, Any]) -> discord.Embed:
    current_level = int(extra.get("cutting_current_level", 0))
    max_level = int(extra.get("max_level", 6))
    success_rate = cutting_success_rate(current_level) if current_level < max_level else None
    logs = [str(log) for log in extra.get("cutting_logs", [])]

    embed = discord.Embed(
        title=round_row["title"],
        description=round_row["title"],
        color=discord.Color.teal(),
    )
    embed.add_field(name="현재 단계", value=f"{current_level}강화", inline=True)
    embed.add_field(name="최대 단계", value=f"{max_level}강화", inline=True)
    embed.add_field(
        name="다음 시도 확률",
        value=f"{success_rate * 100:.0f}%" if success_rate is not None else "최대 단계 도달",
        inline=True,
    )
    embed.add_field(
        name="진행 기록",
        value="\n".join(logs[-10:]) if logs else "아직 시도 없음",
        inline=False,
    )
    embed.add_field(
        name="진행 방식",
        value="관리자가 아래 `세공 시도` 버튼을 눌러 실패할 때까지 직접 진행합니다.",
        inline=False,
    )
    return embed


def finalize_cutting_round(
    conn: sqlite3.Connection,
    round_row: sqlite3.Row,
    extra: dict[str, Any],
    result_level: int,
    logs: list[str],
    guild: discord.Guild | None,
) -> discord.Embed:
    reward_points = CUTTING_REWARD_POINTS[result_level]
    votes = conn.execute(
        "SELECT user_id, choice FROM votes WHERE round_id = ? ORDER BY updated_at ASC",
        (round_row["id"],),
    ).fetchall()
    correct_users = [row["user_id"] for row in votes if row["choice"] == str(result_level)]
    for user_id in correct_users:
        add_score(
            conn,
            user_id,
            member_name(guild, user_id),
            reward_points,
            f"라운드 #{round_row['id']} 돌 세공 {result_level}강화 예측",
            round_row["id"],
        )

    grouped: dict[str, list[str]] = {}
    for row in votes:
        grouped.setdefault(row["choice"], []).append(row["user_id"])

    extra.update(
        {
            "cutting_current_level": result_level,
            "cutting_result_level": result_level,
            "cutting_logs": logs,
            "cutting_resolved_at": now_text(),
        }
    )
    conn.execute(
        "UPDATE rounds SET extra_json = ? WHERE id = ?",
        (json.dumps(extra, ensure_ascii=False), round_row["id"]),
    )

    prediction_lines = [
        f"{level}강화: {len(grouped.get(level, []))}명"
        for level in CUTTING_LEVEL_CHOICES
    ]
    embed = discord.Embed(
        title=f"결과 · {round_row['title']}",
        description=round_row["title"],
        color=discord.Color.green(),
    )
    embed.add_field(name="세공 기록", value="\n".join(logs[-10:]), inline=False)
    embed.add_field(name="최종 단계", value=f"{result_level}강화", inline=True)
    embed.add_field(name="정답자", value=f"{len(correct_users)}명", inline=True)
    embed.add_field(name="예측 분포", value="\n".join(prediction_lines), inline=False)
    embed.add_field(name="정답자 명단", value=mention_list(correct_users), inline=False)
    embed.add_field(
        name="정산",
        value=f"{result_level}강화를 맞힌 {len(correct_users)}명 전원 +{reward_points}점",
        inline=False,
    )
    return embed


async def handle_cutting_button(interaction: discord.Interaction, round_id: int) -> None:
    if not is_operator_user(interaction.user):
        await interaction.response.send_message("이 버튼은 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'cutting'",
            (round_id,),
        ).fetchone()
        if not round_row:
            await interaction.response.send_message("존재하지 않는 돌 세공 라운드입니다.", ephemeral=True)
            return
        if round_row["status"] != "closed":
            await interaction.response.send_message("투표 집계를 먼저 마감한 뒤 진행해주세요.", ephemeral=True)
            return

        extra = round_extra(round_row)
        if "cutting_result_level" in extra:
            await interaction.response.send_message("이미 세공이 종료된 라운드입니다.", ephemeral=True)
            return

        max_level = int(extra.get("max_level", 6))
        current_level = int(extra.get("cutting_current_level", 0))
        logs = [str(log) for log in extra.get("cutting_logs", [])]

        if current_level >= max_level:
            logs.append(f"{max_level}강까지 성공, 세공 종료")
            embed = finalize_cutting_round(conn, round_row, extra, max_level, logs, interaction.guild)
            await interaction.response.edit_message(embed=embed, view=cutting_action_view(round_id, disabled=True))
            return

        next_level = current_level + 1
        success_rate = cutting_success_rate(current_level)
        if random.random() < success_rate:
            current_level = next_level
            logs.append(f"{next_level}강 시도: 성공")
            if current_level >= max_level:
                logs.append(f"{max_level}강까지 성공, 세공 종료")
                embed = finalize_cutting_round(conn, round_row, extra, current_level, logs, interaction.guild)
                await interaction.response.edit_message(embed=embed, view=cutting_action_view(round_id, disabled=True))
                return

            extra.update(
                {
                    "cutting_current_level": current_level,
                    "cutting_logs": logs,
                    "cutting_updated_at": now_text(),
                }
            )
            conn.execute(
                "UPDATE rounds SET extra_json = ? WHERE id = ?",
                (json.dumps(extra, ensure_ascii=False), round_row["id"]),
            )
            embed = build_cutting_progress_embed(round_row, extra)
            await interaction.response.edit_message(embed=embed, view=cutting_action_view(round_id))
            return

        logs.append(f"{next_level}강 시도: 실패, {current_level}강화에서 종료")
        embed = finalize_cutting_round(conn, round_row, extra, current_level, logs, interaction.guild)
        await interaction.response.edit_message(embed=embed, view=cutting_action_view(round_id, disabled=True))


def close_round_record(
    round_id: int,
    guild: discord.Guild | None,
    closed_by: str = "수동 마감",
) -> tuple[str, discord.Embed] | None:
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row or round_row["status"] != "open":
            return None

        votes = conn.execute(
            "SELECT user_id, choice FROM votes WHERE round_id = ? ORDER BY updated_at ASC",
            (round_row["id"],),
        ).fetchall()
        conn.execute(
            "UPDATE rounds SET status = 'closed', closed_at = ? WHERE id = ?",
            (now_text(), round_row["id"]),
        )

        extra = round_extra(round_row)
        if round_row["type"] == "balance":
            a_users = [row["user_id"] for row in votes if row["choice"] == "A"]
            b_users = [row["user_id"] for row in votes if row["choice"] == "B"]

            winner = None
            winner_users: list[str] = []
            if len(a_users) > len(b_users):
                winner = "A"
                winner_users = a_users
            elif len(b_users) > len(a_users):
                winner = "B"
                winner_users = b_users

            if winner:
                for user_id in winner_users:
                    add_score(
                        conn,
                        user_id,
                        member_name(guild, user_id),
                        1,
                        f"라운드 #{round_row['id']} 다수파 선택",
                        round_row["id"],
                    )

            embed = discord.Embed(
                title=f"결과 · {round_row['title']}",
                description=round_row["question"],
                color=discord.Color.green() if winner else discord.Color.light_grey(),
            )
            embed.add_field(
                name=f"A. {round_row['option_a']}",
                value=f"{len(a_users)}명\n{mention_list(a_users)}",
                inline=False,
            )
            embed.add_field(
                name=f"B. {round_row['option_b']}",
                value=f"{len(b_users)}명\n{mention_list(b_users)}",
                inline=False,
            )
            embed.add_field(
                name="정산",
                value="무승부라 점수가 지급되지 않았습니다." if not winner else f"다수파 `{winner}` 선택자 전원 +1점",
                inline=False,
            )
        elif round_row["type"] == "bomb":
            wires: dict[str, str] = extra["wires"]
            safe_wire_ids = set(extra["safe_wire_ids"])
            safe_users = [row["user_id"] for row in votes if row["choice"] in safe_wire_ids]
            is_defuse = extra.get("mode") == "defuse4"
            defuse_hint = str(extra.get("hint", "힌트 없음"))

            for user_id in safe_users:
                add_score(
                    conn,
                    user_id,
                    member_name(guild, user_id),
                    1,
                    f"라운드 #{round_row['id']} 안전선 선택",
                    round_row["id"],
                )

            grouped: dict[str, list[str]] = {}
            for row in votes:
                grouped.setdefault(row["choice"], []).append(row["user_id"])

            embed = discord.Embed(
                title=f"결과 · {round_row['title']}",
                description=round_row["title"],
                color=discord.Color.green(),
            )
            embed.add_field(
                name="해체 성공 선" if is_defuse else "안전선",
                value=", ".join(wires[wire_id] for wire_id in extra["safe_wire_ids"]),
                inline=False,
            )
            if is_defuse:
                embed.add_field(name="관리자의 힌트", value=defuse_hint, inline=False)
            for wire_id, wire_name in wires.items():
                users = grouped.get(wire_id, [])
                marker = "안전" if wire_id in safe_wire_ids else "폭탄"
                embed.add_field(
                    name=f"{wire_name} ({marker})",
                    value=f"{len(users)}명\n{mention_list(users)}",
                    inline=False,
                )
            embed.add_field(
                name="정산",
                value=f"{'해체 성공자' if is_defuse else '안전선 선택자'} {len(safe_users)}명 전원 +1점",
                inline=False,
            )
        elif round_row["type"] == "grid":
            target_tile = str(extra["target_tile"])
            admin_clue = str(extra.get("admin_clue", round_row["question"]))
            reward_points = int(extra.get("reward_points", GRID_REWARD_POINTS))
            correct_users = [row["user_id"] for row in votes if row["choice"] == target_tile]

            for user_id in correct_users:
                add_score(
                    conn,
                    user_id,
                    member_name(guild, user_id),
                    reward_points,
                    f"라운드 #{round_row['id']} 관리자 시점 타일 정답",
                    round_row["id"],
                )

            grouped: dict[str, list[str]] = {}
            for row in votes:
                grouped.setdefault(row["choice"], []).append(row["user_id"])

            grid_lines = []
            for row_start in range(1, 10, 3):
                cells = []
                for tile in range(row_start, row_start + 3):
                    tile_text = str(tile)
                    count = len(grouped.get(tile_text, []))
                    marker = "정답" if tile_text == target_tile else f"{count}명"
                    cells.append(f"{tile_text}({marker})")
                grid_lines.append(" | ".join(cells))

            embed = discord.Embed(
                title=f"결과 · {round_row['title']}",
                description=round_row["title"],
                color=discord.Color.green(),
            )
            embed.add_field(name="관리자의 선택 이유", value=admin_clue, inline=False)
            embed.add_field(name="정답 칸", value=f"{target_tile}번", inline=True)
            embed.add_field(name="정답자", value=f"{len(correct_users)}명", inline=True)
            embed.add_field(name="타일별 선택", value="\n".join(grid_lines), inline=False)
            embed.add_field(name="정답자 명단", value=mention_list(correct_users), inline=False)
            embed.add_field(name="정산", value=f"정답자 {len(correct_users)}명 전원 +{reward_points}점", inline=False)
        elif round_row["type"] == "trust":
            vote_choices = {str(row["user_id"]): str(row["choice"]) for row in votes}
            pairs = conn.execute(
                """
                SELECT user_a_id, user_b_id FROM trust_pairings
                WHERE round_id = ?
                ORDER BY created_at ASC
                """,
                (round_row["id"],),
            ).fetchall()
            outcome_counts = {choice: 0 for choice in TRUST_OUTCOME_LABELS}
            result_lines: list[str] = []

            for pair in pairs:
                user_a_id = str(pair["user_a_id"])
                user_b_id = str(pair["user_b_id"])
                user_a_choice = vote_choices.get(user_a_id)
                user_b_choice = vote_choices.get(user_b_id)
                if not user_a_choice or not user_b_choice:
                    result_lines.append(f"<@{user_a_id}> ↔ <@{user_b_id}>: 미선택 (0점)")
                    continue

                if user_a_choice == "trust" and user_b_choice == "trust":
                    outcome = "mutual"
                    for user_id in (user_a_id, user_b_id):
                        add_score(
                            conn,
                            user_id,
                            member_name(guild, user_id),
                            1,
                            f"라운드 #{round_row['id']} 신뢰 거래 상호 신뢰",
                            round_row["id"],
                        )
                    result_lines.append(f"<@{user_a_id}> ↔ <@{user_b_id}>: 상호 신뢰 (각 +1점)")
                elif user_a_choice == "betray" and user_b_choice == "betray":
                    outcome = "double"
                    for user_id in (user_a_id, user_b_id):
                        add_score(
                            conn,
                            user_id,
                            member_name(guild, user_id),
                            -1,
                            f"라운드 #{round_row['id']} 신뢰 거래 동시 배신",
                            round_row["id"],
                        )
                    result_lines.append(f"<@{user_a_id}> ↔ <@{user_b_id}>: 동시 배신 (각 -1점)")
                else:
                    outcome = "single"
                    betrayer_id = user_a_id if user_a_choice == "betray" else user_b_id
                    victim_id = user_b_id if betrayer_id == user_a_id else user_a_id
                    add_score(
                        conn,
                        betrayer_id,
                        member_name(guild, betrayer_id),
                        2,
                        f"라운드 #{round_row['id']} 신뢰 거래 단독 배신",
                        round_row["id"],
                    )
                    result_lines.append(
                        f"<@{betrayer_id}> → <@{victim_id}>: 단독 배신 (배신자 +2점, 상대 0점)"
                    )
                outcome_counts[outcome] += 1

            extra.update({"trust_phase": "scored", "trust_scored_at": now_text()})
            conn.execute(
                "UPDATE rounds SET extra_json = ? WHERE id = ?",
                (json.dumps(extra, ensure_ascii=False), round_row["id"]),
            )
            embed = discord.Embed(
                title=f"결과 · {round_row['title']}",
                description=round_row["title"],
                color=discord.Color.gold(),
            )
            embed.add_field(name="짝별 결과", value=field_text(result_lines), inline=False)
            embed.add_field(
                name="전체 결과 분포",
                value=" · ".join(
                    f"{TRUST_OUTCOME_LABELS[choice]} {count}쌍"
                    for choice, count in outcome_counts.items()
                ),
                inline=False,
            )
            unmatched_user_id = str(extra.get("unmatched_user_id") or "")
            if unmatched_user_id:
                embed.add_field(
                    name="매칭 대기",
                    value=f"<@{unmatched_user_id}>님은 참여자가 홀수여서 이번 회차에 짝이 배정되지 않았습니다.",
                    inline=False,
                )
            embed.add_field(
                name="정산",
                value="상호 신뢰는 각 +1점, 단독 배신은 배신자 +2점·상대 0점, 동시 배신은 각 -1점입니다.",
                inline=False,
            )
        elif round_row["type"] == "sand":
            result_lines: list[str] = []
            completed_count = 0
            for pair in extra.get("sand_pairs", []):
                user_a_id = str(pair["user_a_id"])
                user_b_id = str(pair["user_b_id"])
                outcome = str(pair.get("outcome") or "")
                result = pair.get("result", {})
                number_a = pair.get("number_a")
                number_b = pair.get("number_b")
                number_text = (
                    f"선택 {number_a}/{number_b}, 차이 {abs(int(number_a) - int(number_b))}"
                    if number_a is not None and number_b is not None
                    else "숫자 선택 미완료"
                )

                if outcome == "mutual_quit":
                    for user_id in (user_a_id, user_b_id):
                        add_score(
                            conn, user_id, member_name(guild, user_id), -1,
                            f"라운드 #{round_row['id']} 모래성 둘 다 포기", round_row["id"],
                        )
                    settlement = "각 -1점"
                elif outcome == "quit_success":
                    winner_id = str(result["winner"])
                    quitter_id = str(result["quitter"])
                    add_score(
                        conn, winner_id, member_name(guild, winner_id), 2,
                        f"라운드 #{round_row['id']} 모래성 포기 후 덜기 성공", round_row["id"],
                    )
                    add_score(
                        conn, quitter_id, member_name(guild, quitter_id), -1,
                        f"라운드 #{round_row['id']} 모래성 단독 포기", round_row["id"],
                    )
                    settlement = f"<@{winner_id}> +2점 · <@{quitter_id}> -1점"
                elif outcome == "quit_collapse":
                    winner_id = str(result["winner"])
                    collapsed_user_id = str(result["collapsed_user"])
                    add_score(
                        conn, winner_id, member_name(guild, winner_id), 1,
                        f"라운드 #{round_row['id']} 모래성 상대 붕괴", round_row["id"],
                    )
                    add_score(
                        conn, collapsed_user_id, member_name(guild, collapsed_user_id), -1,
                        f"라운드 #{round_row['id']} 모래성 마지막 덜기 붕괴", round_row["id"],
                    )
                    settlement = f"<@{winner_id}> +1점 · <@{collapsed_user_id}> -1점"
                elif outcome == "plain_collapse":
                    collapsed_user_id = str(result["collapsed_user"])
                    add_score(
                        conn, collapsed_user_id, member_name(guild, collapsed_user_id), -1,
                        f"라운드 #{round_row['id']} 모래성 일반 붕괴", round_row["id"],
                    )
                    settlement = f"<@{collapsed_user_id}> -1점"
                else:
                    result_lines.append(
                        f"<@{user_a_id}> ↔ <@{user_b_id}>: 제한 시간 내 미완료 (0점)"
                    )
                    continue

                completed_count += 1
                result_lines.append(
                    f"<@{user_a_id}> ↔ <@{user_b_id}>: {SAND_OUTCOME_LABELS[outcome]} "
                    f"({number_text}) · {settlement}"
                )

            extra.update({"sand_phase": "scored", "sand_scored_at": now_text()})
            conn.execute(
                "UPDATE rounds SET extra_json = ? WHERE id = ?",
                (json.dumps(extra, ensure_ascii=False), round_row["id"]),
            )
            embed = discord.Embed(
                title=f"결과 · {round_row['title']}",
                description=round_row["title"],
                color=discord.Color.from_rgb(218, 165, 90),
            )
            embed.add_field(name="짝별 결과", value=field_text(result_lines), inline=False)
            embed.add_field(
                name="정산 완료",
                value=f"완료된 1:1 게임 {completed_count}쌍의 점수를 반영했습니다.",
                inline=False,
            )
            unmatched_user_id = str(extra.get("unmatched_user_id") or "")
            if unmatched_user_id:
                embed.add_field(
                    name="매칭 대기",
                    value=f"<@{unmatched_user_id}>님은 이번 회차에 짝이 없어 점수 변동이 없습니다.",
                    inline=False,
                )
        elif round_row["type"] == "cutting":
            grouped: dict[str, list[str]] = {}
            for row in votes:
                grouped.setdefault(row["choice"], []).append(row["user_id"])

            lines = []
            for level in CUTTING_LEVEL_CHOICES:
                users = grouped.get(level, [])
                lines.append(f"{level}강화: {len(users)}명")

            embed = discord.Embed(
                title=f"집계 · {round_row['title']}",
                description=round_row["title"],
                color=discord.Color.teal(),
            )
            embed.add_field(name="강화 단계별 예측", value="\n".join(lines), inline=False)
            embed.add_field(
                name="다음 진행",
                value="운영자가 `/세공진행`을 실행하면 관리자 전용 세공 버튼이 열립니다. 버튼을 눌러 실패할 때까지 진행하면 결과와 정산이 공개됩니다.",
                inline=False,
            )
        else:
            candidates: list[dict[str, Any]] = extra["candidates"]
            candidate_ids = [str(candidate["user_id"]) for candidate in candidates]
            grouped: dict[str, list[str]] = {}
            for row in votes:
                grouped.setdefault(row["choice"], []).append(row["user_id"])

            none_votes = len(grouped.get(TOP3_NONE_CHOICE, []))
            candidate_vote_sum = sum(len(grouped.get(user_id, [])) for user_id in candidate_ids)
            candidate_lines = []
            for candidate in candidates:
                user_id = str(candidate["user_id"])
                candidate_lines.append(
                    f"<@{user_id}>: {len(grouped.get(user_id, []))}표"
                )

            if none_votes > candidate_vote_sum:
                if round_row["event_session_id"] is None:
                    winners = conn.execute(
                        """
                        SELECT user_id, display_name FROM users
                        WHERE user_id NOT IN (?, ?, ?)
                        ORDER BY total_score DESC, updated_at ASC
                        """,
                        tuple(candidate_ids),
                    ).fetchall()
                else:
                    winners = conn.execute(
                        """
                        SELECT users.user_id, users.display_name
                        FROM event_participants
                        JOIN users ON users.user_id = event_participants.user_id
                        WHERE event_participants.event_session_id = ?
                          AND users.user_id NOT IN (?, ?, ?)
                        ORDER BY users.total_score DESC, users.updated_at ASC
                        """,
                        (round_row["event_session_id"], *candidate_ids),
                    ).fetchall()
                for winner in winners:
                    add_score(
                        conn,
                        str(winner["user_id"]),
                        str(winner["display_name"]),
                        1,
                        f"라운드 #{round_row['id']} 상위 3명 견제 성공",
                        round_row["id"],
                    )
                outcome = "`선택하지 않는다`가 후보 득표 합보다 많았습니다."
                settlement = f"상위 3명을 제외한 {len(winners)}명 전원 +1점"
            else:
                target = max(
                    candidates,
                    key=lambda candidate: len(grouped.get(str(candidate["user_id"]), [])),
                )
                add_score(
                    conn,
                    str(target["user_id"]),
                    str(target["display_name"]),
                    -1,
                    f"라운드 #{round_row['id']} 상위 3명 견제 피격",
                    round_row["id"],
                )
                outcome = "세 후보 득표 합이 `선택하지 않는다` 이상이었습니다."
                settlement = f"<@{target['user_id']}> -1점"

            embed = discord.Embed(
                title=f"결과 · {round_row['title']}",
                description=round_row["title"],
                color=discord.Color.green(),
            )
            embed.add_field(name="후보별 득표", value="\n".join(candidate_lines), inline=False)
            embed.add_field(name="선택하지 않는다", value=f"{none_votes}표", inline=True)
            embed.add_field(name="후보 득표 합", value=f"{candidate_vote_sum}표", inline=True)
            embed.add_field(name="결과", value=outcome, inline=False)
            embed.add_field(name="정산", value=settlement, inline=False)

        embed.add_field(name="통계", value=f"투표 참여: {len(votes)}명", inline=False)
        planned_mini_game = str(extra.get("planned_mini_game") or "")
        if round_row["type"] in MAIN_GAME_TYPES and planned_mini_game in MINI_GAME_TYPES:
            embed.add_field(
                name="다음 미니게임",
                value=f"처음 선택한 `{GAME_NAMES[planned_mini_game]}`의 설정 버튼을 눌러 시작해주세요.",
                inline=False,
            )
        embed.set_footer(text=f"시작: {round_row['created_at']} · 종료: {now_text()}")

        return str(round_row["channel_id"]), embed


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    message = "명령어 처리 중 오류가 발생했습니다."
    if isinstance(error, app_commands.CheckFailure):
        message = str(error)

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def parse_duration_minutes(value: str) -> int:
    value = value.strip()
    if not value.isdigit():
        raise ValueError("진행 시간은 숫자로 입력해주세요.")
    minutes = int(value)
    if minutes < 1 or minutes > 1440:
        raise ValueError("진행 시간은 1분부터 1440분 사이로 입력해주세요.")
    return minutes


def parse_defuse_color(value: str) -> str:
    normalized = value.strip().lower().replace(" ", "")
    color_id = DEFUSE_COLOR_ALIASES.get(normalized)
    if not color_id:
        raise ValueError("안전 색은 빨강, 파랑, 검정, 흰색 중 하나로 입력해주세요.")
    return color_id


def parse_range_int(value: str, name: str, minimum: int, maximum: int) -> int:
    value = value.strip()
    if not value.isdigit():
        raise ValueError(f"{name}은 숫자로 입력해주세요.")
    number = int(value)
    if number < minimum or number > maximum:
        raise ValueError(f"{name}은 {minimum}부터 {maximum} 사이로 입력해주세요.")
    return number


async def ensure_trust_thread_message_permission(interaction: discord.Interaction) -> bool:
    guild = interaction.guild
    interaction_channel = interaction.channel
    parent_channel = interaction_channel.parent if isinstance(interaction_channel, discord.Thread) else interaction_channel
    if guild is None or not isinstance(parent_channel, discord.TextChannel):
        await interaction.response.send_message(
            "신뢰 거래는 서버 텍스트 채널에서 시작해주세요.",
            ephemeral=True,
        )
        return False

    default_role = guild.default_role
    overwrite = parent_channel.overwrites_for(default_role)
    if overwrite.send_messages_in_threads is True:
        return True

    bot_member = guild.me
    if bot_member is None or not parent_channel.permissions_for(bot_member).manage_roles:
        await interaction.response.send_message(
            "신뢰 거래 협상방을 열려면 봇에 `역할 관리(Manage Roles)` 권한이 필요합니다. "
            "권한을 추가한 뒤 다시 시작해주세요.",
            ephemeral=True,
        )
        return False

    overwrite.send_messages_in_threads = True
    try:
        await parent_channel.set_permissions(
            default_role,
            overwrite=overwrite,
            reason="신뢰 거래 공개 협상방 메시지 허용",
        )
    except (discord.Forbidden, discord.HTTPException):
        await interaction.response.send_message(
            "이벤트 채널의 `스레드에서 메시지 보내기` 권한을 설정하지 못했습니다. "
            "봇의 역할 관리 권한과 역할 순서를 확인해주세요.",
            ephemeral=True,
        )
        return False
    return True


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
            max_length=120,
        )
        self.duration = discord.ui.TextInput(
            label="매칭 준비 시간(분)" if game in MAIN_GAME_TYPES else "진행 시간(분)",
            placeholder="예: 2" if game in MAIN_GAME_TYPES else "예: 5",
            default=str(TRUST_REGISTRATION_DEFAULT_MINUTES) if game in MAIN_GAME_TYPES else "5",
            max_length=4,
        )
        self.add_item(self.game_title)
        self.add_item(self.duration)

        if game == "balance":
            self.option_a = discord.ui.TextInput(label="A 선택지", max_length=80)
            self.option_b = discord.ui.TextInput(label="B 선택지", max_length=80)
            self.add_item(self.option_a)
            self.add_item(self.option_b)
        elif game == "defuse4":
            self.safe_color = discord.ui.TextInput(
                label="안전 색",
                placeholder="빨강, 파랑, 검정, 흰색 중 하나",
                max_length=10,
            )
            self.defuse_hint = discord.ui.TextInput(
                label="관리자의 힌트",
                placeholder="예: 차가운 쪽이 안전하다",
                style=discord.TextStyle.paragraph,
                max_length=500,
            )
            self.add_item(self.safe_color)
            self.add_item(self.defuse_hint)
        elif game == "grid":
            self.target_tile = discord.ui.TextInput(
                label="정답 칸",
                placeholder="1~9",
                max_length=1,
            )
            self.admin_clue = discord.ui.TextInput(
                label="관리자의 선택 이유",
                placeholder="나는 이런 이유로 이 칸을 골랐다",
                style=discord.TextStyle.paragraph,
                max_length=800,
            )
            self.add_item(self.target_tile)
            self.add_item(self.admin_clue)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            duration = parse_duration_minutes(str(self.duration.value))
            title = str(self.game_title.value).strip()
            if not title:
                raise ValueError("제목을 입력해주세요.")

            if self.game == "balance":
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
            elif self.game == "defuse4":
                await create_round_from_setup(
                    interaction,
                    self.game,
                    duration,
                    title,
                    안전색=parse_defuse_color(str(self.safe_color.value)),
                    힌트=str(self.defuse_hint.value).strip(),
                    예정_미니게임=self.planned_mini_game,
                    단독_미니게임=self.standalone_mini_game,
                )
            elif self.game == "grid":
                await create_round_from_setup(
                    interaction,
                    self.game,
                    duration,
                    title,
                    정답칸=parse_range_int(str(self.target_tile.value), "정답 칸", 1, 9),
                    관리자설명=str(self.admin_clue.value).strip(),
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


@bot.tree.command(name="오픈", description="새 이벤트 참가 모집을 열고 진행 흐름을 안내합니다.")
@app_commands.default_permissions()
@admin_only()
async def open_event(interaction: discord.Interaction) -> None:
    if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("서버 텍스트 채널에서만 사용할 수 있습니다.", ephemeral=True)
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


@bot.tree.command(name="start", description="메인게임과 미니게임을 골라 이벤트를 시작합니다.")
@app_commands.default_permissions()
@admin_only()
@app_commands.choices(
    메인게임=[
        app_commands.Choice(name="신뢰 거래", value="trust"),
        app_commands.Choice(name="모래성 게임", value="sand"),
    ],
    미니게임=[
        app_commands.Choice(name="일반 밸런스", value="balance"),
        app_commands.Choice(name="4선 폭탄 해체", value="defuse4"),
        app_commands.Choice(name="관리자 시점 타일", value="grid"),
        app_commands.Choice(name="돌 세공 예측", value="cutting"),
        app_commands.Choice(name="상위 3명 견제", value="top3"),
    ],
)
@app_commands.describe(메인게임="먼저 진행할 메인게임", 미니게임="메인게임 뒤에 진행할 미니게임")
async def start(interaction: discord.Interaction, 메인게임: str, 미니게임: str) -> None:
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


@bot.tree.command(name="미니게임", description="메인게임 없이 선택한 미니게임만 시작합니다.")
@app_commands.default_permissions()
@admin_only()
@app_commands.choices(
    게임=[
        app_commands.Choice(name="일반 밸런스", value="balance"),
        app_commands.Choice(name="4선 폭탄 해체", value="defuse4"),
        app_commands.Choice(name="관리자 시점 타일", value="grid"),
        app_commands.Choice(name="돌 세공 예측", value="cutting"),
        app_commands.Choice(name="상위 3명 견제", value="top3"),
    ]
)
@app_commands.describe(게임="바로 시작할 미니게임")
async def start_mini_game(interaction: discord.Interaction, 게임: str) -> None:
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
    정답칸: int | None = None,
    관리자설명: str | None = None,
    예정_미니게임: str | None = None,
    단독_미니게임: bool = False,
) -> None:
    if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("서버 텍스트 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return

    if 게임 == "trust" and not await ensure_trust_thread_message_permission(interaction):
        return

    close_at = time.time() + (int(진행시간분) * 60)
    extra: dict[str, Any] = {
        "auto_close_at": close_at,
        "duration_minutes": int(진행시간분),
    }
    if 게임 in MAIN_GAME_TYPES:
        if 예정_미니게임 not in MINI_GAME_TYPES:
            await interaction.response.send_message("시작할 미니게임을 함께 선택해주세요.", ephemeral=True)
            return
        extra["planned_mini_game"] = 예정_미니게임

    with db() as conn:
        if get_open_round(conn):
            await interaction.response.send_message("이미 열린 라운드가 있습니다. 먼저 `/라운드마감`을 해주세요.", ephemeral=True)
            return

        event_row = get_current_event(conn)
        if not event_row:
            await interaction.response.send_message(
                "현재 열린 이벤트가 없습니다. 먼저 `/오픈`을 실행해주세요.",
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
            await interaction.response.send_message(
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
            await interaction.response.send_message(
                "이번 이벤트의 게임 조합은 이미 확정되었습니다. 다음 이벤트는 `/오픈`부터 다시 시작해주세요.",
                ephemeral=True,
            )
            return
        if previous_round and previous_round["type"] == "cutting":
            previous_extra = round_extra(previous_round)
            if "cutting_result_level" not in previous_extra:
                await interaction.response.send_message(
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
                await interaction.response.send_message(
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
                await interaction.response.send_message(
                    "미니게임을 시작하려면 참여자 모집·매칭·선택·정산까지 끝난 메인게임이 필요합니다.",
                    ephemeral=True,
                )
                return

        if 게임 == "balance":
            if not 선택_a or not 선택_b:
                await interaction.response.send_message(
                    "일반 밸런스 게임은 `선택_a`와 `선택_b`가 필요합니다.",
                    ephemeral=True,
                )
                return

            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, option_a, option_b, status, channel_id, extra_json, created_at)
                VALUES ('balance', ?, ?, ?, ?, 'open', ?, ?, ?)
                """,
                (
                    "일반 밸런스 라운드",
                    제목,
                    선택_a,
                    선택_b,
                    str(interaction.channel.id),
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_balance_embed(round_id, 제목, 선택_a, 선택_b)
            view = vote_view(round_id, [("A 선택", "A"), ("B 선택", "B")])
        elif 게임 == "defuse4":
            if not 안전색:
                await interaction.response.send_message(
                    "4선 폭탄 해체 게임은 안전 색이 필요합니다.",
                    ephemeral=True,
                )
                return
            if not 힌트:
                await interaction.response.send_message(
                    "4선 폭탄 해체 게임은 관리자의 힌트가 필요합니다.",
                    ephemeral=True,
                )
                return
            wire_map = {
                color["id"]: f"{color['emoji']} {color['name']}"
                for color in DEFUSE_COLORS
            }
            safe_wire_ids = [안전색]
            extra.update(
                {
                    "mode": "defuse4",
                    "wires": wire_map,
                    "safe_wire_ids": safe_wire_ids,
                    "hint": 힌트,
                }
            )

            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('bomb', ?, ?, 'open', ?, ?, ?)
                """,
                (
                    제목,
                    제목,
                    str(interaction.channel.id),
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_defuse_embed(round_id, 제목, 힌트)
            view = defuse_vote_view(round_id)
        elif 게임 == "grid":
            if 정답칸 is None or not 관리자설명:
                await interaction.response.send_message(
                    "관리자 시점 타일 게임은 `정답칸`과 `관리자설명`이 필요합니다.",
                    ephemeral=True,
                )
                return

            extra.update(
                {
                    "target_tile": str(int(정답칸)),
                    "admin_clue": 관리자설명,
                    "reward_points": GRID_REWARD_POINTS,
                }
            )
            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('grid', ?, ?, 'open', ?, ?, ?)
                """,
                (
                    제목,
                    관리자설명,
                    str(interaction.channel.id),
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_grid_embed(round_id, 제목, 관리자설명, GRID_REWARD_POINTS)
            view = grid_vote_view(round_id)
        elif 게임 == "trust":
            extra.update(
                {
                    "trust_phase": "registration",
                    "registration_minutes": int(진행시간분),
                    "registration_close_at": close_at,
                }
            )
            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('trust', ?, ?, 'open', ?, ?, ?)
                """,
                (
                    제목,
                    제목,
                    str(interaction.channel.id),
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_trust_registration_embed(round_id, 제목, int(진행시간분))
            view = None
        elif 게임 == "sand":
            extra.update(
                {
                    "sand_phase": "registration",
                    "registration_minutes": int(진행시간분),
                    "registration_close_at": close_at,
                }
            )
            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('sand', ?, ?, 'open', ?, ?, ?)
                """,
                (
                    제목,
                    제목,
                    str(interaction.channel.id),
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_sand_registration_embed(round_id, 제목, int(진행시간분))
            view = None
        elif 게임 == "top3":
            rows = conn.execute(
                """
                SELECT users.user_id, users.display_name, users.total_score
                FROM event_participants
                JOIN users ON users.user_id = event_participants.user_id
                WHERE event_participants.event_session_id = ?
                ORDER BY users.total_score DESC, users.updated_at ASC
                LIMIT 3
                """,
                (event_session_id,),
            ).fetchall()
            if len(rows) < 3:
                await interaction.response.send_message(
                    "상위 3명 견제 게임은 이번 이벤트 참가자 중 점수 기록 유저가 최소 3명 필요합니다.",
                    ephemeral=True,
                )
                return

            candidates = [
                {
                    "user_id": str(row["user_id"]),
                    "display_name": str(row["display_name"]),
                    "score": int(row["total_score"]),
                }
                for row in rows
            ]
            extra.update({"candidates": candidates})
            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('top3', ?, ?, 'open', ?, ?, ?)
                """,
                (
                    제목,
                    제목,
                    str(interaction.channel.id),
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_top3_embed(round_id, 제목, candidates)
            view = top3_vote_view(round_id, candidates)
        else:
            extra.update({"max_level": 6})
            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('cutting', ?, ?, 'open', ?, ?, ?)
                """,
                (
                    제목,
                    제목,
                    str(interaction.channel.id),
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_cutting_embed(round_id, 제목)
            view = cutting_vote_view(round_id)

        conn.execute(
            "UPDATE rounds SET event_session_id = ? WHERE id = ?",
            (event_session_id, round_id),
        )
        conn.execute(
            "UPDATE event_sessions SET status = 'active' WHERE id = ?",
            (event_session_id,),
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

    if 게임 in MAIN_GAME_TYPES:
        next_step = (
            "매칭 후에는 신뢰/배신 선택 시간이 3분 주어집니다."
            if 게임 == "trust"
            else "매칭 후에는 비공개 숫자 선택과 1:1 진행 시간이 10분 주어집니다."
        )
        if 게임 == "sand":
            embed.add_field(
                name="게임 준비 중",
                value=(
                    f"현재는 모든 참가자에게 공개되는 규칙 안내 단계입니다. "
                    f"{진행시간분}분 후 자동으로 1:1 매칭과 실제 게임을 시작합니다.\n"
                    f"실제 시작 예정: {close_time_text(close_at)}\n{next_step}"
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="랜덤 매칭 시작",
                value=(
                    f"오늘의 이벤트 참가 명단으로 {진행시간분}분 후 랜덤 매칭을 시작합니다.\n"
                    f"예정 시각: {close_time_text(close_at)}\n{next_step}"
                ),
                inline=False,
            )
        embed.add_field(
            name="선택한 미니게임",
            value=f"메인게임 정산 후 `{GAME_NAMES[str(예정_미니게임)]}` 설정 버튼이 표시됩니다.",
            inline=False,
        )
    else:
        embed.add_field(
            name="자동 마감",
            value=f"{진행시간분}분 후 자동으로 종료됩니다.\n예정 시각: {close_time_text(close_at)}",
            inline=False,
        )
    await interaction.response.send_message(embed=embed, view=view)
    message = await interaction.original_response()

    discussion_thread_id = None
    if 게임 in {"balance", "grid", "defuse4", "trust"}:
        try:
            thread_name = f"{제목} | 토론방"[:100]
            thread = await message.create_thread(name=thread_name)
            if 게임 == "trust":
                await thread.send(
                    "이곳은 작성자가 표시되는 공개 협상방입니다. 참여 등록과 랜덤 매칭이 끝나면 "
                    "배정된 상대와 신뢰 또는 배신을 두고 협상하세요.",
                )
                notice_embed = build_thread_notice_embed(
                    round_id,
                    thread,
                    "거래 협상방이 열렸습니다",
                    "참여 등록 뒤 봇이 무작위로 둘씩 매칭합니다. 매칭 후 공개 협상방에서 상대와 대화할 수 있습니다.",
                )
            elif 게임 == "balance":
                await thread.send(
                    "아래 버튼으로 익명 발언을 남겨주세요. 일반 메시지는 익명 보호를 위해 삭제됩니다. "
                    "단, 한 사람당 한 번만 발언 가능합니다.",
                    view=anonymous_discussion_view(round_id),
                )
                notice_embed = build_thread_notice_embed(
                    round_id,
                    thread,
                    "토론방이 열렸습니다",
                    "버튼을 눌러 익명으로 설득, 블러핑, 진실 말하기를 할 수 있습니다. 한 사람당 한 번만 발언 가능합니다.",
                )
            elif 게임 == "grid":
                await thread.send(
                    "아래 버튼으로 익명 의견을 남겨주세요. 일반 메시지는 익명 보호를 위해 삭제됩니다. "
                    "단, 한 사람당 한 번만 의견을 남길 수 있습니다.",
                    view=anonymous_discussion_view(round_id),
                )
                notice_embed = build_thread_notice_embed(
                    round_id,
                    thread,
                    "타일 추리방이 열렸습니다",
                    "버튼을 눌러 익명으로 관리자의 선택 이유를 해석해 정답 칸을 추리해보세요. 분탕, 블러핑, 역추리 모두 가능합니다.",
                )
            else:
                await thread.send(
                    "아래 버튼으로 익명 의견을 남겨주세요. 일반 메시지는 익명 보호를 위해 삭제됩니다. "
                    "단, 한 사람당 한 번만 의견을 남길 수 있습니다.",
                    view=anonymous_discussion_view(round_id),
                )
                notice_embed = build_thread_notice_embed(
                    round_id,
                    thread,
                    "해체 토론방이 열렸습니다",
                    "버튼을 눌러 익명으로 관리자의 힌트를 해석해 안전 색을 추리해보세요. 블러핑과 역추리 모두 가능합니다.",
                )
            if 게임 != "trust":
                try:
                    await thread.edit(
                        archived=True,
                        locked=True,
                        reason="익명 미니게임 토론방: 일반 채팅 차단",
                    )
                except discord.HTTPException:
                    # 권한이 부족한 경우에도 on_message의 삭제 처리로 익명성을 최대한 보호합니다.
                    pass
            await interaction.followup.send(embed=notice_embed)
            discussion_thread_id = str(thread.id)
        except discord.HTTPException:
            discussion_thread_id = None

    with db() as conn:
        conn.execute(
            """
            UPDATE rounds
            SET message_id = ?, discussion_thread_id = ?
            WHERE id = ?
            """,
            (str(message.id), discussion_thread_id, round_id),
        )

    bot.schedule_auto_close(round_id, close_at)


@bot.tree.command(name="라운드마감", description="현재 열린 라운드를 마감하고 결과를 공개합니다.")
@app_commands.default_permissions()
@admin_only()
async def close_round(interaction: discord.Interaction) -> None:
    with db() as conn:
        round_row = get_open_round(conn)
    if not round_row:
        await interaction.response.send_message("현재 열린 라운드가 없습니다.", ephemeral=True)
        return

    if round_row["type"] == "trust" and round_extra(round_row).get("trust_phase") == "registration":
        transition = start_trust_decision(int(round_row["id"]))
        if not transition:
            await interaction.response.send_message("참여자 모집을 마감하지 못했습니다.", ephemeral=True)
            return
        task = bot.auto_close_tasks.pop(int(round_row["id"]), None)
        if task:
            task.cancel()
        await update_trust_main_message(transition)
        if transition["state"] == "decision":
            bot.schedule_auto_close(int(round_row["id"]), float(transition["close_at"]))
            await interaction.response.send_message("참여자 모집을 마감하고 랜덤 매칭 및 3분 선택을 시작했습니다.", ephemeral=True)
        else:
            await interaction.response.send_message("참여자가 부족해 신뢰 거래를 취소했습니다.", ephemeral=True)
        return

    if round_row["type"] == "sand" and round_extra(round_row).get("sand_phase") == "registration":
        transition = start_sand_game(int(round_row["id"]))
        if not transition:
            await interaction.response.send_message("참여자 모집을 마감하지 못했습니다.", ephemeral=True)
            return
        task = bot.auto_close_tasks.pop(int(round_row["id"]), None)
        if task:
            task.cancel()
        await update_trust_main_message(transition)
        if transition["state"] == "playing":
            bot.schedule_auto_close(int(round_row["id"]), float(transition["close_at"]))
            await interaction.response.send_message("참여자 모집을 마감하고 1:1 모래성 게임을 시작했습니다.", ephemeral=True)
        else:
            await interaction.response.send_message("참여자가 부족해 모래성 게임을 취소했습니다.", ephemeral=True)
        return

    result = close_round_record(int(round_row["id"]), interaction.guild)
    if not result:
        await interaction.response.send_message("현재 열린 라운드가 없습니다.", ephemeral=True)
        return

    task = bot.auto_close_tasks.pop(int(round_row["id"]), None)
    if task:
        task.cancel()

    _, embed = result
    await interaction.response.send_message(
        embed=embed,
        view=planned_mini_game_view(int(round_row["id"])),
    )


@bot.tree.command(name="이벤트종료", description="개발 중인 현재 이벤트를 남은 게임과 관계없이 강제로 종료합니다.")
@app_commands.default_permissions()
@admin_only()
async def force_end_event(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    with db() as conn:
        result = force_close_current_event(conn)
    if not result:
        await interaction.followup.send("현재 열려 있는 이벤트가 없습니다.", ephemeral=True)
        return

    event_row, round_rows = result
    open_round_count = sum(1 for row in round_rows if row["status"] == "open")

    for round_row in round_rows:
        task = bot.auto_close_tasks.pop(int(round_row["id"]), None)
        if task:
            task.cancel()

        channel_id = round_row["channel_id"]
        message_id = round_row["message_id"]
        if channel_id and message_id:
            try:
                channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
                if hasattr(channel, "fetch_message"):
                    message = await channel.fetch_message(int(message_id))
                    await message.edit(view=None)
            except (discord.HTTPException, ValueError):
                pass

        thread_id = round_row["discussion_thread_id"]
        if thread_id:
            try:
                thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
                if isinstance(thread, discord.Thread):
                    await thread.edit(
                        archived=True,
                        locked=True,
                        reason="개발용 이벤트 강제 종료",
                    )
            except (discord.HTTPException, ValueError):
                pass

    if event_row["message_id"]:
        try:
            channel = bot.get_channel(int(event_row["channel_id"])) or await bot.fetch_channel(
                int(event_row["channel_id"])
            )
            if hasattr(channel, "fetch_message"):
                message = await channel.fetch_message(int(event_row["message_id"]))
                await message.edit(view=None)
        except (discord.HTTPException, ValueError):
            pass

    await interaction.followup.send(
        f"이벤트 #{event_row['id']}을 강제로 종료했습니다. "
        f"미완료 라운드 {open_round_count}개는 점수 정산 없이 취소했습니다.",
        ephemeral=True,
    )


@bot.tree.command(name="세공진행", description="가장 최근 마감된 돌 세공 예측의 관리자 진행 패널을 엽니다.")
@app_commands.default_permissions()
@admin_only()
async def run_cutting(interaction: discord.Interaction) -> None:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM rounds
            WHERE type = 'cutting' AND status = 'closed'
            ORDER BY id DESC
            """
        ).fetchall()

        round_row = None
        extra: dict[str, Any] = {}
        for row in rows:
            parsed_extra = round_extra(row)
            if "cutting_result_level" not in parsed_extra and not parsed_extra.get("force_closed"):
                round_row = row
                extra = parsed_extra
                break

        if not round_row:
            await interaction.response.send_message("세공을 진행할 마감된 돌 세공 라운드가 없습니다.", ephemeral=True)
            return
        if round_row["status"] != "closed":
            await interaction.response.send_message("먼저 투표 집계를 마감한 뒤 `/세공진행`을 실행해주세요.", ephemeral=True)
            return

        extra.setdefault("cutting_current_level", 0)
        extra.setdefault("cutting_logs", [])
        extra.setdefault("cutting_started_at", now_text())
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_row["id"]),
        )

    embed = build_cutting_progress_embed(round_row, extra)
    await interaction.response.send_message(embed=embed, view=cutting_action_view(int(round_row["id"])))


@bot.tree.command(name="점수", description="내 점수 또는 특정 유저의 점수를 확인합니다.")
@app_commands.describe(사용자="확인할 사용자")
async def show_score(interaction: discord.Interaction, 사용자: discord.Member | None = None) -> None:
    target = 사용자 or interaction.user
    current_bot = interaction.client.user
    if current_bot and target.id == current_bot.id:
        await interaction.response.send_message(
            "이 이벤트 봇의 점수는 조회할 수 없습니다.",
            ephemeral=True,
        )
        return
    with db() as conn:
        ensure_user(conn, target)
        row = conn.execute("SELECT total_score FROM users WHERE user_id = ?", (str(target.id),)).fetchone()
    await interaction.response.send_message(
        format_score_message(target.display_name, int(row["total_score"])),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="랭킹", description="현재 누적 점수 랭킹을 확인합니다.")
async def ranking(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버 안에서만 랭킹을 확인할 수 있습니다.", ephemeral=True)
        return

    current_bot_id = str(interaction.client.user.id) if interaction.client.user else ""
    with db() as conn:
        candidates = shuffle_tied_ranking_rows(fetch_ranking_rows(conn, current_bot_id))

    rows = []
    try:
        for candidate in candidates:
            if await current_guild_member(interaction.guild, str(candidate["user_id"])) is None:
                continue
            rows.append(candidate)
            if len(rows) == 10:
                break
    except discord.HTTPException:
        await interaction.response.send_message(
            "현재 서버 멤버를 확인하지 못했습니다. 잠시 후 다시 시도해주세요.",
            ephemeral=True,
        )
        return

    if not rows:
        await interaction.response.send_message("현재 서버에 게임 참여 기록이 있는 유저가 없습니다.", ephemeral=True)
        return

    lines = format_ranking_lines(rows)
    embed = discord.Embed(title="현재 랭킹 TOP 10", description="\n".join(lines), color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="데이터백업", description="SQLite 데이터베이스를 백업합니다.")
@app_commands.default_permissions()
@admin_only()
async def backup_data(interaction: discord.Interaction) -> None:
    if not DB_PATH.exists():
        await interaction.response.send_message("아직 데이터베이스 파일이 없습니다.", ephemeral=True)
        return

    backup_dir = DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"event_game_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sqlite3"
    shutil.copy2(DB_PATH, backup_path)
    await interaction.response.send_message(f"백업 완료: `{backup_path.name}`", ephemeral=True)


def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError(".env 파일에 DISCORD_TOKEN을 설정해주세요.")

    init_db()
    bot.run(token)


if __name__ == "__main__":
    main()
