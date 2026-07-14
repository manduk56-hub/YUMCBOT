from __future__ import annotations

import asyncio
import csv
import json
import os
import random
import signal
import socket
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

# 배포 환경(systemd)이 넘겨준 실제 환경변수가 우선이고, 없으면 로컬 .env로 채운다.
load_dotenv(BASE_DIR / ".env")

# 운영 서버는 DB를 코드 디렉터리 밖(/var/lib/yumcbot/data)에 두어 배포로 덮이지 않게 한다.
DATA_DIR = Path(os.getenv("YUMCBOT_DATA_DIR") or (BASE_DIR / "data")).resolve()
DB_PATH = DATA_DIR / (os.getenv("YUMCBOT_DB_NAME") or "event_game.sqlite3")
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
CUTTING_LEVEL_CHOICES = [str(index) for index in range(0, 7)]
CUTTING_REWARD_POINTS = {
    0: 3,
    1: 3,
    2: 1,
    3: 1,
    4: 1,
    5: 2,
    6: 4,
}
CUTTING_SUCCESS_RATES = {
    0: 0.9,
    1: 0.9,
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
    "mutual": "신뢰",
    "single": "단독 배신",
    "double": "동시 배신",
    "incomplete": "미선택",
}
MAIN_GAME_TYPES = {"trust", "sand"}
# 'grid'(관리자 시점 타일)는 제거된 미니게임이다. rounds.type CHECK 제약에는 남아 있지만
# (BASE_SCHEMA 불변) 새 라운드로는 만들 수 없다.
MINI_GAME_TYPES = {"balance", "defuse4", "cutting", "top3"}
GAME_NAMES = {
    "balance": "밸런스 게임",
    "defuse4": "4선 폭탄 해체",
    "cutting": "돌 세공 예측",
    "top3": "상위 3명 견제",
    "trust": "딜레마의 섬",
    "sand": "모래성 게임",
}
BALANCE_VOTE_CHOICES = [("A 선택", "A"), ("B 선택", "B")]
ANONYMOUS_GUIDE_TEXT = (
    "아래 버튼으로 익명 발언을 남겨주세요. 일반 메시지는 익명 보호를 위해 삭제됩니다.\n"
    "발언 횟수 제한은 없으며, 같은 라운드에서는 계속 같은 익명 번호로 표시됩니다."
)
SAND_NUMBER_CHOICES = [str(index) for index in range(1, 11)]
SAND_PLAY_SECONDS = 600
SAND_RULES_TEXT = (
    "1. 각 참여자가 1~10 중 모래성이 무너질 횟수를 정합니다.\n"
    "2. 두 수의 차이만큼은 안전하게 덜 수 있고, 그 횟수를 초과해서 더는 순간 무너집니다. "
    "(예: 9와 7이면 차이가 2 → 2번까지는 성공, 3번째에 붕괴. 차이가 0이면 첫 덜기에서 붕괴)\n"
    "3. 참여자는 번갈아가며 `포기하기` 또는 `모래 덜기`를 결정합니다.\n"
    "4. 숫자를 안 내거나 제한 시간 안에 끝내지 못하면 마감 때 랜덤으로 자동 진행됩니다.\n\n"
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
EVENT_STATUS_LABELS = {
    "recruiting": "참가 모집 중",
    "active": "게임 진행 중",
    "closed": "종료",
}
# 참가자가 홀수라 짝이 없는 사람이 생겼을 때, 새 참가자가 긴급 투입되면 전체 마감이 이만큼 밀린다.
EMERGENCY_EXTENSION_SECONDS = 60
AFK_TICK_SECONDS = 60
AFK_COIN_PER_TICK = 1
SCORE_REVEAL_COST = 30


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def cutting_success_rate(current_level: int) -> float:
    return CUTTING_SUCCESS_RATES[current_level]


def _group_consecutive(values: dict[int, Any]) -> list[tuple[list[int], Any]]:
    """연속하면서 값이 같은 단계들을 한 묶음으로 만든다. 안내문을 상수에서 생성하기 위한 것."""
    groups: list[tuple[list[int], Any]] = []
    for level in sorted(values):
        if groups and groups[-1][1] == values[level] and groups[-1][0][-1] == level - 1:
            groups[-1][0].append(level)
        else:
            groups.append(([level], values[level]))
    return groups


def cutting_success_rate_text() -> str:
    """예: '0→1·1→2 90% | 2→3·3→4 60% | 4→5 50% | 5→6 30%'"""
    parts = []
    for levels, rate in _group_consecutive(CUTTING_SUCCESS_RATES):
        steps = "·".join(f"{level}→{level + 1}" for level in levels)
        parts.append(f"{steps} {rate * 100:.0f}%")
    return " | ".join(parts)


def cutting_reward_text() -> str:
    """예: '0~1강화 +3점 | 2~4강화 +1점 | 5강화 +2점 | 6강화 +4점'"""
    parts = []
    for levels, points in _group_consecutive(CUTTING_REWARD_POINTS):
        span = f"{levels[0]}강화" if len(levels) == 1 else f"{levels[0]}~{levels[-1]}강화"
        parts.append(f"{span} +{points}점")
    return " | ".join(parts)


@contextmanager
def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL: 재시작/급종료 시 DB 손상 방지, /데이터백업 중에도 쓰기가 막히지 않는다.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# v1 베이스라인. 이 문자열은 절대 수정하지 않는다.
# 스키마를 바꾸려면 SCHEMA_VERSION을 올리고 MIGRATIONS에 항목을 추가한다.
# (여기를 고치면 새 DB와 기존 운영 DB의 스키마가 갈라진다.)
BASE_SCHEMA = """
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
                discussion_thread_id TEXT,
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

BASELINE_VERSION = 1

# 스키마 변경은 전부 여기로. 예:
#   MIGRATIONS = {2: ["ALTER TABLE rounds ADD COLUMN foo TEXT"]}
# 롤백 가능하도록 반드시 '추가' 형태(ADD COLUMN 등)로만 작성한다.
MIGRATIONS: dict[int, list[str]] = {
    # discussion_posts는 PK가 (round_id, user_id)라 유저당 한 건만 담을 수 있다.
    # 발언 횟수 제한을 없애면서 익명 번호를 고정하려면 별도 테이블이 필요하다.
    # (BASE_SCHEMA는 불변이므로 기존 테이블의 PK를 고칠 수 없다.)
    2: [
        """
        CREATE TABLE IF NOT EXISTS discussion_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            anon_number INTEGER NOT NULL,
            message_id TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE
        )
        """,
    ],
    # 음성채널 체류로 쌓이는 잠수 코인. /지목으로 소모한다.
    3: [
        """
        CREATE TABLE IF NOT EXISTS afk_coins (
            user_id TEXT PRIMARY KEY,
            coins INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        )
        """,
    ],
}

SCHEMA_VERSION = max([BASELINE_VERSION, *MIGRATIONS])


def apply_baseline_schema(conn: sqlite3.Connection) -> None:
    """v1 베이스라인 = BASE_SCHEMA + 아래 세 개의 멱등 보정.

    user_version이 도입되기 전부터 굴러온 DB들은 매 부팅마다 이 보정을 거쳐 왔다.
    따라서 이미 존재하는 모든 DB의 실제 스키마가 곧 v1이다. 여기에 ADD COLUMN을
    새로 끼워 넣으면 기존 DB에서 'duplicate column' 으로 터지므로 손대지 말고,
    앞으로의 변경은 MIGRATIONS(v2~)에 추가한다.
    """
    conn.executescript(BASE_SCHEMA)
    migrate_round_type_check(conn)
    ensure_event_session_schema(conn)
    ensure_trust_pairing_schema(conn)


def _schema_snapshot(conn: sqlite3.Connection) -> dict[str, set[str]]:
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {
        table: {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for table in tables
    }


def canonical_schema() -> dict[str, set[str]]:
    """베이스라인 + 모든 마이그레이션을 적용했을 때 나와야 하는 스키마."""
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row  # ensure_* 보정이 row["name"]으로 접근한다
    try:
        apply_baseline_schema(mem)
        for version in range(BASELINE_VERSION + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS[version]:
                mem.execute(statement)
        return _schema_snapshot(mem)
    finally:
        mem.close()


def verify_schema() -> None:
    """실제 DB가 코드가 기대하는 스키마를 만족하는지 확인한다.

    마이그레이션을 빠뜨린 채 배포하면 봇은 정상 로그인까지 해버리고 게임 도중에
    'no such column'으로 터진다. 부팅 시점에 미리 죽여서 배포 헬스체크가 잡아내게 한다.
    구버전으로 롤백한 경우 DB에 모르는 컬럼이 더 있을 수 있는데, 그건 허용한다.
    """
    expected = canonical_schema()
    with db() as conn:
        actual = _schema_snapshot(conn)

    problems: list[str] = []
    for table, columns in sorted(expected.items()):
        if table not in actual:
            problems.append(f"테이블 누락: {table}")
            continue
        missing = columns - actual[table]
        if missing:
            problems.append(f"{table} 컬럼 누락: {', '.join(sorted(missing))}")

    if problems:
        detail = "\n  - ".join(problems)
        raise RuntimeError(
            f"DB 스키마가 코드와 일치하지 않습니다 (기대 v{SCHEMA_VERSION}):\n  - {detail}\n"
            "MIGRATIONS에 변경분을 등록했는지 확인하세요."
        )


def init_db() -> None:
    with db() as conn:
        apply_baseline_schema(conn)

        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            # 새로 만든 DB이거나, user_version 도입 이전부터 굴러온 기존 DB. 둘 다 v1이다.
            version = BASELINE_VERSION
            conn.execute(f"PRAGMA user_version = {BASELINE_VERSION}")

        if version > SCHEMA_VERSION:
            print(
                f"경고: DB 스키마(v{version})가 코드(v{SCHEMA_VERSION})보다 최신입니다. "
                "구버전으로 롤백된 상태일 수 있습니다.",
                flush=True,
            )
            return

        for target in range(version + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS[target]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {target}")
            print(f"스키마 마이그레이션 적용: v{target}", flush=True)


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


def ensure_trust_pairing_schema(conn: sqlite3.Connection) -> None:
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(trust_pairings)").fetchall()
    }
    if "discussion_thread_id" not in columns:
        conn.execute("ALTER TABLE trust_pairings ADD COLUMN discussion_thread_id TEXT")


def assign_anonymous_number(conn: sqlite3.Connection, round_id: int, user_id: str) -> int:
    """라운드 안에서 유저에게 고정으로 붙는 익명 번호. 첫 발언 때 1부터 순서대로 발급한다."""
    existing = conn.execute(
        "SELECT anon_number FROM discussion_messages WHERE round_id = ? AND user_id = ? LIMIT 1",
        (round_id, user_id),
    ).fetchone()
    if existing:
        return int(existing["anon_number"])

    row = conn.execute(
        "SELECT COALESCE(MAX(anon_number), 0) + 1 AS next FROM discussion_messages WHERE round_id = ?",
        (round_id,),
    ).fetchone()
    return int(row["next"])


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


def get_afk_coins(conn: sqlite3.Connection, user_id: str) -> int:
    row = conn.execute("SELECT coins FROM afk_coins WHERE user_id = ?", (user_id,)).fetchone()
    return int(row["coins"]) if row else 0


def grant_afk_coins(conn: sqlite3.Connection, user_id: str, amount: int) -> None:
    conn.execute(
        """
        INSERT INTO afk_coins (user_id, coins, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            coins = coins + excluded.coins,
            updated_at = excluded.updated_at
        """,
        (user_id, amount, now_text()),
    )


def spend_afk_coins(conn: sqlite3.Connection, user_id: str, cost: int) -> bool:
    """잔액이 충분할 때만 차감한다. 동시 호출에도 잔액이 음수로 내려가지 않도록 조건부 UPDATE로 처리한다."""
    cursor = conn.execute(
        "UPDATE afk_coins SET coins = coins - ?, updated_at = ? WHERE user_id = ? AND coins >= ?",
        (cost, now_text(), user_id, cost),
    )
    return cursor.rowcount == 1


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


def format_my_score_message(third_score: int | None, my_score: int) -> str:
    """3등 점수와 내 점수만 알려준다. 이름은 넣지 않는다.

    3등이 동점이어도 보여주는 값이 '점수'뿐이라, 누가 3등으로 뽑히든 결과가 같다.
    (동점자가 몇 명인지 유저가 알 수 없어야 한다.)
    """
    third_text = f"**{third_score}점**" if third_score is not None else "아직 없음"
    return f"3등 점수: {third_text}\n내 점수: **{my_score}점**"


def fetch_ranking_rows(conn: sqlite3.Connection, current_bot_id: str) -> list[sqlite3.Row]:
    """점수 순으로 줄 세운 참여자 명단. 게임 기록이 있는 사람만, 봇은 제외.

    동점자는 user_id 순으로 고정한다. 점수가 그대로면 몇 번을 불러도 같은 사람이 3등으로
    뽑혀야 하기 때문이다. (updated_at으로 정렬하면 add_score가 그 값을 건드려 순서가 흔들린다.)
    """
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
        ORDER BY users.total_score DESC, users.user_id ASC
        """,
        (current_bot_id,),
    ).fetchall()


