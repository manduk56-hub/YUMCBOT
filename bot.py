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
TOP3_NONE_CHOICE = "__none__"


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
                type TEXT NOT NULL CHECK (type IN ('balance', 'bomb')),
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


def migrate_round_type_check(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'rounds'"
    ).fetchone()
    if not row or "top3" in row["sql"]:
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        CREATE TABLE rounds_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK (type IN ('balance', 'bomb', 'grid', 'cutting', 'top3')),
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


def mention_list(user_ids: list[str], limit: int = 40) -> str:
    if not user_ids:
        return "없음"
    mentions = [f"<@{user_id}>" for user_id in user_ids[:limit]]
    suffix = f"\n외 {len(user_ids) - limit}명" if len(user_ids) > limit else ""
    return ", ".join(mentions) + suffix


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
                    "SELECT channel_id FROM rounds WHERE id = ? AND status = 'open'",
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
            result = close_round_record(round_id, guild, closed_by="자동 마감")
            if not result:
                return

            _, embed = result
            if hasattr(channel, "send"):
                await channel.send(embed=embed)
        except asyncio.CancelledError:
            raise
        finally:
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

            already = conn.execute(
                """
                SELECT message_id FROM discussion_posts
                WHERE round_id = ? AND user_id = ?
                """,
                (round_row["id"], str(message.author.id)),
            ).fetchone()

            if already:
                try:
                    await message.delete()
                except discord.Forbidden:
                    pass
                except discord.HTTPException:
                    pass

                return

            ensure_user(conn, message.author)
            content = message.content[:1000]
            conn.execute(
                """
                INSERT INTO discussion_posts (round_id, user_id, message_id, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (round_row["id"], str(message.author.id), str(message.id), content, now_text()),
            )


bot = EventBot()


def build_balance_embed(round_id: int, question: str, option_a: str, option_b: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"오늘의 선택 #{round_id}",
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
        title=f"4선 폭탄 해체 게임 #{round_id}",
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
        title=f"관리자 시점 타일 게임 #{round_id}",
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
        title=f"돌 세공 예측 게임 #{round_id}",
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
            "각 세공 시도는 50% 확률로 성공하거나 실패합니다.\n"
            "처음 실패하기 직전의 강화 단계를 맞히는 게임입니다.\n"
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
    embed.set_footer(text=f"라운드 #{round_id}")
    return embed


def build_top3_embed(round_id: int, title: str, candidates: list[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(
        title=f"상위 3명 견제 투표 #{round_id}",
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
    success_rate = float(extra.get("success_rate", 0.5))
    logs = [str(log) for log in extra.get("cutting_logs", [])]

    embed = discord.Embed(
        title=f"돌 세공 진행 #{round_row['id']}",
        description=round_row["title"],
        color=discord.Color.teal(),
    )
    embed.add_field(name="현재 단계", value=f"{current_level}강화", inline=True)
    embed.add_field(name="최대 단계", value=f"{max_level}강화", inline=True)
    embed.add_field(name="성공 확률", value=f"{success_rate * 100:.0f}%", inline=True)
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
            1,
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
        title=f"돌 세공 결과 #{round_row['id']}",
        description=round_row["title"],
        color=discord.Color.green(),
    )
    embed.add_field(name="세공 기록", value="\n".join(logs[-10:]), inline=False)
    embed.add_field(name="최종 단계", value=f"{result_level}강화", inline=True)
    embed.add_field(name="정답자", value=f"{len(correct_users)}명", inline=True)
    embed.add_field(name="예측 분포", value="\n".join(prediction_lines), inline=False)
    embed.add_field(name="정답자 명단", value=mention_list(correct_users), inline=False)
    embed.add_field(name="정산", value=f"{result_level}강화를 맞힌 {len(correct_users)}명 전원 +1점", inline=False)
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
        success_rate = float(extra.get("success_rate", 0.5))
        current_level = int(extra.get("cutting_current_level", 0))
        logs = [str(log) for log in extra.get("cutting_logs", [])]

        if current_level >= max_level:
            logs.append(f"{max_level}강까지 성공, 세공 종료")
            embed = finalize_cutting_round(conn, round_row, extra, max_level, logs, interaction.guild)
            await interaction.response.edit_message(embed=embed, view=cutting_action_view(round_id, disabled=True))
            return

        next_level = current_level + 1
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
        discussion_count = conn.execute(
            "SELECT COUNT(*) AS count FROM discussion_posts WHERE round_id = ?",
            (round_row["id"],),
        ).fetchone()["count"]

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
                title=f"결과 공개 #{round_row['id']}",
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
                title=f"{'4선 폭탄 해체 결과' if is_defuse else '폭탄 결과'} #{round_row['id']}",
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
                title=f"관리자 시점 타일 결과 #{round_row['id']}",
                description=round_row["title"],
                color=discord.Color.green(),
            )
            embed.add_field(name="관리자의 선택 이유", value=admin_clue, inline=False)
            embed.add_field(name="정답 칸", value=f"{target_tile}번", inline=True)
            embed.add_field(name="정답자", value=f"{len(correct_users)}명", inline=True)
            embed.add_field(name="타일별 선택", value="\n".join(grid_lines), inline=False)
            embed.add_field(name="정답자 명단", value=mention_list(correct_users), inline=False)
            embed.add_field(name="정산", value=f"정답자 {len(correct_users)}명 전원 +{reward_points}점", inline=False)
        elif round_row["type"] == "cutting":
            grouped: dict[str, list[str]] = {}
            for row in votes:
                grouped.setdefault(row["choice"], []).append(row["user_id"])

            lines = []
            for level in CUTTING_LEVEL_CHOICES:
                users = grouped.get(level, [])
                lines.append(f"{level}강화: {len(users)}명")

            embed = discord.Embed(
                title=f"돌 세공 예측 집계 #{round_row['id']}",
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
                winners = conn.execute(
                    """
                    SELECT user_id, display_name FROM users
                    WHERE user_id NOT IN (?, ?, ?)
                    ORDER BY total_score DESC, updated_at ASC
                    """,
                    tuple(candidate_ids),
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
                title=f"상위 3명 견제 투표 결과 #{round_row['id']}",
                description=round_row["title"],
                color=discord.Color.green(),
            )
            embed.add_field(name="후보별 득표", value="\n".join(candidate_lines), inline=False)
            embed.add_field(name="선택하지 않는다", value=f"{none_votes}표", inline=True)
            embed.add_field(name="후보 득표 합", value=f"{candidate_vote_sum}표", inline=True)
            embed.add_field(name="결과", value=outcome, inline=False)
            embed.add_field(name="정산", value=settlement, inline=False)

        duration = extra.get("duration_minutes")
        close_at = extra.get("auto_close_at")
        stats = [f"투표 참여: {len(votes)}명", f"토론 발언: {discussion_count}개", f"마감 방식: {closed_by}"]
        if duration:
            stats.append(f"진행 시간: {duration}분")
        if close_at:
            stats.append(f"예정 마감: {close_time_text(float(close_at))}")
        embed.add_field(name="통계", value="\n".join(stats), inline=False)
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


class StartSetupModal(discord.ui.Modal):
    def __init__(self, game: str) -> None:
        self.game = game
        game_names = {
            "balance": "일반 밸런스",
            "defuse4": "4선 폭탄 해체",
            "grid": "관리자 시점 타일",
            "cutting": "돌 세공 예측",
            "top3": "상위 3명 견제",
        }
        super().__init__(title=f"{game_names.get(game, '게임')} 설정")

        self.game_title = discord.ui.TextInput(
            label="제목",
            placeholder="게임 제목 또는 질문",
            max_length=120,
        )
        self.duration = discord.ui.TextInput(
            label="진행 시간(분)",
            placeholder="예: 5",
            default="5",
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
                )
            elif self.game == "defuse4":
                await create_round_from_setup(
                    interaction,
                    self.game,
                    duration,
                    title,
                    안전색=parse_defuse_color(str(self.safe_color.value)),
                    힌트=str(self.defuse_hint.value).strip(),
                )
            elif self.game == "grid":
                await create_round_from_setup(
                    interaction,
                    self.game,
                    duration,
                    title,
                    정답칸=parse_range_int(str(self.target_tile.value), "정답 칸", 1, 9),
                    관리자설명=str(self.admin_clue.value).strip(),
                )
            else:
                await create_round_from_setup(interaction, self.game, duration, title)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)


@bot.tree.command(name="start", description="게임을 고르면 설정 창이 열립니다.")
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
@app_commands.describe(게임="진행할 게임 종류")
async def start(interaction: discord.Interaction, 게임: str) -> None:
    await interaction.response.send_modal(StartSetupModal(게임))


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
) -> None:
    if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("서버 텍스트 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return

    close_at = time.time() + (int(진행시간분) * 60)
    extra: dict[str, Any] = {
        "auto_close_at": close_at,
        "duration_minutes": int(진행시간분),
    }

    with db() as conn:
        if get_open_round(conn):
            await interaction.response.send_message("이미 열린 라운드가 있습니다. 먼저 `/라운드마감`을 해주세요.", ephemeral=True)
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
        elif 게임 == "top3":
            rows = conn.execute(
                """
                SELECT user_id, display_name, total_score
                FROM users
                ORDER BY total_score DESC, updated_at ASC
                LIMIT 3
                """
            ).fetchall()
            if len(rows) < 3:
                await interaction.response.send_message(
                    "상위 3명 견제 게임은 점수 기록이 있는 유저가 최소 3명 필요합니다.",
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
            extra.update({"max_level": 6, "success_rate": 0.5})
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

    embed.add_field(
        name="자동 마감",
        value=f"{진행시간분}분 후 자동으로 종료됩니다.\n예정 시각: {close_time_text(close_at)}",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, view=view)
    message = await interaction.original_response()

    discussion_thread_id = None
    if 게임 in {"balance", "grid", "defuse4"}:
        try:
            thread_name = (
                f"토론방 #{round_id} | 의견 남기기"
                if 게임 == "balance"
                else (
                    f"타일추리방 #{round_id} | 블러핑과 추리"
                    if 게임 == "grid"
                    else f"해체토론방 #{round_id} | 힌트와 블러핑"
                )
            )
            thread = await message.create_thread(name=thread_name)
            if 게임 == "balance":
                await thread.send("이곳에서 설득, 블러핑, 진실 말하기를 할 수 있습니다. 단, 한 사람당 한 번만 발언 가능합니다.")
                notice_embed = build_thread_notice_embed(
                    round_id,
                    thread,
                    "토론방이 열렸습니다",
                    "설득, 블러핑, 진실 말하기를 할 수 있습니다. 한 사람당 한 번만 발언 가능합니다.",
                )
            elif 게임 == "grid":
                await thread.send("관리자의 선택 이유를 해석해 정답 칸을 추리해보세요. 분탕, 블러핑, 역추리 모두 가능합니다. 단, 한 사람당 한 번만 의견을 남길 수 있습니다.")
                notice_embed = build_thread_notice_embed(
                    round_id,
                    thread,
                    "타일 추리방이 열렸습니다",
                    "관리자의 선택 이유를 해석해 정답 칸을 추리해보세요. 분탕, 블러핑, 역추리 모두 가능합니다.",
                )
            else:
                await thread.send("관리자의 힌트를 해석해 안전 색을 추리해보세요. 블러핑과 역추리 모두 가능합니다. 단, 한 사람당 한 번만 의견을 남길 수 있습니다.")
                notice_embed = build_thread_notice_embed(
                    round_id,
                    thread,
                    "해체 토론방이 열렸습니다",
                    "관리자의 힌트를 해석해 안전 색을 추리해보세요. 블러핑과 역추리 모두 가능합니다.",
                )
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
@admin_only()
async def close_round(interaction: discord.Interaction) -> None:
    with db() as conn:
        round_row = get_open_round(conn)
    if not round_row:
        await interaction.response.send_message("현재 열린 라운드가 없습니다.", ephemeral=True)
        return

    result = close_round_record(int(round_row["id"]), interaction.guild)
    if not result:
        await interaction.response.send_message("현재 열린 라운드가 없습니다.", ephemeral=True)
        return

    task = bot.auto_close_tasks.pop(int(round_row["id"]), None)
    if task:
        task.cancel()

    _, embed = result
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="세공진행", description="마감된 돌 세공 예측 라운드의 관리자 진행 패널을 엽니다.")
@admin_only()
@app_commands.describe(라운드번호="비워두면 가장 최근 마감된 미진행 돌 세공 라운드를 사용합니다.")
async def run_cutting(interaction: discord.Interaction, 라운드번호: int | None = None) -> None:
    with db() as conn:
        if 라운드번호 is not None:
            rows = conn.execute(
                "SELECT * FROM rounds WHERE id = ? AND type = 'cutting'",
                (라운드번호,),
            ).fetchall()
        else:
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
            if "cutting_result_level" not in parsed_extra:
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
    with db() as conn:
        ensure_user(conn, target)
        row = conn.execute("SELECT total_score FROM users WHERE user_id = ?", (str(target.id),)).fetchone()
    await interaction.response.send_message(f"{target.mention} 현재 점수: **{row['total_score']}점**", ephemeral=True)


@bot.tree.command(name="랭킹", description="현재 누적 점수 랭킹을 확인합니다.")
async def ranking(interaction: discord.Interaction) -> None:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT user_id, display_name, total_score
            FROM users
            ORDER BY total_score DESC, updated_at ASC
            LIMIT 10
            """
        ).fetchall()

    if not rows:
        await interaction.response.send_message("아직 점수 기록이 없습니다.", ephemeral=True)
        return

    lines = []
    for index, row in enumerate(rows, start=1):
        lines.append(f"{index}. <@{row['user_id']}> - **{row['total_score']}점**")

    embed = discord.Embed(title="현재 랭킹 TOP 10", description="\n".join(lines), color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="라운드현황", description="현재 열린 라운드 정보를 확인합니다.")