def fetch_admin_score_rows(conn: sqlite3.Connection, current_bot_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT user_id, display_name, total_score, updated_at
        FROM users
        WHERE user_id != ?
        ORDER BY total_score DESC, updated_at ASC
        """,
        (current_bot_id,),
    ).fetchall()


def write_admin_score_report(path: Path, rows: list[sqlite3.Row]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as report_file:
        writer = csv.writer(report_file)
        writer.writerow(["user_id", "display_name", "total_score", "updated_at"])
        for row in rows:
            writer.writerow([row["user_id"], row["display_name"], row["total_score"], row["updated_at"]])


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


def round_game_key(round_row: sqlite3.Row) -> str:
    """코드상 게임 이름. 4선 폭탄 해체는 DB에 type='bomb'으로 저장되고 extra.mode가 'defuse4'다."""
    return str(round_extra(round_row).get("mode") or round_row["type"])


def thread_discussion_view(round_id: int, game_key: str) -> discord.ui.View:
    """토론방 맨 아래에 붙는 버튼 묶음: 선택지 + 익명 발언하기.

    토론 스레드는 라운드 메시지에서 파생시키므로 그 메시지가 스레드의 시작 메시지가 되는데,
    시작 메시지의 버튼은 스레드 안에서 누를 수 없다(실체가 부모 채널에 있는 메시지라 회색으로 뜬다).
    그래서 같은 custom_id의 선택 버튼을 스레드 안에도 둔다. votes는 (round_id, user_id) upsert라
    채널에서 누르든 스레드에서 누르든 마지막 선택 하나만 남는다.
    """
    view = discord.ui.View(timeout=None)
    vote_buttons = (
        vote_view(round_id, BALANCE_VOTE_CHOICES)
        if game_key == "balance"
        else defuse_vote_view(round_id)
    )
    for button in [*vote_buttons.children, *anonymous_discussion_view(round_id).children]:
        view.add_item(button)
    return view


def count_discussion_messages(conn: sqlite3.Connection, round_id: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count FROM discussion_messages
        WHERE round_id = ? AND message_id != 'pending'
        """,
        (round_id,),
    ).fetchone()
    return int(row["count"]) if row else 0


def build_anonymous_guide_embed(post_count: int) -> discord.Embed:
    embed = discord.Embed(
        title="🕶️ 익명 토론방",
        description=ANONYMOUS_GUIDE_TEXT,
        color=discord.Color.dark_grey(),
    )
    embed.add_field(
        name="투표",
        value="아래 선택 버튼으로 이 방에서 바로 투표할 수 있습니다. 마감 전까지 몇 번이든 바꿀 수 있습니다.",
        inline=False,
    )
    embed.add_field(name="누적 발언", value=f"{post_count}개", inline=False)
    return embed


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
            "2. 메인게임 1개 진행 (딜레마의 섬 또는 모래성 게임)\n"
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
        # 발언 게시 -> 옛 버튼 삭제 -> 버튼 재게시까지 API 호출이 여러 번이라 모달의 3초 응답
        # 제한을 넘긴다. 먼저 defer하고, 성공하면 그 ephemeral을 지워서 흔적을 남기지 않는다.
        await interaction.response.defer(ephemeral=True)

        if not isinstance(interaction.channel, discord.Thread):
            await interaction.followup.send("토론 스레드에서만 발언할 수 있습니다.", ephemeral=True)
            return

        content = self.content_input.value.strip()
        if not content:
            await interaction.followup.send("발언 내용을 입력해주세요.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        with db() as conn:
            round_row = conn.execute(
                """
                SELECT * FROM rounds
                WHERE id = ? AND status = 'open' AND discussion_thread_id = ?
                """,
                (self.round_id, str(interaction.channel.id)),
            ).fetchone()
            if not round_row:
                await interaction.followup.send("이미 마감되었거나 유효하지 않은 토론방입니다.", ephemeral=True)
                return
            if not can_join_round(conn, self.round_id, user_id):
                await interaction.followup.send(
                    "이번 이벤트에서 `참여하기`를 누른 유저만 토론에 참여할 수 있습니다.",
                    ephemeral=True,
                )
                return

            ensure_user(conn, interaction.user)
            anon_number = assign_anonymous_number(conn, self.round_id, user_id)
            # 게시 전에 행을 잡아둔다. 더블클릭해도 번호가 두 개로 갈리지 않는다.
            cursor = conn.execute(
                """
                INSERT INTO discussion_messages
                    (round_id, user_id, anon_number, message_id, content, created_at)
                VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (self.round_id, user_id, anon_number, content, now_text()),
            )
            post_id = int(cursor.lastrowid)
            old_button_message_id = str(round_extra(round_row).get("anon_button_message_id") or "")
            game_key = round_game_key(round_row)

        try:
            posted_message = await interaction.channel.send(
                f"**익명{anon_number}**\n{content}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            with db() as conn:
                conn.execute("DELETE FROM discussion_messages WHERE id = ?", (post_id,))
            await interaction.followup.send("익명 발언을 올리지 못했습니다. 다시 시도해주세요.", ephemeral=True)
            return

        with db() as conn:
            conn.execute(
                "UPDATE discussion_messages SET message_id = ? WHERE id = ?",
                (str(posted_message.id), post_id),
            )
            post_count = count_discussion_messages(conn, self.round_id)

        # 버튼이 발언에 밀려 위로 올라가지 않도록, 옛 버튼을 지우고 맨 아래에 다시 붙인다.
        if old_button_message_id:
            try:
                old_button = await interaction.channel.fetch_message(int(old_button_message_id))
                await old_button.delete()
            except (discord.NotFound, discord.HTTPException, ValueError):
                pass
        try:
            new_button = await interaction.channel.send(
                embed=build_anonymous_guide_embed(post_count),
                view=thread_discussion_view(self.round_id, game_key),
            )
        except discord.HTTPException:
            # 버튼 재게시에 실패해도 발언 자체는 이미 올라갔다. 발언을 되돌리지는 않는다.
            new_button = None

        if new_button:
            with db() as conn:
                fresh_row = conn.execute(
                    "SELECT extra_json FROM rounds WHERE id = ?", (self.round_id,)
                ).fetchone()
                if fresh_row:
                    extra = round_extra(fresh_row)
                    extra["anon_button_message_id"] = str(new_button.id)
                    conn.execute(
                        "UPDATE rounds SET extra_json = ? WHERE id = ?",
                        (json.dumps(extra, ensure_ascii=False), self.round_id),
                    )

        # 발언자를 드러내지 않도록 확인 문구를 남기지 않고 defer한 ephemeral만 조용히 지운다.
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass


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
            "round_id": round_id,
            "title": str(round_row["title"]),
            "pairs": pairs,
            "embed": embed,
            "view": emergency_join_view(round_id) if unmatched_user_id else None,
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


async def resolve_text_channel(channel_id: str) -> discord.TextChannel | None:
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except (discord.HTTPException, ValueError):
            return None
    return channel if isinstance(channel, discord.TextChannel) else None


async def create_pair_thread(
    channel: discord.TextChannel,
    thread_name: str,
    member_ids: list[str],
    reason: str,
    view: discord.ui.View,
    content: str | None = None,
    embed: discord.Embed | None = None,
) -> tuple[discord.Thread, discord.Message] | None:
    """짝 하나를 위한 비공개 스레드를 열고 첫 메시지를 올린다. 실패하면 None.

    딜레마의 섬 · 모래성 · 긴급투입이 모두 이 헬퍼를 쓴다.
    보관되면 `스레드 관리` 권한이 없는 유저의 버튼 클릭이 막히므로 보관 시간을 넉넉히 잡는다.

    초대는 짝 두 사람뿐이다. 운영자도 게임에 참여할 수 있으므로 참관용으로 방에 넣지 않는다.
    운영자는 `스레드 관리` 권한으로 스레드 목록에서 필요한 방만 골라 보면 된다.
    """
    try:
        thread = await channel.create_thread(
            name=thread_name[:100],
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=1440,
            reason=reason,
        )
    except (discord.Forbidden, discord.HTTPException):
        return None

    for user_id in member_ids:
        try:
            await thread.add_user(discord.Object(id=int(user_id)))
        except (discord.HTTPException, ValueError):
            pass

    try:
        message = await thread.send(content=content, embed=embed, view=view)
    except discord.HTTPException:
        return None
    return thread, message


def save_pair_thread_id(round_id: int, user_a_id: str, user_b_id: str, thread_id: str) -> None:
    with db() as conn:
        conn.execute(
            """
            UPDATE trust_pairings
            SET discussion_thread_id = ?
            WHERE round_id = ? AND user_a_id = ? AND user_b_id = ?
            """,
            (thread_id, round_id, user_a_id, user_b_id),
        )


async def warn_failed_pair_threads(channel: discord.TextChannel, round_id: int, failed: int) -> None:
    if not failed:
        return
    try:
        await channel.send(
            f"라운드 #{round_id}의 비공개 방 {failed}개를 만들지 못했습니다. "
            "봇의 비공개 스레드 생성·관리 권한을 확인해주세요."
        )
    except discord.HTTPException:
        pass


def trust_pair_intro(user_a_id: str, user_b_id: str) -> str:
    return (
        f"<@{user_a_id}>님과 <@{user_b_id}>님의 비공개 협상방입니다.\n"
        "서로 마음껏 대화한 뒤 아래 버튼으로 `신뢰` 또는 `배신`을 선택하세요. (마감 전까지 몇 번이든 바꿀 수 있습니다)\n"
        "선택은 마감 전까지 상대에게 공개되지 않으며, 마감 후 이 방에서 함께 공개됩니다.\n"
        "*마감까지 고르지 않으면 `신뢰`/`배신` 중 하나가 랜덤으로 배정됩니다."
    )


async def create_trust_pair_threads(transition: dict[str, Any]) -> None:
    if transition.get("state") != "decision":
        return

    channel = await resolve_text_channel(str(transition["channel_id"]))
    if channel is None:
        return

    round_id = int(transition["round_id"])
    title = str(transition["title"])
    failed_pairs = 0
    for pair_number, pair in enumerate(transition.get("pairs", []), start=1):
        user_a_id, user_b_id = (str(pair[0]), str(pair[1]))
        created = await create_pair_thread(
            channel,
            f"{title} · {pair_number}세트",
            [user_a_id, user_b_id],
            f"딜레마의 섬 #{round_id} 세트별 비공개 협상방",
            trust_decision_view(round_id),
            content=trust_pair_intro(user_a_id, user_b_id),
        )
        if created is None:
            failed_pairs += 1
            continue
        thread, _ = created
        save_pair_thread_id(round_id, user_a_id, user_b_id, str(thread.id))

    await warn_failed_pair_threads(channel, round_id, failed_pairs)


def build_trust_pair_result_embed(
    title: str,
    user_a_id: str,
    user_b_id: str,
    user_a_choice: str | None,
    user_b_choice: str | None,
) -> discord.Embed:
    choice_labels = {"trust": "신뢰", "betray": "배신"}
    if not user_a_choice or not user_b_choice:
        outcome = "미선택 포함 · 점수 변동 없음"
    elif user_a_choice == "trust" and user_b_choice == "trust":
        outcome = "상호 신뢰 · 두 사람 모두 +1점"
    elif user_a_choice == "betray" and user_b_choice == "betray":
        outcome = "동시 배신 · 두 사람 모두 -1점"
    else:
        betrayer_id = user_a_id if user_a_choice == "betray" else user_b_id
        outcome = f"단독 배신 · <@{betrayer_id}> +2점, 상대 0점"

    embed = discord.Embed(
        title=f"세트 결과 · {title}",
        description="두 사람의 선택이 공개되었습니다.",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="서로의 선택",
        value=(
            f"<@{user_a_id}>: **{choice_labels.get(user_a_choice, '미선택')}**\n"
            f"<@{user_b_id}>: **{choice_labels.get(user_b_choice, '미선택')}**"
        ),
        inline=False,
    )
    embed.add_field(name="세트 정산", value=outcome, inline=False)
    return embed


async def publish_trust_pair_results(round_id: int) -> None:
    with db() as conn:
        round_row = conn.execute(
            "SELECT title FROM rounds WHERE id = ? AND type = 'trust'",
            (round_id,),
        ).fetchone()
        if not round_row:
            return
        pairs = conn.execute(
            """
            SELECT user_a_id, user_b_id, discussion_thread_id
            FROM trust_pairings
            WHERE round_id = ? AND discussion_thread_id IS NOT NULL
            ORDER BY created_at ASC
            """,
            (round_id,),
        ).fetchall()
        votes = conn.execute(
            "SELECT user_id, choice FROM votes WHERE round_id = ?",
            (round_id,),
        ).fetchall()

    vote_choices = {str(row["user_id"]): str(row["choice"]) for row in votes}
    for pair in pairs:
        thread_id = int(pair["discussion_thread_id"])
        user_a_id = str(pair["user_a_id"])
        user_b_id = str(pair["user_b_id"])
        try:
            thread = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
            if not isinstance(thread, discord.Thread):
                continue
            if thread.archived:
                await thread.edit(archived=False, reason="딜레마의 섬 세트 결과 공개")
            await thread.send(
                embed=build_trust_pair_result_embed(
                    str(round_row["title"]),
                    user_a_id,
                    user_b_id,
                    vote_choices.get(user_a_id),
                    vote_choices.get(user_b_id),
                )
            )
            await thread.edit(
                archived=True,
                locked=True,
                reason="딜레마의 섬 세트 결과 공개 완료",
            )
        except (discord.Forbidden, discord.HTTPException, ValueError):
            continue


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
            await interaction.response.send_message("이미 오늘의 메인 게임에 참여 등록했습니다.", ephemeral=True)
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
            WHERE round_id = ?
              AND (user_a_id = ? OR user_b_id = ?)
              AND discussion_thread_id = ?
            """,
            (
                round_id,
                str(interaction.user.id),
                str(interaction.user.id),
                str(interaction.channel_id),
            ),
        ).fetchone()
        if not pairing:
            await interaction.response.send_message(
                "자신에게 배정된 비공개 협상방에서만 선택할 수 있습니다.",
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
        "선택을 저장했습니다. 마감 전까지 다시 누르면 선택이 바뀝니다.",
        ephemeral=True,
    )


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
            "round_id": round_id,
            "title": str(round_row["title"]),
            "pairs": pairs,
            "embed": build_sand_play_embed(updated_row, extra),
            # 숫자·행동 버튼은 짝별 비공개 방 안에만 둔다. 메인 메시지는 진행판 역할만 한다.
            "view": emergency_join_view(round_id) if unmatched_user_id else None,
            "close_at": play_close_at,
        }


def sand_pair_intro(user_a_id: str, user_b_id: str) -> str:
    return (
        f"<@{user_a_id}>님과 <@{user_b_id}>님의 1:1 모래성 방입니다.\n"
        "먼저 각자 1~10 중 하나를 고르세요. (번복 불가, 마감까지 상대에게 공개되지 않습니다)\n"
        "두 사람이 모두 고르면 번갈아가며 `포기하기` 또는 `모래 덜기`를 선택합니다."
    )


def sand_pair_settlement_text(pair: dict[str, Any]) -> str:
    """짝 하나의 정산 문구. 라운드 결과 요약과 방별 결과 공개가 같은 문구를 쓰도록 한 곳에 모은다."""
    outcome = str(pair.get("outcome") or "")
    result = pair.get("result", {})
    if outcome == "mutual_quit":
        return "각 -1점"
    if outcome == "quit_success":
        return f"<@{result['winner']}> +2점 · <@{result['quitter']}> -1점"
    if outcome == "quit_collapse":
        return f"<@{result['winner']}> +1점 · <@{result['collapsed_user']}> -1점"
    if outcome == "plain_collapse":
        return f"<@{result['collapsed_user']}> -1점"
    return "점수 변동 없음"


async def create_sand_pair_threads(transition: dict[str, Any]) -> None:
    """짝마다 비공개 방을 열고 그 안에 숫자·행동 버튼을 둔다. (딜레마의 섬과 같은 방식)"""
    if transition.get("state") != "playing":
        return

    channel = await resolve_text_channel(str(transition["channel_id"]))
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
    for pair_number, pair in enumerate(extra.get("sand_pairs", []), start=1):
        user_a_id = str(pair["user_a_id"])
        user_b_id = str(pair["user_b_id"])
        created = await create_pair_thread(
            channel,
            f"{title} · {pair_number}번 모래성",
            [user_a_id, user_b_id],
            f"모래성 게임 #{round_id} 1:1 비공개 방",
            sand_play_view(round_id),
            content=sand_pair_intro(user_a_id, user_b_id),
            embed=build_sand_pair_embed(title, pair, extra),
        )
        if created is None:
            failed_pairs += 1
            continue
        thread, message = created
        save_pair_thread_id(round_id, user_a_id, user_b_id, str(thread.id))
        created_threads[(user_a_id, user_b_id)] = (str(thread.id), str(message.id))

    if created_threads:
        store_sand_thread_ids(round_id, created_threads)
    await warn_failed_pair_threads(channel, round_id, failed_pairs)


def store_sand_thread_ids(
    round_id: int,
    created_threads: dict[tuple[str, str], tuple[str, str]],
) -> None:
    """스레드/메시지 id를 sand_pairs에 심는다. 재시작 후에도 방별 진행판을 갱신할 수 있어야 한다."""
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row:
            return
        extra = round_extra(round_row)
        for pair in extra.get("sand_pairs", []):
            key = (str(pair["user_a_id"]), str(pair["user_b_id"]))
            if key in created_threads:
                pair["thread_id"], pair["play_message_id"] = created_threads[key]
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )


async def publish_sand_pair_results(round_id: int) -> None:
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'sand'",
            (round_id,),
        ).fetchone()
    if not round_row:
        return

    title = str(round_row["title"])
    extra = round_extra(round_row)
    for pair in extra.get("sand_pairs", []):
        thread_id = str(pair.get("thread_id") or "")
        if not thread_id:
            continue
        try:
            bot.sand_board_locks.pop(int(thread_id), None)
            thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
            if not isinstance(thread, discord.Thread):
                continue
            if thread.archived:
                await thread.edit(archived=False, reason="모래성 게임 결과 공개")
            await thread.send(embed=build_sand_pair_result_embed(title, pair))
            await thread.edit(
                archived=True,
                locked=True,
                reason="모래성 게임 결과 공개 완료",
            )
        except (discord.Forbidden, discord.HTTPException, ValueError):
            continue