async def round_status(interaction: discord.Interaction) -> None:
    with db() as conn:
        round_row = get_open_round(conn)
        if not round_row:
            await interaction.response.send_message("현재 열린 라운드가 없습니다.", ephemeral=True)
            return
        vote_count = conn.execute(
            "SELECT COUNT(*) AS count FROM votes WHERE round_id = ?",
            (round_row["id"],),
        ).fetchone()["count"]

    embed = discord.Embed(
        title=f"진행 중인 라운드 #{round_row['id']}",
        description=round_row["question"],
        color=discord.Color.blurple(),
    )
    embed.add_field(name="종류", value=round_row["type"], inline=True)
    embed.add_field(name="참여 인원", value=f"{vote_count}명", inline=True)
    embed.add_field(name="비공개", value="선택 분포와 선택자는 마감 전까지 공개되지 않습니다.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="점수수정", description="운영자가 특정 유저의 점수를 수정합니다.")
@admin_only()
@app_commands.describe(사용자="점수를 수정할 유저", 점수="더하거나 뺄 점수", 사유="기록에 남길 사유")
async def adjust_score(
    interaction: discord.Interaction,
    사용자: discord.Member,
    점수: int,
    사유: str,
) -> None:
    with db() as conn:
        add_score(conn, str(사용자.id), 사용자.display_name, 점수, f"운영자 수정: {사유}", None)
    await interaction.response.send_message(f"{사용자.mention} 점수를 {점수:+d}점 수정했습니다. 사유: {사유}")


@bot.tree.command(name="데이터백업", description="SQLite 데이터베이스를 백업합니다.")
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