def apply_emergency_join(
    extra: dict[str, Any],
    round_type: str,
    new_user_id: str,
) -> dict[str, Any] | None:
    """긴급 투입을 extra에 반영한다. 짝 없는 대기자가 없으면 None.

    DB도 디스코드도 건드리지 않는 순수 함수라 테스트에서 그대로 쓴다.
    호출 전에 라운드 상태·중복 참여 검증이 끝나 있어야 한다.
    """
    unmatched_user_id = str(extra.get("unmatched_user_id") or "")
    if not unmatched_user_id or unmatched_user_id == new_user_id:
        return None

    pair = {
        "user_a_id": unmatched_user_id,
        "user_b_id": new_user_id,
        "number_a": None,
        "number_b": None,
        "phase": "number_selection",
        "current_user_id": random.choice([unmatched_user_id, new_user_id]),
        "successful_removals": 0,
    }
    if round_type == "sand":
        extra.setdefault("sand_pairs", []).append(pair)

    extra["unmatched_user_id"] = None
    extra["emergency_user_id"] = new_user_id

    # 늦게 합류한 짝도 게임을 끝낼 시간이 필요하다. 전체 마감을 1분 미룬다.
    close_at = float(extra.get("auto_close_at") or time.time()) + EMERGENCY_EXTENSION_SECONDS
    extra["auto_close_at"] = close_at
    extra["decision_close_at" if round_type == "trust" else "play_close_at"] = close_at

    return {
        "user_a_id": unmatched_user_id,
        "user_b_id": new_user_id,
        "pair": pair,
        "close_at": close_at,
    }


def emergency_thread_notice(close_at: float) -> str:
    """비공개 방에 나가는 공지. 투입자가 누구인지는 절대 넣지 않는다.

    방마다 상대가 누구인지가 게임의 일부다. 투입자 이름은 메인 채널의 진행판에서만 공개한다.
    """
    return (
        "🚨 새 참가자가 긴급 투입되어 전체 진행 시간이 1분 연장됩니다.\n"
        f"새 마감: {close_time_text(close_at)}"
    )


async def announce_emergency_join(round_id: int, close_at: float) -> None:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT discussion_thread_id FROM trust_pairings
            WHERE round_id = ? AND discussion_thread_id IS NOT NULL
            """,
            (round_id,),
        ).fetchall()

    notice = emergency_thread_notice(close_at)
    for row in rows:
        thread_id = str(row["discussion_thread_id"])
        try:
            thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
            if isinstance(thread, discord.Thread):
                await thread.send(notice)
        except (discord.HTTPException, ValueError):
            continue


async def refresh_main_trust_board(round_id: int) -> None:
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row or not round_row["message_id"]:
            return
        pair_rows = conn.execute(
            """
            SELECT user_a_id, user_b_id FROM trust_pairings
            WHERE round_id = ? ORDER BY created_at ASC
            """,
            (round_id,),
        ).fetchall()

    extra = round_extra(round_row)
    channel = await resolve_text_channel(str(round_row["channel_id"]))
    if channel is None:
        return

    pairs = [(str(row["user_a_id"]), str(row["user_b_id"])) for row in pair_rows]
    unmatched_user_id = str(extra.get("unmatched_user_id") or "") or None
    try:
        message = await channel.fetch_message(int(round_row["message_id"]))
        await message.edit(
            embed=build_trust_decision_embed(
                round_id,
                str(round_row["title"]),
                pairs,
                unmatched_user_id,
                str(extra.get("emergency_user_id") or "") or None,
            ),
            view=emergency_join_view(round_id) if unmatched_user_id else None,
        )
    except (discord.HTTPException, ValueError):
        pass


async def handle_emergency_join(interaction: discord.Interaction, round_id: int) -> None:
    if interaction.user.bot:
        await interaction.response.send_message("봇 계정은 긴급 투입할 수 없습니다.", ephemeral=True)
        return

    user_id = str(interaction.user.id)
    # 방 생성 + 전체 공지까지 API 호출이 여러 번이라 3초 응답 제한을 넘긴다.
    await interaction.response.defer(ephemeral=True)

    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND status = 'open' AND type IN ('trust', 'sand')",
            (round_id,),
        ).fetchone()
        if not round_row:
            await interaction.followup.send("이미 끝난 라운드입니다.", ephemeral=True)
            return

        round_type = str(round_row["type"])
        extra = round_extra(round_row)
        phase = extra.get("trust_phase") if round_type == "trust" else extra.get("sand_phase")
        if phase not in {"decision", "playing"}:
            await interaction.followup.send("지금은 긴급 투입할 수 없습니다.", ephemeral=True)
            return

        registered = conn.execute(
            "SELECT 1 FROM trust_registrations WHERE round_id = ? AND user_id = ?",
            (round_id, user_id),
        ).fetchone()
        if registered:
            await interaction.followup.send(
                "이미 이 게임에 참여 중입니다. 긴급 투입은 게임에 참여하지 않은 사람만 할 수 있습니다.",
                ephemeral=True,
            )
            return

        # 동시 클릭 방어: 대기자를 읽고 비우는 것까지 한 트랜잭션 안에서 끝낸다.
        applied = apply_emergency_join(extra, round_type, user_id)
        if not applied:
            await interaction.followup.send("긴급 투입 자리가 이미 채워졌습니다.", ephemeral=True)
            return

        # 서버 아무나 누를 수 있다. can_join_round와 정산이 통과하도록 이벤트 명단에도 넣는다.
        ensure_user(conn, interaction.user)
        if round_row["event_session_id"] is not None:
            conn.execute(
                """
                INSERT OR IGNORE INTO event_participants (event_session_id, user_id, joined_at)
                VALUES (?, ?, ?)
                """,
                (int(round_row["event_session_id"]), user_id, now_text()),
            )
        conn.execute(
            "INSERT INTO trust_registrations (round_id, user_id, registered_at) VALUES (?, ?, ?)",
            (round_id, user_id, now_text()),
        )
        conn.execute(
            """
            INSERT INTO trust_pairings (round_id, user_a_id, user_b_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (round_id, applied["user_a_id"], applied["user_b_id"], now_text()),
        )
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )
        title = str(round_row["title"])
        channel_id = str(round_row["channel_id"])

    close_at = float(applied["close_at"])
    bot.schedule_auto_close(round_id, close_at)

    channel = await resolve_text_channel(channel_id)
    if channel is None:
        await interaction.followup.send("긴급 투입했지만 채널을 찾지 못해 방을 열지 못했습니다.", ephemeral=True)
        return

    # 새 방을 만들기 전에 알린다. 갓 투입된 짝에게 자기 자신에 대한 공지가 가지 않는다.
    await announce_emergency_join(round_id, close_at)

    user_a_id = str(applied["user_a_id"])
    user_b_id = str(applied["user_b_id"])
    if round_type == "trust":
        created = await create_pair_thread(
            channel,
            f"{title} · 긴급 투입 세트",
            [user_a_id, user_b_id],
            f"딜레마의 섬 #{round_id} 긴급 투입 협상방",
            trust_decision_view(round_id),
            content=trust_pair_intro(user_a_id, user_b_id),
        )
    else:
        created = await create_pair_thread(
            channel,
            f"{title} · 긴급 투입 모래성",
            [user_a_id, user_b_id],
            f"모래성 게임 #{round_id} 긴급 투입 방",
            sand_play_view(round_id),
            content=sand_pair_intro(user_a_id, user_b_id),
            embed=build_sand_pair_embed(title, applied["pair"], extra),
        )

    if created is None:
        await interaction.followup.send(
            "긴급 투입은 됐지만 비공개 방을 열지 못했습니다. 운영자에게 알려주세요.",
            ephemeral=True,
        )
    else:
        thread, message = created
        save_pair_thread_id(round_id, user_a_id, user_b_id, str(thread.id))
        if round_type == "sand":
            store_sand_thread_ids(
                round_id, {(user_a_id, user_b_id): (str(thread.id), str(message.id))}
            )
        await interaction.followup.send(
            f"긴급 투입 완료! <@{user_a_id}>님과의 방이 열렸습니다.",
            ephemeral=True,
        )

    if round_type == "sand":
        await refresh_main_sand_board(round_id)
    else:
        await refresh_main_trust_board(round_id)


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
    """(두 수의 차이, 무너지는 덜기 횟수).

    차이만큼은 안전하게 덜 수 있고 그 횟수를 초과하는 순간 무너진다.
    예: 9와 7이면 차이가 2 -> 2번까지 성공, 3번째에 붕괴.
    차이가 0이면 첫 덜기에서 바로 무너진다.
    """
    raw_difference = abs(int(pair["number_a"]) - int(pair["number_b"]))
    return raw_difference, raw_difference + 1


def sand_games_complete(extra: dict[str, Any]) -> bool:
    pairs = extra.get("sand_pairs", [])
    return bool(pairs) and all(pair.get("phase") == "resolved" for pair in pairs)


def apply_sand_action(pair: dict[str, Any], user_id: str, action: str) -> str:
    """모래성 한 수를 pair에 반영하고 행동한 사람에게 보여줄 문구를 돌려준다.

    DB도 디스코드도 건드리지 않는 순수 함수라, 마감 시 랜덤 자동 진행에서도 그대로 재사용한다.
    호출 전에 차례·단계 검증이 끝나 있어야 한다.
    """
    phase = str(pair.get("phase"))
    user_a_id = str(pair["user_a_id"])
    user_b_id = str(pair["user_b_id"])
    other_user_id = user_b_id if user_id == user_a_id else user_a_id
    raw_difference, collapse_attempt = sand_pair_threshold(pair)

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
            return "상대의 포기에 함께 포기했습니다. 두 사람 모두 -1점입니다."
        pair.update(
            {
                "phase": "awaiting_quit_response",
                "quitter_id": user_id,
                "current_user_id": other_user_id,
            }
        )
        return "포기했습니다. 상대가 포기할지, 마지막으로 모래를 덜지 결정합니다."

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
            return "모래를 덜다가 무너졌습니다. 나는 -1점, 먼저 포기한 상대는 +1점입니다."
        pair.update(
            {
                "phase": "resolved",
                "outcome": "plain_collapse",
                "result": {"collapsed_user": user_id},
                "collapse_attempt": attempt,
                "raw_difference": raw_difference,
            }
        )
        return "모래를 덜다가 무너졌습니다. -1점입니다."

    if phase == "awaiting_quit_response":
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
        return "마지막 모래 덜기에 성공했습니다. 나는 +2점, 먼저 포기한 상대는 -1점입니다."

    pair.update(
        {
            "successful_removals": attempt,
            "current_user_id": other_user_id,
        }
    )
    return f"모래 덜기 성공! 이제 <@{other_user_id}>님의 차례입니다."


def autocomplete_sand_pair(pair: dict[str, Any]) -> None:
    """마감 시점에 안 끝난 페어를 랜덤으로 끝까지 진행시킨다.

    숫자를 안 낸 사람에게는 1~10을 무작위로 배정하고, 남은 턴은 포기/덜기를 무작위로 고른다.
    미선택으로 0점이 되는 대신 어떤 식으로든 결과가 나오게 하는 것이 목적이다.
    """
    if str(pair.get("phase")) == "resolved":
        return

    for key in ("number_a", "number_b"):
        if pair.get(key) is None:
            pair[key] = random.randint(1, 10)
    if str(pair.get("phase")) == "number_selection":
        pair["phase"] = "active"
    if not pair.get("current_user_id"):
        pair["current_user_id"] = random.choice([pair["user_a_id"], pair["user_b_id"]])

    # 차이는 최대 9라 붕괴까지 최대 10수 + 포기 응답 1수. 상한은 순수 안전장치다.
    for _ in range(20):
        if str(pair.get("phase")) == "resolved":
            break
        apply_sand_action(pair, str(pair["current_user_id"]), random.choice(["quit", "remove"]))

    pair["auto_completed"] = True


def sand_pair_thread_matches(pair: dict[str, Any], channel_id: int | None) -> bool:
    """누른 곳이 본인 짝의 비공개 방인지 확인한다.

    thread_id가 없는 짝은 이 기능 이전에 시작된 라운드이므로, 종전처럼 어디서든 진행할 수 있게 둔다.
    """
    thread_id = str(pair.get("thread_id") or "")
    if not thread_id:
        return True
    return thread_id == str(channel_id)


async def refresh_main_sand_board(round_id: int) -> None:
    """메인 채널의 실시간 진행판을 다시 그린다. 게임 버튼은 각 비공개 방에만 있다."""
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
    if not round_row or not round_row["message_id"]:
        return

    extra = round_extra(round_row)
    channel = await resolve_text_channel(str(round_row["channel_id"]))
    if channel is None:
        return
    try:
        message = await channel.fetch_message(int(round_row["message_id"]))
        await message.edit(
            embed=build_sand_play_embed(round_row, extra),
            view=emergency_join_view(round_id) if extra.get("unmatched_user_id") else None,
        )
    except (discord.HTTPException, ValueError):
        pass


def save_sand_pair_message_id(round_id: int, pair: dict[str, Any], message_id: str) -> None:
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row:
            return
        extra = round_extra(round_row)
        target = find_sand_pair(extra, str(pair["user_a_id"]))
        if not target:
            return
        target["play_message_id"] = message_id
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )


async def refresh_sand_pair_board(interaction: discord.Interaction, round_id: int) -> None:
    """누른 사람의 방 진행판을 다시 그린다.

    버튼을 누를 때마다 개인 응답(ephemeral)이 방에 쌓여서 버튼이 달린 진행판이 위로 밀려 올라간다.
    그러면 다음 차례에 버튼을 누르려고 스크롤을 올려야 한다. 그래서 진행 중인 짝은 진행판을 지우고
    맨 아래에 다시 붙인다. 끝난 짝은 더 누를 버튼이 없으므로 제자리에서 비활성화만 한다.

    숫자 선택은 두 사람이 동시에 누를 수 있다. 그때 `interaction.message`는 상대가 이미 재게시해서
    사라진 옛 메시지일 수 있으므로, 락을 잡고 DB에서 현재 진행판 id를 다시 읽어야 방이 두 개로 갈리지 않는다.
    """
    channel = interaction.channel
    if channel is None:
        return

    async with bot.sand_board_lock(channel.id):
        with db() as conn:
            round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row:
            return

        extra = round_extra(round_row)
        pair = find_sand_pair(extra, str(interaction.user.id))
        if not pair:
            return

        resolved = str(pair.get("phase")) == "resolved"
        embed = build_sand_pair_embed(str(round_row["title"]), pair, extra)
        view = sand_play_view(round_id, disabled=resolved)

        board_message_id = str(pair.get("play_message_id") or "")
        if not board_message_id and interaction.message:
            board_message_id = str(interaction.message.id)

        # 방이 없는 옛 라운드(메인 채널 진행)나 이미 끝난 짝은 제자리에서 갱신한다.
        if resolved or not isinstance(channel, discord.Thread):
            if board_message_id:
                try:
                    message = await channel.fetch_message(int(board_message_id))
                    await message.edit(embed=embed, view=view)
                except (discord.NotFound, discord.HTTPException, ValueError):
                    pass
            return

        if board_message_id:
            try:
                old_board = await channel.fetch_message(int(board_message_id))
                await old_board.delete()
            except (discord.NotFound, discord.HTTPException, ValueError):
                pass
        try:
            new_board = await channel.send(embed=embed, view=view)
        except discord.HTTPException:
            return
        save_sand_pair_message_id(round_id, pair, str(new_board.id))


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
        channel_id = str(round_row["channel_id"])
        complete = sand_games_complete(round_extra(round_row))

    await interaction.response.send_message(private_message, ephemeral=True)

    # 누른 사람의 방만 갱신한다. 다른 짝의 상황은 이 방에 새어 나가지 않는다.
    await refresh_sand_pair_board(interaction, round_id)
    await refresh_main_sand_board(round_id)

    if not complete:
        return
    result = close_round_record(round_id, interaction.guild, closed_by="모든 1:1 게임 종료")
    if not result:
        return
    task = bot.auto_close_tasks.pop(round_id, None)
    if task:
        task.cancel()
    await publish_sand_pair_results(round_id)

    # followup은 버튼을 누른 비공개 방으로 들어간다. 라운드 결과는 메인 채널에 알려야 한다.
    _, result_embed = result
    channel = await resolve_text_channel(channel_id)
    if channel is not None:
        try:
            await channel.send(embed=result_embed, view=planned_mini_game_view(round_id))
        except discord.HTTPException:
            pass


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
        if not sand_pair_thread_matches(pair, interaction.channel_id):
            await interaction.response.send_message(
                "자신에게 배정된 1:1 모래성 방에서만 진행할 수 있습니다.",
                ephemeral=True,
            )
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
        else " 상대의 선택을 기다립니다."
    )
    await refresh_or_finish_sand_round(interaction, round_id, f"`{number}`을 확정했습니다.{suffix}")


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
        if not sand_pair_thread_matches(pair, interaction.channel_id):
            await interaction.response.send_message(
                "자신에게 배정된 1:1 모래성 방에서만 진행할 수 있습니다.",
                ephemeral=True,
            )
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

        private_message = apply_sand_action(pair, user_id, action)

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
        self.afk_coin_task: asyncio.Task[None] | None = None
        self.sand_board_locks: dict[int, asyncio.Lock] = {}

    def sand_board_lock(self, thread_id: int) -> asyncio.Lock:
        """방별 진행판 재게시를 직렬화한다. 두 사람이 동시에 눌러도 진행판이 하나만 남는다."""
        return self.sand_board_locks.setdefault(thread_id, asyncio.Lock())

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
        if self.afk_coin_task is None or self.afk_coin_task.done():
            self.afk_coin_task = asyncio.create_task(self.afk_coin_loop())
        # 로그인 + 진행 중이던 라운드 타이머 복구까지 끝나야 배포가 성공으로 처리된다.
        sd_notify("READY=1")

    async def afk_coin_loop(self) -> None:
        """음성채널에 있는 사람에게 1분마다 잠수 코인을 준다.

        입장/퇴장을 추적하지 않고 매 틱마다 현재 접속자를 훑는다. 코인이 DB에만 쌓이므로
        재시작 후 복구할 상태가 없다 (재시작 시 손해는 1분 미만).
        """
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(AFK_TICK_SECONDS)
            try:
                self.grant_voice_afk_coins()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - 틱 한 번 실패로 루프가 죽으면 안 된다.
                print(f"잠수 코인 적립 실패: {error}")

    def grant_voice_afk_coins(self) -> None:
        with db() as conn:
            for guild in self.guilds:
                for voice_channel in guild.voice_channels:
                    for member in voice_channel.members:
                        if member.bot:
                            continue
                        ensure_user(conn, member)
                        grant_afk_coins(conn, str(member.id), AFK_COIN_PER_TICK)

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
                await create_trust_pair_threads(transition)
                await update_trust_main_message(transition)
                if transition["state"] == "decision":
                    self.schedule_auto_close(round_id, float(transition["close_at"]))
                return

            if row["type"] == "sand" and round_extra(row).get("sand_phase") == "registration":
                transition = start_sand_game(round_id)
                if not transition:
                    return
                await create_sand_pair_threads(transition)
                await update_trust_main_message(transition)
                if transition["state"] == "playing":
                    self.schedule_auto_close(round_id, float(transition["close_at"]))
                return

            result = close_round_record(round_id, guild, closed_by="자동 마감")
            if not result:
                return

            _, embed = result
            if row["type"] == "trust":
                await publish_trust_pair_results(round_id)
            if row["type"] == "sand":
                await publish_sand_pair_results(round_id)
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

        if action == "emergency":
            try:
                round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return
            await handle_emergency_join(interaction, round_id)
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
                await interaction.response.send_message("잘못된 딜레마의 섬 선택입니다.", ephemeral=True)
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
                "거래 제안 방식은 랜덤 매칭 방식으로 변경되었습니다. 새 딜레마의 섬에 참여해주세요.",
                ephemeral=True,
            )
            return

        if action == "trustcontract":
            await interaction.response.send_message(
                "거래 제안 방식은 랜덤 매칭 방식으로 변경되었습니다. 새 딜레마의 섬에 참여해주세요.",
                ephemeral=True,
            )
            return

        if action in {"trustself", "trust"}:
            await interaction.response.send_message(
                "직접 점수 투표 방식은 랜덤 매칭 신뢰/배신 방식으로 변경되었습니다. 새 딜레마의 섬에 참여해주세요.",
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

            # can_join_round는 반드시 with 블록 안에서 부른다. 밖으로 나가면 커넥션이 닫혀 있다.
            with db() as conn:
                round_row = conn.execute(
                    """
                    SELECT id FROM rounds
                    WHERE id = ? AND status = 'open' AND discussion_thread_id = ?
                    """,
                    (round_id, str(interaction.channel.id)),
                ).fetchone()
                joinable = bool(round_row) and can_join_round(
                    conn, round_id, str(interaction.user.id)
                )

            if not round_row:
                await interaction.response.send_message("이미 마감되었거나 유효하지 않은 토론방입니다.", ephemeral=True)
                return
            if not joinable:
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
            if round_row["type"] == "top3" and str(interaction.user.id) in {
                str(candidate["user_id"])
                for candidate in round_extra(round_row).get("candidates", [])
            }:
                await interaction.response.send_message(
                    "견제 대상인 상위 3명은 투표할 수 없습니다.",
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
            "토론 스레드에서 익명으로 몇 번이든 발언할 수 있습니다.\n"
            "마감 후 다수파 선택자에게 +1점이 지급됩니다.\n"
            "단, 한쪽이 0명이면 밸런스가 성립하지 않으므로 고른 사람 전원 -2점입니다."
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
            "해체 토론방에서 익명으로 몇 번이든 심리전과 블러핑을 할 수 있습니다.\n"
            "마감 후 안전한 버튼을 고른 사람에게만 +1점이 지급됩니다."
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
        value=" | ".join(f"{level}강화" for level in CUTTING_LEVEL_CHOICES),
        inline=False,
    )
    # 확률과 보상은 상수에서 만든다. 손으로 적어두면 상수를 바꿨을 때 안내문만 옛날 값으로 남는다.
    embed.add_field(
        name="규칙",
        value=(
            f"강화 성공 확률: {cutting_success_rate_text()}\n"
            "처음 실패하기 직전의 강화 단계를 맞히는 게임입니다.\n"
            f"정답 보상: {cutting_reward_text()}\n"
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
            "후보로 오른 3명은 투표할 수 없습니다.\n"
            "`선택하지 않는다` 득표수가 세 후보 득표수 합보다 많으면 `선택하지 않는다`를 고른 사람 전원이 +1점입니다.\n"
            "그렇지 않으면 세 후보 중 가장 많이 선택된 1명이 -1점입니다.\n"
            "아무도 투표하지 않으면 이 라운드는 무효 처리됩니다."
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
        name="진행 게임",
        value="딜레마의 섬",
        inline=False,
    )
    embed.add_field(
        name="메인 규칙",
        value=(
            "무작위로 매칭된 두 명의 참가자가 신뢰와 배신을 선택합니다.\n"
            "- 상호 신뢰 시 : 각 +1점\n"
            "- 한명만 배신 시 : 배신자 +2점\n"
            "- 동시 배신 시 : 각 -1점\n\n"
            "선택은 마감 전까지 바꿀 수 있고, 끝까지 고르지 않으면 랜덤으로 배정됩니다."
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


def emergency_join_view(round_id: int) -> discord.ui.View:
    """참가자가 홀수라 짝이 없는 사람이 생겼을 때만 메인 메시지에 붙는다."""
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="긴급 투입하기",
            emoji="🚨",
            style=discord.ButtonStyle.danger,
            custom_id=f"eventbot:emergency:{round_id}",
        )
    )
    return view


def emergency_join_notice(emergency_user_id: str) -> str:
    return f"<@{emergency_user_id}>님이 긴급 투입되어 전체 진행 시간이 1분 연장되었습니다."


def build_trust_decision_embed(
    round_id: int,
    title: str,
    pairs: list[tuple[str, str]],
    unmatched_user_id: str | None,
    emergency_user_id: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=title,
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="랜덤 매칭 완료",
        value=f"총 {len(pairs)}세트의 비공개 협상방을 열었습니다. 각자 초대된 방에서 대화와 선택을 진행하세요.",
        inline=False,
    )
    if unmatched_user_id:
        embed.add_field(
            name="🚨 긴급 투입 대기",
            value=(
                f"<@{unmatched_user_id}>님, 참여자가 홀수라 짝이 없습니다. 잠시만 기다려주세요!\n"
                "아직 게임에 참여하지 않은 분이 아래 `긴급 투입하기` 버튼을 누르면 둘만의 방이 새로 열리고, "
                "전체 진행 시간이 1분 연장됩니다."
            ),
            inline=False,
        )
    if emergency_user_id:
        embed.add_field(
            name="🚨 긴급 투입",
            value=emergency_join_notice(emergency_user_id),
            inline=False,
        )
    embed.add_field(
        name="선택 규칙 (3분)",
        value=(
            "비공개 협상방에서 매칭된 짝과 대화한 뒤 `신뢰` 또는 `배신`을 선택하세요. 서로의 선택은 마감 후 해당 방에서 공개됩니다.\n"
            "선택은 마감 전까지 몇 번이든 바꿀 수 있습니다. 끝까지 고르지 않으면 랜덤으로 배정됩니다."
        ),
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
        name="진행 게임",
        value="모래성 게임",
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


def sand_pair_status_text(pair: dict[str, Any]) -> str:
    phase = str(pair.get("phase"))
    if phase == "number_selection":
        selected = int(pair.get("number_a") is not None) + int(pair.get("number_b") is not None)
        return f"숫자를 고르세요(번복 불가) : {selected}/2"
    if phase == "active":
        return (
            f"<@{pair['current_user_id']}> 차례 · 성공한 모래 덜기 "
            f"{int(pair.get('successful_removals', 0))}회"
        )
    if phase == "awaiting_quit_response":
        return f"<@{pair['current_user_id']}>님이 함께 포기/마지막 덜기 선택 중"
    return SAND_OUTCOME_LABELS.get(str(pair.get("outcome")), "종료")


def build_sand_pair_embed(title: str, pair: dict[str, Any], extra: dict[str, Any]) -> discord.Embed:
    """짝 하나의 비공개 방에 걸리는 진행판. 다른 짝의 정보는 담지 않는다."""
    embed = discord.Embed(
        title=f"{title} · 1:1 모래성",
        description=f"<@{pair['user_a_id']}> ↔ <@{pair['user_b_id']}>",
        color=discord.Color.from_rgb(218, 165, 90),
    )
    embed.add_field(name="진행 상황", value=sand_pair_status_text(pair), inline=False)
    embed.add_field(name="규칙", value=SAND_RULES_TEXT, inline=False)
    if str(pair.get("phase")) != "resolved":
        embed.add_field(
            name="제한 시간",
            value=(
                f"예정 마감: {close_time_text(extra.get('play_close_at'))}\n"
                "시간 안에 끝내지 못하면 남은 숫자와 행동이 랜덤으로 배정됩니다."
            ),
            inline=False,
        )
    return embed


def build_sand_pair_result_embed(title: str, pair: dict[str, Any]) -> discord.Embed:
    number_a = pair.get("number_a")
    number_b = pair.get("number_b")
    embed = discord.Embed(
        title=f"1:1 결과 · {title}",
        description=f"<@{pair['user_a_id']}> ↔ <@{pair['user_b_id']}>",
        color=discord.Color.from_rgb(218, 165, 90),
    )
    if number_a is not None and number_b is not None:
        embed.add_field(
            name="서로의 숫자",
            value=(
                f"<@{pair['user_a_id']}>: **{number_a}**\n"
                f"<@{pair['user_b_id']}>: **{number_b}**\n"
                f"차이 {abs(int(number_a) - int(number_b))} → {abs(int(number_a) - int(number_b)) + 1}번째 덜기에서 붕괴"
            ),
            inline=False,
        )
    embed.add_field(
        name="결과",
        value=SAND_OUTCOME_LABELS.get(str(pair.get("outcome")), "미완료"),
        inline=False,
    )
    embed.add_field(name="정산", value=sand_pair_settlement_text(pair), inline=False)
    if pair.get("auto_completed"):
        embed.add_field(name="자동 진행", value="제한 시간이 끝나 남은 선택이 랜덤으로 배정되었습니다.", inline=False)
    return embed


def build_sand_play_embed(round_row: sqlite3.Row, extra: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title=str(round_row["title"]),
        description="각자 초대된 1:1 비공개 방에서 숫자와 행동을 선택하세요. 선택 내용은 비공개입니다.",
        color=discord.Color.from_rgb(218, 165, 90),
    )
    lines = [
        f"{index}. <@{pair['user_a_id']}> ↔ <@{pair['user_b_id']}> · {sand_pair_status_text(pair)}"
        for index, pair in enumerate(extra.get("sand_pairs", []), start=1)
    ]
    embed.add_field(name="1:1 진행 현황", value=field_text(lines), inline=False)
    unmatched_user_id = str(extra.get("unmatched_user_id") or "")
    if unmatched_user_id:
        embed.add_field(
            name="🚨 긴급 투입 대기",
            value=(
                f"<@{unmatched_user_id}>님, 참여자가 홀수라 짝이 없습니다. 잠시만 기다려주세요!\n"
                "아직 게임에 참여하지 않은 분이 아래 `긴급 투입하기` 버튼을 누르면 둘만의 방이 새로 열리고, "
                "전체 진행 시간이 1분 연장됩니다."
            ),
            inline=False,
        )
    emergency_user_id = str(extra.get("emergency_user_id") or "")
    if emergency_user_id:
        embed.add_field(
            name="🚨 긴급 투입",
            value=emergency_join_notice(emergency_user_id),
            inline=False,
        )
    embed.add_field(
        name="선택 제한 시간",
        value=(
            f"예정 마감: {close_time_text(extra.get('play_close_at'))}\n"
            "시간 안에 끝내지 못하면 남은 숫자와 행동이 랜덤으로 배정되어 자동으로 결과가 확정됩니다."
        ),
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


def cutting_progress_bar(current_level: int, max_level: int) -> str:
    """강화 진척도 막대. 초록(🟩)은 성공한 강화, 흰색(◻️)은 남은 강화. 예: 3강 → 🟩🟩🟩◻️◻️◻️"""
    filled = max(0, min(current_level, max_level))
    return "🟩" * filled + "◻️" * (max_level - filled)


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
        name="진척도",
        value=f"{cutting_progress_bar(current_level, max_level)}  ({current_level}/{max_level}강)",
        inline=False,
    )
    embed.add_field(
        name="직전 시도",
        value=logs[-1] if logs else "아직 시도 없음",
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
    max_level = int(extra.get("max_level", 6))
    embed.add_field(
        name="진척도",
        value=f"{cutting_progress_bar(result_level, max_level)}  ({result_level}/{max_level}강)",
        inline=False,
    )
    embed.add_field(name="마지막 시도", value=logs[-1] if logs else "-", inline=False)
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

            scored = False
            if not a_users and not b_users:
                settlement = "투표가 없어 점수가 지급되지 않았습니다."
            elif not a_users or not b_users:
                # 한쪽으로 몰표. 밸런스가 성립하지 않았으므로 고른 사람 전원에게 패널티.
                one_sided_users = a_users or b_users
                one_sided_choice = "A" if a_users else "B"
                for user_id in one_sided_users:
                    add_score(
                        conn,
                        user_id,
                        member_name(guild, user_id),
                        -2,
                        f"라운드 #{round_row['id']} 몰표 패널티",
                        round_row["id"],
                    )
                scored = True
                settlement = (
                    f"모두 `{one_sided_choice}`만 골라 밸런스가 성립하지 않았습니다. "
                    f"선택자 {len(one_sided_users)}명 전원 -2점"
                )
            elif len(a_users) == len(b_users):
                settlement = "무승부라 점수가 지급되지 않았습니다."
            else:
                winner = "A" if len(a_users) > len(b_users) else "B"
                winner_users = a_users if winner == "A" else b_users
                for user_id in winner_users:
                    add_score(
                        conn,
                        user_id,
                        member_name(guild, user_id),
                        1,
                        f"라운드 #{round_row['id']} 다수파 선택",
                        round_row["id"],
                    )
                scored = True
                settlement = f"다수파 `{winner}` 선택자 전원 +1점"

            embed = discord.Embed(
                title=f"결과 · {round_row['title']}",
                description=round_row["question"],
                color=discord.Color.green() if scored else discord.Color.light_grey(),
            )
            # 누가 무엇을 골랐는지는 공개하지 않는다. 인원수만.
            embed.add_field(
                name=f"A. {round_row['option_a']}",
                value=f"{len(a_users)}명",
                inline=True,
            )
            embed.add_field(
                name=f"B. {round_row['option_b']}",
                value=f"{len(b_users)}명",
                inline=True,
            )
            embed.add_field(name="정산", value=settlement, inline=False)
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

            # 마감까지 안 고른 사람은 랜덤 배정한다. 미선택으로 짝 전체가 0점이 되는 일이 없도록.
            # 협상방 결과 공개(publish_trust_pair_results)가 votes를 다시 읽으므로 DB에도 남긴다.
            auto_assigned = 0
            for pair in pairs:
                for user_id in (str(pair["user_a_id"]), str(pair["user_b_id"])):
                    if vote_choices.get(user_id):
                        continue
                    assigned = random.choice(["trust", "betray"])
                    ts = now_text()
                    conn.execute(
                        """
                        INSERT INTO votes (round_id, user_id, choice, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(round_id, user_id) DO UPDATE SET
                            choice = excluded.choice,
                            updated_at = excluded.updated_at
                        """,
                        (round_row["id"], user_id, assigned, ts, ts),
                    )
                    vote_choices[user_id] = assigned
                    auto_assigned += 1

            for pair in pairs:
                user_a_id = str(pair["user_a_id"])
                user_b_id = str(pair["user_b_id"])
                user_a_choice = vote_choices.get(user_a_id)
                user_b_choice = vote_choices.get(user_b_id)
                if not user_a_choice or not user_b_choice:
                    outcome_counts["incomplete"] += 1
                    continue

                if user_a_choice == "trust" and user_b_choice == "trust":
                    outcome = "mutual"
                    for user_id in (user_a_id, user_b_id):
                        add_score(
                            conn,
                            user_id,
                            member_name(guild, user_id),
                            1,
                            f"라운드 #{round_row['id']} 딜레마의 섬 상호 신뢰",
                            round_row["id"],
                        )
                elif user_a_choice == "betray" and user_b_choice == "betray":
                    outcome = "double"
                    for user_id in (user_a_id, user_b_id):
                        add_score(
                            conn,
                            user_id,
                            member_name(guild, user_id),
                            -1,
                            f"라운드 #{round_row['id']} 딜레마의 섬 동시 배신",
                            round_row["id"],
                        )
                else:
                    outcome = "single"
                    betrayer_id = user_a_id if user_a_choice == "betray" else user_b_id
                    add_score(
                        conn,
                        betrayer_id,
                        member_name(guild, betrayer_id),
                        2,
                        f"라운드 #{round_row['id']} 딜레마의 섬 단독 배신",
                        round_row["id"],
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
            embed.add_field(
                name="전체 결과 분포",
                value=" · ".join(
                    [
                        f"신뢰 : {outcome_counts['mutual']}",
                        f"단독 배신 : {outcome_counts['single']}",
                        f"동시 배신 : {outcome_counts['double']}",
                    ]
                    + (
                        [f"미선택 : {outcome_counts['incomplete']}"]
                        if outcome_counts["incomplete"]
                        else []
                    )
                ),
                inline=False,
            )
            if auto_assigned:
                embed.add_field(
                    name="미선택 랜덤 배정",
                    value=f"{auto_assigned}명",
                    inline=False,
                )
            unmatched_user_id = str(extra.get("unmatched_user_id") or "")
            if unmatched_user_id:
                embed.add_field(
                    name="매칭 대기",
                    value="1명",
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
            auto_completed_count = 0
            # 마감 시점에 안 끝난 짝은 랜덤으로 끝까지 진행시킨다. 미완료 0점은 남기지 않는다.
            for pair in extra.get("sand_pairs", []):
                if str(pair.get("phase")) != "resolved":
                    autocomplete_sand_pair(pair)
                    auto_completed_count += 1

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
                elif outcome == "plain_collapse":
                    collapsed_user_id = str(result["collapsed_user"])
                    add_score(
                        conn, collapsed_user_id, member_name(guild, collapsed_user_id), -1,
                        f"라운드 #{round_row['id']} 모래성 일반 붕괴", round_row["id"],
                    )
                else:
                    result_lines.append(
                        f"<@{user_a_id}> ↔ <@{user_b_id}>: 제한 시간 내 미완료 (0점)"
                    )
                    continue

                completed_count += 1
                auto_mark = " · 자동 진행" if pair.get("auto_completed") else ""
                result_lines.append(
                    f"<@{user_a_id}> ↔ <@{user_b_id}>: {SAND_OUTCOME_LABELS[outcome]} "
                    f"({number_text}) · {sand_pair_settlement_text(pair)}{auto_mark}"
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
            completion_text = f"완료된 1:1 게임 {completed_count}쌍의 점수를 반영했습니다."
            if auto_completed_count:
                completion_text += (
                    f"\n제한 시간 내에 끝나지 않은 {auto_completed_count}쌍은 랜덤으로 자동 진행했습니다."
                )
            embed.add_field(name="정산 완료", value=completion_text, inline=False)
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
        elif round_row["type"] == "top3":
            candidates: list[dict[str, Any]] = extra["candidates"]
            candidate_ids = [str(candidate["user_id"]) for candidate in candidates]
            grouped: dict[str, list[str]] = {}
            for row in votes:
                grouped.setdefault(row["choice"], []).append(row["user_id"])

            none_voters = grouped.get(TOP3_NONE_CHOICE, [])
            none_votes = len(none_voters)
            candidate_vote_sum = sum(len(grouped.get(user_id, [])) for user_id in candidate_ids)
            candidate_lines = []
            for candidate in candidates:
                user_id = str(candidate["user_id"])
                candidate_lines.append(
                    f"<@{user_id}>: {len(grouped.get(user_id, []))}표"
                )

            if none_votes + candidate_vote_sum == 0:
                outcome = "아무도 투표하지 않아 이번 라운드는 무효입니다."
                settlement = "점수 변동 없음"
            elif none_votes > candidate_vote_sum:
                for user_id in none_voters:
                    add_score(
                        conn,
                        user_id,
                        member_name(guild, user_id),
                        1,
                        f"라운드 #{round_row['id']} 상위 3명 견제 성공",
                        round_row["id"],
                    )
                outcome = "`선택하지 않는다`가 후보 득표 합보다 많았습니다."
                settlement = f"`선택하지 않는다`를 고른 {none_votes}명 전원 +1점"
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
        else:
            # 제거된 게임 타입(예: 과거의 'grid')이 열린 채 남아 있는 경우. 정산 없이 닫는다.
            embed = discord.Embed(
                title=f"결과 · {round_row['title']}",
                description=round_row["title"],
                color=discord.Color.light_grey(),
            )
            embed.add_field(
                name="정산",
                value="더 이상 지원하지 않는 게임이라 점수 없이 마감했습니다.",
                inline=False,
            )

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


async def ensure_pair_thread_permissions(interaction: discord.Interaction, 게임: str) -> bool:
    """딜레마의 섬·모래성은 짝마다 비공개 스레드를 연다. 시작 전에 권한을 확인한다."""
    game_name = GAME_NAMES[게임]
    guild = interaction.guild
    interaction_channel = interaction.channel
    if guild is None or not isinstance(interaction_channel, discord.TextChannel):
        await interaction.response.send_message(
            f"{game_name}은(는) 스레드가 아닌 서버 텍스트 채널에서 시작해주세요.",
            ephemeral=True,
        )
        return False

    bot_member = guild.me
    if bot_member is None:
        await interaction.response.send_message("서버에서 봇 권한을 확인하지 못했습니다.", ephemeral=True)
        return False

    permissions = interaction_channel.permissions_for(bot_member)
    if (
        permissions.create_private_threads
        and permissions.send_messages_in_threads
        and permissions.manage_threads
    ):
        return True

    await interaction.response.send_message(
        f"{game_name}에는 봇의 `비공개 스레드 만들기`, `스레드에서 메시지 보내기`, "
        "`스레드 관리` 권한이 필요합니다.",
        ephemeral=True,
    )
    return False


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
        app_commands.Choice(name="딜레마의 섬", value="trust"),
        app_commands.Choice(name="모래성 게임", value="sand"),
    ],
    미니게임=[
        app_commands.Choice(name="밸런스 게임", value="balance"),
        app_commands.Choice(name="4선 폭탄 해체", value="defuse4"),
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
        app_commands.Choice(name="밸런스 게임", value="balance"),
        app_commands.Choice(name="4선 폭탄 해체", value="defuse4"),
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
    예정_미니게임: str | None = None,
    단독_미니게임: bool = False,
) -> None:
    if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("서버 텍스트 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return

    if 게임 in MAIN_GAME_TYPES and not await ensure_pair_thread_permissions(interaction, 게임):
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
                    "밸런스 게임은 `선택_a`와 `선택_b`가 필요합니다.",
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
                    "밸런스 게임 라운드",
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
            view = vote_view(round_id, BALANCE_VOTE_CHOICES)
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
            view = discord.utils.MISSING
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
            view = discord.utils.MISSING
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
        if 게임 == "sand":
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
    if 게임 in {"balance", "defuse4"}:
        try:
            thread_name = f"{제목} | 토론방"[:100]
            # 보관되면 일반 유저의 버튼 클릭이 막힌다. 라운드가 끝날 때까지 살아 있도록 넉넉히 잡는다.
            thread = await message.create_thread(name=thread_name, auto_archive_duration=1440)
            button_message = await thread.send(
                embed=build_anonymous_guide_embed(0),
                view=thread_discussion_view(round_id, 게임),
            )
            # 발언이 쌓여도 버튼이 항상 맨 아래에 오도록, 발언마다 이 메시지를 지우고 다시 보낸다.
            extra["anon_button_message_id"] = str(button_message.id)
            if 게임 == "balance":
                notice_embed = build_thread_notice_embed(
                    round_id,
                    thread,
                    "토론방이 열렸습니다",
                    "버튼을 눌러 익명으로 설득, 블러핑, 진실 말하기를 할 수 있습니다. 횟수 제한은 없습니다.",
                )
            else:
                notice_embed = build_thread_notice_embed(
                    round_id,
                    thread,
                    "해체 토론방이 열렸습니다",
                    "버튼을 눌러 익명으로 관리자의 힌트를 해석해 안전 색을 추리해보세요. 블러핑과 역추리 모두 가능합니다.",
                )
            # 스레드를 잠그면 `스레드 관리` 권한이 없는 유저의 버튼 클릭까지 디스코드가 막는다.
            # 익명성은 on_message의 일반 메시지 삭제로만 지킨다.
            await interaction.followup.send(embed=notice_embed)
            discussion_thread_id = str(thread.id)
        except discord.HTTPException:
            discussion_thread_id = None

    with db() as conn:
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
        await interaction.response.defer(ephemeral=True)
        transition = start_trust_decision(int(round_row["id"]))
        if not transition:
            await interaction.followup.send("참여자 모집을 마감하지 못했습니다.", ephemeral=True)
            return
        task = bot.auto_close_tasks.pop(int(round_row["id"]), None)
        if task:
            task.cancel()
        await create_trust_pair_threads(transition)
        await update_trust_main_message(transition)
        if transition["state"] == "decision":
            bot.schedule_auto_close(int(round_row["id"]), float(transition["close_at"]))
            await interaction.followup.send("참여자 모집을 마감하고 랜덤 매칭 및 3분 선택을 시작했습니다.", ephemeral=True)
        else:
            await interaction.followup.send("참여자가 부족해 딜레마의 섬을 취소했습니다.", ephemeral=True)
        return

    if round_row["type"] == "sand" and round_extra(round_row).get("sand_phase") == "registration":
        # 짝마다 비공개 방을 여느라 API 호출이 여러 번이다. 3초 응답 제한을 넘기므로 먼저 defer한다.
        await interaction.response.defer(ephemeral=True)
        transition = start_sand_game(int(round_row["id"]))
        if not transition:
            await interaction.followup.send("참여자 모집을 마감하지 못했습니다.", ephemeral=True)
            return
        task = bot.auto_close_tasks.pop(int(round_row["id"]), None)
        if task:
            task.cancel()
        await create_sand_pair_threads(transition)
        await update_trust_main_message(transition)
        if transition["state"] == "playing":
            bot.schedule_auto_close(int(round_row["id"]), float(transition["close_at"]))
            await interaction.followup.send("참여자 모집을 마감하고 1:1 모래성 게임을 시작했습니다.", ephemeral=True)
        else:
            await interaction.followup.send("참여자가 부족해 모래성 게임을 취소했습니다.", ephemeral=True)
        return

    # 짝별 방에 결과를 공개해야 하는 라운드는 응답 전에 API 호출이 여러 번이라 먼저 defer한다.
    round_extra_row = round_extra(round_row)
    is_trust_decision = (
        round_row["type"] == "trust" and round_extra_row.get("trust_phase") == "decision"
    )
    is_sand_playing = round_row["type"] == "sand" and round_extra_row.get("sand_phase") == "playing"
    publishes_pair_results = is_trust_decision or is_sand_playing
    if publishes_pair_results:
        await interaction.response.defer()

    result = close_round_record(int(round_row["id"]), interaction.guild)
    if not result:
        if publishes_pair_results:
            await interaction.edit_original_response(content="현재 열린 라운드가 없습니다.")
        else:
            await interaction.response.send_message("현재 열린 라운드가 없습니다.", ephemeral=True)
        return

    task = bot.auto_close_tasks.pop(int(round_row["id"]), None)
    if task:
        task.cancel()

    _, embed = result
    if publishes_pair_results:
        if is_trust_decision:
            await publish_trust_pair_results(int(round_row["id"]))
        else:
            await publish_sand_pair_results(int(round_row["id"]))
        await interaction.edit_original_response(
            embed=embed,
            view=planned_mini_game_view(int(round_row["id"])),
        )
    else:
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
                    thread = bot.get_channel(pair_thread_id) or await bot.fetch_channel(pair_thread_id)
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


@bot.tree.command(name="이벤트리스트", description="진행 중인 이벤트와 라운드를 확인합니다.")
@app_commands.default_permissions()
@admin_only()
async def list_events(interaction: discord.Interaction) -> None:
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
        phase = extra.get("trust_phase") or extra.get("sand_phase")
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


@bot.tree.command(name="잠수", description="내 잠수 코인을 확인합니다.")
async def show_afk_coins(interaction: discord.Interaction) -> None:
    with db() as conn:
        ensure_user(conn, interaction.user)
        coins = get_afk_coins(conn, str(interaction.user.id))

    await interaction.response.send_message(
        f"현재 잠수 코인 **{coins}개**입니다.\n"
        f"음성채널에 있으면 1분마다 {AFK_COIN_PER_TICK}개씩 쌓이고, "
        f"`/지목` 1회에 {SCORE_REVEAL_COST}개를 씁니다.",
        ephemeral=True,
    )


@bot.tree.command(
    name="지목",
    description=f"잠수 코인 {SCORE_REVEAL_COST}개를 써서 한 사람의 점수를 확인합니다.",
)
@app_commands.describe(사용자="점수를 확인할 사람")
async def reveal_score(interaction: discord.Interaction, 사용자: discord.Member) -> None:
    if 사용자.bot:
        await interaction.response.send_message("봇의 점수는 확인할 수 없습니다.", ephemeral=True)
        return
    if 사용자.id == interaction.user.id:
        await interaction.response.send_message(
            "내 점수는 `/점수`로 코인 없이 확인할 수 있습니다.",
            ephemeral=True,
        )
        return

    user_id = str(interaction.user.id)
    with db() as conn:
        ensure_user(conn, interaction.user)
        if not spend_afk_coins(conn, user_id, SCORE_REVEAL_COST):
            coins = get_afk_coins(conn, user_id)
            await interaction.response.send_message(
                f"잠수 코인이 부족합니다. (현재 {coins}개 / 필요 {SCORE_REVEAL_COST}개)",
                ephemeral=True,
            )
            return
        remaining = get_afk_coins(conn, user_id)
        target_row = conn.execute(
            "SELECT total_score FROM users WHERE user_id = ?", (str(사용자.id),)
        ).fetchone()

    target_score = int(target_row["total_score"]) if target_row else 0
    await interaction.response.send_message(
        f"<@{사용자.id}>님의 현재 점수는 **{target_score}점**입니다.\n"
        f"잠수 코인 {SCORE_REVEAL_COST}개를 사용했습니다. (잔여 {remaining}개)",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="점수", description="3등 점수와 내 점수를 확인합니다.")
async def show_score(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버 안에서만 점수를 확인할 수 있습니다.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    current_bot_id = str(interaction.client.user.id) if interaction.client.user else ""
    with db() as conn:
        ensure_user(conn, interaction.user)
        my_row = conn.execute(
            "SELECT total_score FROM users WHERE user_id = ?", (str(interaction.user.id),)
        ).fetchone()
        candidates = fetch_ranking_rows(conn, current_bot_id)

    third_score = None
    try:
        found = 0
        for candidate in candidates:
            if await current_guild_member(interaction.guild, str(candidate["user_id"])) is None:
                continue
            found += 1
            if found == 3:
                third_score = int(candidate["total_score"])
                break
    except discord.HTTPException:
        await interaction.followup.send(
            "현재 서버 멤버를 확인하지 못했습니다. 잠시 후 다시 시도해주세요.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        format_my_score_message(third_score, int(my_row["total_score"])),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


def backup_database(dest: Path) -> None:
    """SQLite 온라인 백업 API로 DB를 복사한다.

    WAL 모드에서는 최근 커밋이 아직 -wal 파일에만 있을 수 있어서 DB 파일만
    단순 복사하면 그 내용이 빠진다. backup()은 WAL까지 반영한 일관된 스냅샷을
    만들고, 봇이 돌고 있는 중에 호출해도 안전하다.
    """
    src = sqlite3.connect(DB_PATH, timeout=10.0)
    dst = sqlite3.connect(dest)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()


@bot.tree.command(name="데이터백업", description="SQLite 데이터베이스를 백업합니다.")
@app_commands.default_permissions()
@admin_only()
async def backup_data(interaction: discord.Interaction) -> None:
    if not DB_PATH.exists():
        await interaction.response.send_message("아직 데이터베이스 파일이 없습니다.", ephemeral=True)
        return

    backup_dir = DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"event_game_{timestamp}.sqlite3"
    score_report_path = backup_dir / f"score_report_{timestamp}.csv"
    backup_database(backup_path)
    current_bot_id = str(interaction.client.user.id) if interaction.client.user else ""
    with db() as conn:
        score_rows = fetch_admin_score_rows(conn, current_bot_id)
    write_admin_score_report(score_report_path, score_rows)
    await interaction.response.send_message(
        f"백업 완료: `{backup_path.name}`\n관리자 점수 데이터: `{score_report_path.name}`",
        file=discord.File(score_report_path, filename=score_report_path.name),
        ephemeral=True,
    )


def sd_notify(state: str) -> None:
    """systemd에 상태를 알린다 (Type=notify).

    on_ready에서 READY=1을 보내므로 `systemctl restart`는 봇이 실제로 디스코드
    로그인을 마쳐야 성공으로 끝난다. 배포 스크립트는 이 종료 코드만 보면 된다.
    NOTIFY_SOCKET이 없는 로컬 실행에서는 아무것도 하지 않는다.
    """
    address = os.getenv("NOTIFY_SOCKET")
    if not address or not hasattr(socket, "AF_UNIX"):
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(state.encode())
    except OSError as exc:
        print(f"sd_notify 실패({state}): {exc}", flush=True)


def _install_shutdown_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """systemd가 보내는 SIGTERM에 게이트웨이를 정상 종료시킨다.

    discord.Client.run()은 SIGTERM을 잡지 않아 프로세스가 정리 없이 즉사하고,
    처리 중이던 상호작용 응답과 커밋 직전 트랜잭션이 유실된다.
    """
    stopping = False

    def request_stop(name: str) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        print(f"{name} 수신. 봇을 정상 종료합니다.", flush=True)
        sd_notify("STOPPING=1")
        loop.create_task(bot.close())

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, request_stop, name)
        except NotImplementedError:
            # Windows 이벤트 루프는 add_signal_handler를 지원하지 않는다.
            signal.signal(
                sig,
                lambda _sig, _frame, _name=name: loop.call_soon_threadsafe(
                    request_stop, _name
                ),
            )


async def run_bot(token: str) -> None:
    _install_shutdown_handlers(asyncio.get_running_loop())
    async with bot:
        await bot.start(token)


def main() -> None:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN이 없습니다. 로컬은 .env, 서버는 systemd EnvironmentFile을 확인하세요."
        )

    init_db()
    verify_schema()
    print(f"DB: {DB_PATH} (schema v{SCHEMA_VERSION})", flush=True)

    # bot.run()을 우회하므로 discord.py 로깅을 직접 켠다 (journald로 흘러간다).
    discord.utils.setup_logging()
    asyncio.run(run_bot(token))


if __name__ == "__main__":
    main()
