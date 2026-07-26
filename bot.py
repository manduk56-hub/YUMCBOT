from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import random
import secrets
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


def _detect_bot_version() -> str:
    """활동 로그에 남길 코드 버전. '어느 코드로 돈 게임인가'를 사후에 알기 위한 것.

    배포는 릴리스를 /opt/yumcbot/releases/<sha>/ 에 풀고 current 링크를 갈아끼운다.
    따라서 운영에서는 코드 디렉터리 이름이 곧 커밋 SHA다 (deploy/deploy.sh 참고).
    로컬 체크아웃에서는 그렇지 않으므로 'dev'로 둔다. 부팅 시 한 번만 계산한다.
    """
    override = os.getenv("YUMCBOT_VERSION")
    if override:
        return override
    name = BASE_DIR.name
    if len(name) >= 7 and all(char in "0123456789abcdef" for char in name):
        return name[:12]
    return "dev"


BOT_VERSION = _detect_bot_version()
DEFUSE_COLORS = [
    {"id": "red", "name": "빨강", "emoji": "🟥"},
    {"id": "blue", "name": "파랑", "emoji": "🟦"},
    {"id": "green", "name": "초록", "emoji": "🟩"},
]
DEFUSE_COLOR_BY_ID = {color["id"]: color for color in DEFUSE_COLORS}
# 배포 전에 열린 구방식 라운드의 버튼 응답 문구를 위해서만 남긴다.
DEFUSE_COLOR_BY_ID.update(
    {
        "black": {"id": "black", "name": "검정", "emoji": "⬛"},
        "white": {"id": "white", "name": "흰색", "emoji": "⬜"},
    }
)
CUTTING_LEVEL_CHOICES = [str(index) for index in range(0, 7)]
CUTTING_REWARD_POINTS = {
    0: 3,
    1: 3,
    2: 1,
    3: 1,
    4: 1,
    5: 2,
    6: 6,
}
CUTTING_SUCCESS_RATES = {
    0: 0.9,
    1: 0.9,
    2: 0.6,
    3: 0.6,
    4: 0.5,
    5: 0.15,
}
TOP3_NONE_CHOICE = "__none__"
UNIQUE_NUMBER_CHOICES = [str(index) for index in range(1, 16)]
TRUST_REGISTRATION_DEFAULT_MINUTES = 2
TRUST_DECISION_SECONDS = 180
MATCH_HISTORY_ROUNDS = 3  # 메인게임 1:1 매칭에서 이 라운드 수만큼 직전 짝을 회피 (trust·sand 공유)
TRUST_OUTCOME_LABELS = {
    "mutual": "신뢰",
    "single": "단독 배신",
    "double": "동시 배신",
    "incomplete": "미선택",
}
# 게임 키 집합이다 (rounds.type 값 집합이 아니다). 'bail'은 type='sand'로 저장되므로
# `round_row["type"] in MAIN_GAME_TYPES` 검사도 그대로 통과한다. 게임을 갈라야 할 때는
# round_game_key()를 쓴다.
MAIN_GAME_TYPES = {"trust", "sand", "bail"}
# 'grid'(관리자 시점 타일)는 제거된 미니게임이다. 불변인 rounds.type CHECK 제약의 grid 값은
# extra_json.mode='unique_number'인 새 유일 숫자 게임의 저장 타입으로만 재사용한다.
MINI_GAME_TYPES = {
    "balance",
    "minority_balance",
    "defuse4",
    "cutting",
    "top3",
    "unique_number",
}
GAME_NAMES = {
    "balance": "밸런스 게임",
    # 참가자에게는 1차 집계가 끝날 때까지 일반 밸런스 게임으로 보여야 한다.
    "minority_balance": "밸런스 게임",
    # DB 호환을 위해 내부 키는 defuse4를 유지하지만 현재 규칙은 3선 방식이다.
    "defuse4": "몰리면 터진다!",
    "cutting": "돌 세공 예측",
    "top3": "상위 3명 견제",
    "unique_number": "홍피겔만의 황금금고",
    "trust": "딜레마의 섬",
    "sand": "모래성 게임",
    # DB type은 sand를 재사용하고 extra_json.mode='bail'로 갈라 쓴다.
    "bail": "가라앉는 배",
}
BALANCE_VOTE_CHOICES = [("A 선택", "A"), ("B 선택", "B")]
ANONYMOUS_EMBED_COLORS = (
    # 가까운 번호끼리 색상·명도 차이가 크게 나도록 고대비 순서로 배치한다.
    0xD1382A,
    0x0AD10A,
    0x940CF5,
    0x31C4F5,
    0xD1B00A,
    0xAD3D91,
    0x3DAD75,
    0x3172F5,
    0xAD753D,
    0xF556F5,
    0xF50C6D,
    0x3D6CAD,
    0xF5810C,
    0x74AD23,
    0xAD3D59,
    0xF50CA7,
    0x8D49D1,
    0x0AD1D1,
    0x2AD162,
    0xF50C0C,
)
ANONYMOUS_GUIDE_TEXT = (
    "아래 버튼으로 익명 발언을 남겨주세요. 일반 메시지는 익명 보호를 위해 삭제됩니다.\n"
    "발언 횟수 제한은 없으며, 같은 라운드에서는 계속 같은 색상으로 표시됩니다."
)
SAND_NUMBER_CHOICES = [str(index) for index in range(1, 11)]
SAND_PLAY_SECONDS = 600
SAND_RULES_TEXT = (
    "**1** ─ 각 참여자가 1~10 중 모래성이 무너질 횟수를 정합니다.\n"
    "**2** ─ 두 수의 차이만큼은 안전하게 덜 수 있고, 그 횟수를 초과해서 더는 순간 무너집니다. "
    "(예: 9와 7이면 차이가 2 → 2번까지는 성공, 3번째에 붕괴. 차이가 0이면 첫 덜기에서 붕괴)\n"
    "**3** ─ 참여자는 번갈아가며 `포기하기` 또는 `모래 덜기`를 결정합니다.\n"
    "**4** ─ 숫자를 안 내거나 제한 시간 안에 끝내지 못하면 마감 때 랜덤으로 자동 진행됩니다.\n\n"
    "> 🏳️ 둘 다 포기 선택 시: **서로 -3점**\n"
    "> ⚖️ 한 명이 포기(A), 다른 한 명이 모래 덜기(B) 선택 시:\n"
    "> 　├ 덜기 성공: A는 **-3점**, B는 **+5점**\n"
    "> 　└ 무너짐: B는 **-3점**, A는 **+2점**\n"
    "> 💥 서로 덜기를 하다 무너진 경우: 그 턴의 사람이 **-3점**"
)
SAND_OUTCOME_LABELS = {
    "mutual_quit": "둘 다 포기",
    "quit_success": "포기 후 상대 덜기 성공",
    "quit_collapse": "포기 후 상대 모래성 붕괴",
    "plain_collapse": "일반 모래성 붕괴",
}
# 가라앉는 배: 협상 → 실행 2단계 메인게임. rounds.type CHECK 제약을 못 고쳐서
# type='sand' + extra_json.mode='bail'로 저장한다 (grid→unique_number와 같은 방식).
#
# 협상하는 것은 **구조 보상금의 배분**이고(수락하면 확정), 누가 몇 번 퍼낼지는 아무도 약속하지 않는다.
# 그래서 배신은 "보상은 챙기고 노동은 덜 하기"가 된다.
BAIL_REWARD_POOL = 8  # 배가 뜨면 두 사람이 나눠 받는 구조 보상금
BAIL_REWARD_CHOICES = [str(index) for index in range(BAIL_REWARD_POOL + 1)]  # 0~8
BAIL_FAILED_REWARD_POOL = 4  # 협상 결렬 시 감액된 보상금 (둘이 균등 분배)
BAIL_REQUIRED_SCOOPS = 6  # 배가 뜨는 데 필요한 두 사람 합계 퍼내기 횟수
BAIL_SCOOP_CHOICES = [str(index) for index in range(BAIL_REQUIRED_SCOOPS + 1)]  # 0~6
BAIL_SCOOP_COST = 1  # 퍼내기 1회당 차감
# 침몰 페널티는 보상 몫에 비례한다. 많이 받기로 한 쪽이 더 크게 잃어야
# "협상에서 욕심낸 만큼 침몰 리스크를 진다"가 성립하고, 적게 받은 쪽에 배신 카드가 생긴다.
BAIL_SINK_PENALTY_HIGH = 4  # 보상 몫이 많은 쪽
BAIL_SINK_PENALTY_LOW = 2  # 보상 몫이 적은 쪽
BAIL_SINK_PENALTY_EVEN = 3  # 몫이 같을 때 (결렬 포함)
BAIL_NEGOTIATION_MAX_OFFERS = 3  # 교대 제안 최대 횟수
BAIL_DEFAULT_NEGOTIATION_MINUTES = 5
BAIL_DEFAULT_EXECUTION_MINUTES = 3
BAIL_FALLBACK_SCOOPS = BAIL_REQUIRED_SCOOPS // 2  # 실행 단계 미제출 시의 자동 제출값
BAIL_OUTCOME_LABELS = {
    "survived": "생존",
    "sunk": "침몰",
}
EVENT_STATUS_LABELS = {
    "recruiting": "참가 모집 중",
    "active": "게임 진행 중",
    "closed": "종료",
}
# 참가자가 홀수라 짝이 없는 사람이 생겼을 때, 새 참가자가 긴급 투입되면 전체 마감이 이만큼 밀린다.
EMERGENCY_EXTENSION_SECONDS = 60
AUTO_CLOSE_WARNING_SECONDS = 10
AUTO_CLOSE_COUNTDOWN_SECONDS = (5, 4, 3, 2, 1)
AFK_TICK_SECONDS = 60
AFK_COIN_PER_TICK = 1
SCORE_REVEAL_COST = 30
ROULETTE_CHALLENGE_SECONDS = 300
ROULETTE_BETTING_SECONDS = 180
# 자기 차례에 방아쇠를 안 당기면 자동 패배. 없으면 예치금이 영구 동결되고
# 동시 1결투 제한 때문에 상대까지 묶인다(배포도 check_idle에서 영구 차단된다).
ROULETTE_TURN_SECONDS = 180
ROULETTE_MIN_BET = 1
ROULETTE_MAX_BET = 3
ROULETTE_DAILY_LIMIT = 3
ROULETTE_PAIR_DAILY_LIMIT = 1
# 최소 베팅액도 못 내는 유저(=신용 유저)는 강제로 1점을 걸고, 이기면 점수가 오르지 않는
# 대신 연승 스택이 쌓인다. 스택이 목표치에 닿으면 보상 점수를 받고 스택은 0으로 돌아간다.
ROULETTE_STACK_GOAL = 2
ROULETTE_STACK_REWARD = 2


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
    """코드블록에 넣을 한 줄 한 항목 표. 예: '0→1·1→2 90%\\n2→3·3→4 60%\\n4→5     50%'

    가장 긴 라벨은 공백 하나만 붙는다. 라벨 폭을 len으로 맞춰도 열이 어긋나지 않는 이유는
    항목마다 한글 글자 수가 같아서다(강화/없음). 그 전제가 깨지면 표시 폭으로 계산해야 한다.
    """
    rows = []
    for levels, rate in _group_consecutive(CUTTING_SUCCESS_RATES):
        steps = "·".join(f"{level}→{level + 1}" for level in levels)
        rows.append((steps, f"{rate * 100:.0f}%"))
    width = max(len(steps) for steps, _ in rows)
    return "\n".join(f"{steps:<{width}} {value}" for steps, value in rows)


def cutting_reward_text() -> str:
    """코드블록에 넣을 한 줄 한 항목 표. 예: '0~1강화 +3점\\n2~4강화 +1점\\n5강화   +2점'"""
    rows = []
    for levels, points in _group_consecutive(CUTTING_REWARD_POINTS):
        span = f"{levels[0]}강화" if len(levels) == 1 else f"{levels[0]}~{levels[-1]}강화"
        rows.append((span, f"+{points}점"))
    width = max(len(span) for span, _ in rows)
    return "\n".join(f"{span:<{width}} {value}" for span, value in rows)


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
    # 유저 간 상시 1:1 러시안룰렛. 진행 상태와 예치금을 DB에 보존해 재시작 뒤에도 이어진다.
    4: [
        """
        CREATE TABLE IF NOT EXISTS russian_roulette_games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            message_id TEXT,
            challenger_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            challenger_name TEXT NOT NULL,
            target_name TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'betting', 'playing', 'finished', 'declined', 'expired')
            ),
            challenger_bet INTEGER,
            target_bet INTEGER,
            current_player_id TEXT,
            live_chamber INTEGER CHECK (live_chamber BETWEEN 0 AND 5),
            shots_fired INTEGER NOT NULL DEFAULT 0 CHECK (shots_fired BETWEEN 0 AND 6),
            winner_id TEXT,
            loser_id TEXT,
            created_at TEXT NOT NULL,
            expires_at REAL,
            accepted_at TEXT,
            finished_at TEXT,
            FOREIGN KEY (challenger_id) REFERENCES users(user_id) ON DELETE CASCADE,
            FOREIGN KEY (target_id) REFERENCES users(user_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_roulette_active_users
        ON russian_roulette_games(status, challenger_id, target_id)
        """,
    ],
    # 활동 로그. 사후 분석(SQL/에이전트)용 append-only 이벤트 스트림.
    #
    # 기존 테이블 대부분은 '현재 상태'라 덮어쓰기·삭제가 일어난다
    # (votes는 choice를 upsert, event_participants는 취소 시 DELETE,
    #  russian_roulette_games는 shots_fired 카운터만 증가, extra_json은 통째로 교체).
    # 그래서 '무슨 일이 있었는지'는 그 테이블들만으로 복원할 수 없다.
    # 이 테이블은 일어난 순간을 그대로 적재해 그 구멍을 메운다. UPDATE/DELETE 하지 않는다.
    #
    # FK를 걸지 않는 이유: 로그는 참조 대상이 지워져도 남아야 하고(score_logs가
    # ON DELETE SET NULL을 쓰는 것과 같은 이유), append 비용도 줄인다.
    5: [
        """
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            category TEXT NOT NULL,
            action TEXT NOT NULL,
            event_session_id INTEGER,
            round_id INTEGER,
            actor_user_id TEXT,
            target_user_id TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}'
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_activity_event
        ON activity_logs(event_session_id, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_activity_round
        ON activity_logs(round_id, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_activity_actor
        ON activity_logs(actor_user_id, created_at)
        """,
    ],
    # 러시안룰렛 신용 유저(최소 베팅액도 못 내는 유저) 지원.
    #
    # roulette_win_stack: 신용 유저가 이겨도 점수는 안 오르므로 대신 쌓이는 연승 카운터.
    # *_negative: '신용 유저였는지'는 예치 시점의 점수로 정해지는데, 예치 자체가 점수를
    #   깎아서 정산 시점에 다시 계산할 수가 없다. 그래서 게임 시작 때 박아둔다.
    6: [
        "ALTER TABLE users ADD COLUMN roulette_win_stack INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE russian_roulette_games "
        "ADD COLUMN challenger_negative INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE russian_roulette_games "
        "ADD COLUMN target_negative INTEGER NOT NULL DEFAULT 0",
        # 실탄 격발로 끝났는지 시간 초과로 끝났는지. 둘 다 shots_fired만으로는 구분되지 않는다.
        "ALTER TABLE russian_roulette_games ADD COLUMN finish_cause TEXT",
        # 이 판 직후의 승자 스택. 보상을 받으면 스택이 0으로 돌아가버려서, 나중에
        # '이 판으로 몇 연승이 됐는지'를 users 테이블만 봐서는 알 수 없다.
        "ALTER TABLE russian_roulette_games ADD COLUMN winner_stack_after INTEGER",
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


def build_anonymous_message_embed(anon_number: int, content: str) -> discord.Embed:
    """작성자 이름 없이 내부 익명 번호마다 고정 색상만 붙이는 토론 발언 카드."""
    color = ANONYMOUS_EMBED_COLORS[(anon_number - 1) % len(ANONYMOUS_EMBED_COLORS)]
    return discord.Embed(
        description=content,
        color=color,
    )


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
    display_name: str | None,
    delta: int,
    reason: str,
    round_id: int | None = None,
) -> None:
    ts = now_text()
    # display_name이 None(멤버 캐시 미스)이면 신규 행은 플레이스홀더로,
    # 기존 행은 이름을 보존한다. excluded는 VALUES에서 파생돼 이 분기를 못 하므로
    # 원본 :name 파라미터를 UPDATE에서 직접 참조한다 (issue #19).
    conn.execute(
        """
        INSERT INTO users (user_id, display_name, total_score, created_at, updated_at)
        VALUES (:uid, COALESCE(:name, 'User ' || :uid), 0, :ts, :ts)
        ON CONFLICT(user_id) DO UPDATE SET
            display_name = COALESCE(:name, users.display_name),
            updated_at = :ts
        """,
        {"uid": user_id, "name": display_name, "ts": ts},
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
    reset_roulette_stack_if_escaped(conn, user_id)


def reset_roulette_stack_if_escaped(conn: sqlite3.Connection, user_id: str) -> None:
    """신용 상태를 벗어나면 러시안룰렛 연승 스택을 버린다.

    스택은 '신용 상태가 끊기지 않고 이어졌다'는 뜻이라, 중간에 한 번이라도 점수가
    올라오면 무효다. 그런데 탈출은 룰렛이 아니라 메인게임 정산으로도 일어나므로
    현재 점수만 보고는 '중간에 올라왔다 내려왔는지'를 알 수 없다. 그래서 점수가
    바뀌는 길목인 add_score에서 매번 확인한다.
    """
    row = conn.execute(
        "SELECT total_score, roulette_win_stack FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        return
    stack = int(row["roulette_win_stack"])
    if stack <= 0 or int(row["total_score"]) < ROULETTE_MIN_BET:
        return
    set_roulette_stack(conn, user_id, 0, "escaped", before=stack)


# TODO(activity-log): 운영자용 조회/내보내기 수단이 아직 없다.
# 당분간은 서버에서 직접 조회한다:
#   sqlite3 /var/lib/yumcbot/data/event_game.sqlite3 \
#     "SELECT created_at, category, action, actor_user_id, payload_json
#      FROM activity_logs WHERE event_session_id = 12 ORDER BY created_at"
# 필요해지면 /로그 슬래시 명령이나 CSV 내보내기를 붙인다.
def user_display_name(user: discord.abc.User) -> str | None:
    """로그에 남길 표시 이름. 로깅 때문에 게임이 죽으면 안 되므로 속성이 없어도 견딘다."""
    return getattr(user, "display_name", None) or getattr(user, "name", None)


def log_activity(
    conn: sqlite3.Connection,
    category: str,
    action: str,
    *,
    event_session_id: int | None = None,
    round_id: int | None = None,
    actor_user_id: str | None = None,
    target_user_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """활동 로그 한 줄. 사후 분석용 append-only 기록이라 절대 UPDATE/DELETE 하지 않는다.

    호출자가 이미 쥐고 있는 conn에 얹어서 같은 트랜잭션으로 커밋한다. 별도 커넥션을
    열지 않으므로 추가 fsync가 없고, 본 작업이 롤백되면 로그도 같이 롤백돼 어긋나지 않는다.

    로깅 실패가 게임 진행을 막으면 안 되므로 여기서 예외를 삼킨다. 다만 조용히 사라지면
    누락을 눈치챌 수 없으니 stderr에는 남긴다.

    category: event|round|match|participation|score|roulette|vote|thread
    payload: JSON 직렬화 가능한 세부. 게임마다 다른 값은 전부 여기에 넣어
             새 게임이 추가돼도 스키마 마이그레이션이 필요 없게 한다.
    """
    try:
        conn.execute(
            """
            INSERT INTO activity_logs
                (created_at, category, action, event_session_id, round_id,
                 actor_user_id, target_user_id, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_text(),
                category,
                action,
                event_session_id,
                round_id,
                actor_user_id,
                target_user_id,
                json.dumps(payload or {}, ensure_ascii=False, default=str),
            ),
        )
    except sqlite3.Error as error:
        print(f"활동 로그 기록 실패({category}:{action}): {error}", flush=True)


def game_settings_snapshot(game_key: str) -> dict[str, Any]:
    """라운드가 시작될 때의 규칙값. 규칙을 조정해도 과거 라운드를 당시 기준으로 해석하기 위한 것.

    점수 규칙 일부(밸런스 몰표 -2점, 소수파 +2점 등)는 상수가 아니라
    close_round_record 분기에 하드코딩돼 있어 여기서 값으로 뜰 수 없다.
    그래서 bot_version(배포 커밋 SHA)을 항상 함께 남긴다 — 그 커밋을 열면
    당시 정산 규칙을 그대로 확인할 수 있다. 튜닝 가능한 상수는 값으로도 남긴다.
    """
    settings: dict[str, Any] = {"bot_version": BOT_VERSION}

    if game_key == "cutting":
        settings["reward_points"] = CUTTING_REWARD_POINTS
        settings["success_rates"] = CUTTING_SUCCESS_RATES
    elif game_key == "trust":
        settings["decision_seconds"] = TRUST_DECISION_SECONDS
        settings["match_history_rounds"] = MATCH_HISTORY_ROUNDS
    elif game_key == "sand":
        settings["play_seconds"] = SAND_PLAY_SECONDS
        settings["number_choices"] = SAND_NUMBER_CHOICES
        settings["match_history_rounds"] = MATCH_HISTORY_ROUNDS
    elif game_key == "bail":
        settings["reward_pool"] = BAIL_REWARD_POOL
        settings["failed_reward_pool"] = BAIL_FAILED_REWARD_POOL
        settings["required_scoops"] = BAIL_REQUIRED_SCOOPS
        settings["scoop_cost"] = BAIL_SCOOP_COST
        settings["sink_penalty_high"] = BAIL_SINK_PENALTY_HIGH
        settings["sink_penalty_low"] = BAIL_SINK_PENALTY_LOW
        settings["sink_penalty_even"] = BAIL_SINK_PENALTY_EVEN
        settings["max_offers"] = BAIL_NEGOTIATION_MAX_OFFERS
        settings["fallback_scoops"] = BAIL_FALLBACK_SCOOPS
        settings["match_history_rounds"] = MATCH_HISTORY_ROUNDS
    elif game_key == "unique_number":
        settings["number_choices"] = UNIQUE_NUMBER_CHOICES
    elif game_key == "defuse4":
        settings["colors"] = [color["id"] for color in DEFUSE_COLORS]

    return settings


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


def event_rounds_are_complete(round_rows: list[sqlite3.Row]) -> bool:
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
    conn: sqlite3.Connection,
    event_row: sqlite3.Row | dict[str, Any],
    round_rows: list[sqlite3.Row],
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
    channel = await resolve_text_channel(channel_id)
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


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


def mention_list(user_ids: list[str], limit: int = 40, sep: str = ", ") -> str:
    if not user_ids:
        return "없음"
    mentions = [f"<@{user_id}>" for user_id in user_ids[:limit]]
    suffix = f"\n외 {len(user_ids) - limit}명" if len(user_ids) > limit else ""
    return sep.join(mentions) + suffix


def add_field_divider(embed: discord.Embed) -> None:
    """임베드 필드 사이에 가로 구분선을 넣어 섹션 경계를 분명히 한다.

    임베드는 필드 사이에 굵은 가로줄을 못 넣으므로, 이름이 빈(폭 0 공백) 전체폭 필드에
    선 문자를 채워 대신한다.
    """
    embed.add_field(name="​", value="━" * 15, inline=False)


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


def format_public_ranking_lines(rows: list[sqlite3.Row], start_rank: int = 1) -> list[str]:
    """공개 랭킹에 표시할 순위, 멘션, 점수를 만든다."""
    return [
        f"{index}. <@{row['user_id']}> - **{row['total_score']}점**"
        for index, row in enumerate(rows, start=start_rank)
    ]


def format_anonymous_ranking_lines(rows: list[sqlite3.Row], start_rank: int = 1) -> list[str]:
    """사용자 정보 없이 공개할 순위와 점수만 만든다."""
    return [
        f"{index}등 - **{row['total_score']}점**"
        for index, row in enumerate(rows, start=start_rank)
    ]


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


def member_name(guild: discord.Guild | None, user_id: str) -> str | None:
    """멤버 캐시에서 표시 이름을 읽는다. 미스면 None.

    반환값은 add_score()의 display_name 인자로만 흘러가며, None이면 upsert가
    기존 이름을 보존한다. 예전엔 미스 시 'User <id>' 플레이스홀더를 반환해 그게
    DB에 그대로 덮여쓰였다 (issue #19). 결과는 embed 멘션으로 표시되므로 표시용
    플레이스홀더는 따로 필요 없다."""
    if guild:
        member = guild.get_member(int(user_id))
        if member:
            return member.display_name
    return None


def choice_display(choice: str, game_key: str | None = None) -> str:
    if game_key == "unique_number" and choice in UNIQUE_NUMBER_CHOICES:
        return f"{choice}번"
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
    """코드상 게임 이름. 3선 폭탄 게임은 레거시 키 defuse4, DB type bomb을 유지한다."""
    return str(round_extra(round_row).get("mode") or round_row["type"])


def main_game_phase(round_row: sqlite3.Row | dict[str, Any]) -> str | None:
    """메인게임의 진행 단계. 게임마다 extra_json 키가 달라서 한 곳에 모아둔다.

    세 게임 모두 마지막 단계 이름은 'scored', 취소는 'cancelled'로 맞춘다.
    """
    extra = round_extra(round_row)  # type: ignore[arg-type]
    game_key = round_game_key(round_row)  # type: ignore[arg-type]
    if game_key == "trust":
        return extra.get("trust_phase")
    if game_key == "bail":
        return extra.get("bail_phase")
    return extra.get("sand_phase")


def thread_discussion_view(round_id: int, game_key: str) -> discord.ui.View:
    """토론방 맨 아래에 붙는 버튼 묶음: 선택지 + 익명 발언하기.

    토론 스레드는 라운드 메시지에서 파생시키므로 그 메시지가 스레드의 시작 메시지가 되는데,
    시작 메시지의 버튼은 스레드 안에서 누를 수 없다(실체가 부모 채널에 있는 메시지라 회색으로 뜬다).
    그래서 같은 custom_id의 선택 버튼을 스레드 안에도 둔다. votes는 (round_id, user_id) upsert라
    채널에서 누르든 스레드에서 누르든 마지막 선택 하나만 남는다.
    """
    view = discord.ui.View(timeout=None)
    if game_key in {"balance", "minority_balance"}:
        vote_buttons = vote_view(round_id, BALANCE_VOTE_CHOICES)
    elif game_key == "unique_number":
        vote_buttons = unique_number_vote_view(round_id)
    else:
        vote_buttons = defuse_vote_view(round_id)
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


def build_anonymous_guide_embed(
    post_count: int,
    close_at: float | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title="🕶️ 익명 토론방",
        description=ANONYMOUS_GUIDE_TEXT,
        color=discord.Color.dark_grey(),
    )
    embed.add_field(
        name="🗳️ 투표",
        value="아래 선택 버튼으로 이 방에서 바로 투표할 수 있습니다. 마감 전까지 몇 번이든 바꿀 수 있습니다.",
        inline=False,
    )
    embed.add_field(name="💬 누적 발언", value=f"**{post_count}개**", inline=False)
    if close_at:
        embed.add_field(
            name="⏳ 마감 시각",
            value=f"**{close_time_text(close_at)}**",
            inline=False,
        )
    return embed


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
        phase = main_game_phase(main_round)
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
        log_activity(
            conn,
            "participation",
            "join",
            event_session_id=event_session_id,
            actor_user_id=str(interaction.user.id),
            payload={
                "display_name": user_display_name(interaction.user),
                "participant_count": participant_count,
            },
        )

    await interaction.response.edit_message(
        view=event_join_view(event_session_id, participant_count),
    )
    await interaction.followup.send(
        "참여 등록 완료! 이번 이벤트의 메인게임과 미니게임에 참여할 수 있습니다.",
        ephemeral=True,
    )


async def handle_event_cancel(interaction: discord.Interaction, event_session_id: int) -> None:
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
                "이 이벤트의 참가 등록은 이미 마감되어 참여를 취소할 수 없습니다.",
                ephemeral=True,
            )
            return

        cursor = conn.execute(
            """
            DELETE FROM event_participants
            WHERE event_session_id = ? AND user_id = ?
            """,
            (event_session_id, str(interaction.user.id)),
        )
        if cursor.rowcount == 0:
            await interaction.response.send_message(
                "이번 이벤트에 참여 등록되어 있지 않습니다.",
                ephemeral=True,
            )
            return

        participant_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS count FROM event_participants
                WHERE event_session_id = ?
                """,
                (event_session_id,),
            ).fetchone()["count"]
        )
        # DELETE로 행이 사라지므로 취소했다는 사실은 여기서만 남는다.
        log_activity(
            conn,
            "participation",
            "cancel",
            event_session_id=event_session_id,
            actor_user_id=str(interaction.user.id),
            payload={
                "display_name": user_display_name(interaction.user),
                "participant_count": participant_count,
            },
        )

    await interaction.response.edit_message(
        view=event_join_view(event_session_id, participant_count),
    )
    await interaction.followup.send("참여가 취소되었습니다.", ephemeral=True)


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
            game_key = round_game_key(round_row)

        try:
            posted_message = await interaction.channel.send(
                embed=build_anonymous_message_embed(anon_number, content),
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
            # 내용은 discussion_messages(append-only)에 이미 온전히 있다. 여기서는 중복 저장하지
            # 않고 참조만 남겨, 하나의 타임라인에서 발언 시점을 다른 활동과 함께 볼 수 있게 한다.
            log_activity(
                conn,
                "thread",
                "anon_post",
                event_session_id=round_row["event_session_id"],
                round_id=self.round_id,
                actor_user_id=user_id,
                payload={
                    "discussion_message_id": post_id,
                    "anon_number": anon_number,
                    "content_length": len(content),
                    "thread_id": str(interaction.channel.id),
                },
            )

        # 버튼이 발언에 밀려 위로 올라가지 않도록, 옛 버튼을 지우고 맨 아래에 다시 붙인다.
        # 여러 명이 동시에 발언하면 각자 옛 스냅샷에서 버튼 id를 읽어 버튼판이 여러 개로 갈린다.
        # 그래서 스레드별 락을 잡고, 락 안에서 현재 버튼 id를 DB에서 다시 읽는다. (모래성과 같은 방식)
        async with bot.anon_board_lock(interaction.channel.id):
            with db() as conn:
                fresh_row = conn.execute(
                    "SELECT extra_json FROM rounds WHERE id = ?", (self.round_id,)
                ).fetchone()
                fresh_extra = round_extra(fresh_row) if fresh_row else {}
                current_button_id = str(fresh_extra.get("anon_button_message_id") or "")
                current_auto_close_at = fresh_extra.get("auto_close_at")
                post_count = count_discussion_messages(conn, self.round_id)

            if current_button_id:
                try:
                    old_button = await interaction.channel.fetch_message(int(current_button_id))
                    await old_button.delete()
                except (discord.NotFound, discord.HTTPException, ValueError):
                    pass
            try:
                new_button = await interaction.channel.send(
                    embed=build_anonymous_guide_embed(post_count, current_auto_close_at),
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


def recent_match_pairs(
    conn: sqlite3.Connection,
    round_id: int,
    round_type: str,
    history_rounds: int = MATCH_HISTORY_ROUNDS,
) -> set[tuple[str, str]]:
    """직전 `history_rounds`개 같은 타입 라운드에서 성사된 1:1 짝을 정규화해 모은다.

    trust·sand 모두 `trust_pairings`에 짝을 남기므로 `round_type`으로 갈라 조회한다.
    """
    history_rows = conn.execute(
        """
        SELECT id FROM rounds
        WHERE type = ? AND id < ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (round_type, round_id, history_rounds),
    ).fetchall()
    history_ids = [int(row["id"]) for row in history_rows]
    if not history_ids:
        return set()
    placeholders = ", ".join("?" for _ in history_ids)
    history_pairs = conn.execute(
        f"SELECT user_a_id, user_b_id FROM trust_pairings WHERE round_id IN ({placeholders})",
        tuple(history_ids),
    ).fetchall()
    return {
        tuple(sorted((str(row["user_a_id"]), str(row["user_b_id"]))))
        for row in history_pairs
    }


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

        recent_pairs = recent_match_pairs(conn, round_id, "trust")
        pairs, unmatched_user_id = build_random_trust_pairs(user_ids, recent_pairs)
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
                payload={"game_key": "trust"},
            )
        log_activity(
            conn,
            "match",
            "pairing_complete",
            event_session_id=round_row["event_session_id"],
            round_id=round_id,
            payload={
                "game_key": "trust",
                "pair_count": len(pairs),
                "registered_count": len(user_ids),
                "unmatched_user_id": unmatched_user_id,
                "avoided_recent_pairs": len(recent_pairs),
            },
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

        embed = build_trust_decision_embed(
            round_id,
            str(round_row["title"]),
            pairs,
            unmatched_user_id,
            close_at=decision_close_at,
        )
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


async def archive_round_discussion_thread(
    round_row: sqlite3.Row,
    *,
    reason: str,
) -> None:
    """일반 토론 스레드를 삭제하지 않고 읽기 전용 보관 상태로 남긴다."""
    thread_id = str(round_row["discussion_thread_id"] or "")
    if not thread_id:
        return
    try:
        thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
        if isinstance(thread, discord.Thread):
            await thread.edit(archived=True, locked=True, reason=reason)
    except (discord.HTTPException, ValueError):
        pass


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


def trust_pair_intro(
    user_a_id: str,
    user_b_id: str,
    close_at: float | None = None,
) -> str:
    intro = (
        f"<@{user_a_id}>님과 <@{user_b_id}>님의 **비공개 협상방**입니다.\n"
        "> 서로 마음껏 대화한 뒤 아래 버튼으로 `신뢰` 또는 `배신`을 선택하세요. (마감 전까지 몇 번이든 바꿀 수 있습니다)\n"
        "> 선택은 마감 전까지 상대에게 공개되지 않으며, 마감 후 이 방에서 함께 공개됩니다.\n"
        "> \\*마감까지 고르지 않으면 `신뢰`/`배신` 중 하나가 랜덤으로 배정됩니다."
    )
    if close_at:
        intro += f"\n\n⏳ **선택 마감:** {close_time_text(close_at)}"
    return intro


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
            content=trust_pair_intro(user_a_id, user_b_id, transition.get("close_at")),
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
        outcome = "상호 신뢰 · 두 사람 모두 +2점"
    elif user_a_choice == "betray" and user_b_choice == "betray":
        outcome = "동시 배신 · 두 사람 모두 -3점"
    else:
        betrayer_id = user_a_id if user_a_choice == "betray" else user_b_id
        outcome = f"단독 배신 · <@{betrayer_id}> +5점, 상대 0점"

    embed = discord.Embed(
        title=f"세트 결과 · {title}",
        description="두 사람의 선택이 공개되었습니다.",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="🃏 서로의 선택",
        value=(
            f"<@{user_a_id}>: **{choice_labels.get(user_a_choice, '미선택')}**\n"
            f"<@{user_b_id}>: **{choice_labels.get(user_b_choice, '미선택')}**"
        ),
        inline=False,
    )
    embed.add_field(name="💰 세트 정산", value=outcome, inline=False)
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
        # votes는 choice를 덮어쓰므로 바꾸기 전 값을 여기서 잡아둬야 이력이 남는다.
        previous = conn.execute(
            "SELECT choice FROM votes WHERE round_id = ? AND user_id = ?",
            (round_id, str(interaction.user.id)),
        ).fetchone()
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
        log_activity(
            conn,
            "vote",
            "trust_decision",
            round_id=round_id,
            actor_user_id=str(interaction.user.id),
            payload={
                "choice": choice,
                "previous_choice": previous["choice"] if previous else None,
                "changed": bool(previous and previous["choice"] != choice),
                "thread_id": str(interaction.channel_id),
            },
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

        recent_pairs = recent_match_pairs(conn, round_id, "sand")
        pairs, unmatched_user_id = build_random_trust_pairs(user_ids, recent_pairs)
        sand_pairs: list[dict[str, Any]] = []
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
                payload={"game_key": "sand"},
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
        log_activity(
            conn,
            "match",
            "pairing_complete",
            event_session_id=round_row["event_session_id"],
            round_id=round_id,
            payload={
                "game_key": "sand",
                "pair_count": len(pairs),
                "registered_count": len(user_ids),
                "unmatched_user_id": unmatched_user_id,
            },
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
        f"<@{user_a_id}>님과 <@{user_b_id}>님의 **1:1 모래성 방**입니다.\n"
        "> **1** ─ 먼저 각자 1~10 중 하나를 고르세요. (번복 불가, 마감까지 상대에게 공개되지 않습니다)\n"
        "> **2** ─ 두 사람이 모두 고르면 번갈아가며 `포기하기` 또는 `모래 덜기`를 선택합니다."
    )


def sand_pair_settlement_text(pair: dict[str, Any]) -> str:
    """짝 하나의 정산 문구. 라운드 결과 요약과 방별 결과 공개가 같은 문구를 쓰도록 한 곳에 모은다."""
    outcome = str(pair.get("outcome") or "")
    result = pair.get("result", {})
    if outcome == "mutual_quit":
        return "각 -3점"
    if outcome == "quit_success":
        return f"<@{result['winner']}> +5점 · <@{result['quitter']}> -3점"
    if outcome == "quit_collapse":
        return f"<@{result['winner']}> +2점 · <@{result['collapsed_user']}> -3점"
    if outcome == "plain_collapse":
        return f"<@{result['collapsed_user']}> -3점"
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
    game_key: str,
    new_user_id: str,
) -> dict[str, Any] | None:
    """긴급 투입을 extra에 반영한다. 짝 없는 대기자가 없으면 None.

    `game_key`는 rounds.type이 아니라 round_game_key() 값이다 — 가라앉는 배는
    type='sand'로 저장되므로 타입으로는 모래성과 구분되지 않는다.

    DB도 디스코드도 건드리지 않는 순수 함수라 테스트에서 그대로 쓴다.
    호출 전에 라운드 상태·중복 참여 검증이 끝나 있어야 한다.
    """
    unmatched_user_id = str(extra.get("unmatched_user_id") or "")
    if not unmatched_user_id or unmatched_user_id == new_user_id:
        return None

    if game_key == "bail":
        # 가라앉는 배는 짝 구조가 다르다. 늦게 붙은 짝도 협상부터 시작하되,
        # 이미 실행 단계로 넘어간 뒤라면 협상 기회가 없으므로 합의 없이 제출만 한다.
        pair = bail_new_pair(unmatched_user_id, new_user_id)
        if extra.get("bail_phase") == "execution":
            pair.update({"phase": "submitting", "offer": None})
        extra.setdefault("bail_pairs", []).append(pair)
    else:
        pair = {
            "user_a_id": unmatched_user_id,
            "user_b_id": new_user_id,
            "number_a": None,
            "number_b": None,
            "phase": "number_selection",
            "current_user_id": random.choice([unmatched_user_id, new_user_id]),
            "successful_removals": 0,
        }
        if game_key == "sand":
            extra.setdefault("sand_pairs", []).append(pair)

    extra["unmatched_user_id"] = None
    extra["emergency_user_id"] = new_user_id

    # 늦게 합류한 짝도 게임을 끝낼 시간이 필요하다. 전체 마감을 1분 미룬다.
    close_at = float(extra.get("auto_close_at") or time.time()) + EMERGENCY_EXTENSION_SECONDS
    extra["auto_close_at"] = close_at
    if game_key == "trust":
        extra["decision_close_at"] = close_at
    elif game_key == "bail":
        phase_key = (
            "execution_close_at"
            if extra.get("bail_phase") == "execution"
            else "negotiation_close_at"
        )
        extra[phase_key] = close_at
    else:
        extra["play_close_at"] = close_at

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
                extra.get("decision_close_at"),
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
        game_key = round_game_key(round_row)
        extra = round_extra(round_row)
        phase = main_game_phase(round_row)
        # 가라앉는 배는 협상·실행 두 단계 모두에서 투입을 받는다.
        if phase not in {"decision", "playing", "negotiation", "execution"}:
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
        applied = apply_emergency_join(extra, game_key, user_id)
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
        # 긴급 투입은 event_participants에 INSERT OR IGNORE로 끼어들어가 joined_at만 남는다.
        # '정상 참여'와 구분되지 않으므로 투입 경위를 여기서 남긴다.
        log_activity(
            conn,
            "match",
            "emergency_join",
            event_session_id=round_row["event_session_id"],
            round_id=round_id,
            actor_user_id=user_id,
            target_user_id=(
                str(applied["user_b_id"])
                if str(applied["user_a_id"]) == user_id
                else str(applied["user_a_id"])
            ),
            payload={
                "game_key": game_key,
                "extension_seconds": EMERGENCY_EXTENSION_SECONDS,
            },
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
            content=trust_pair_intro(user_a_id, user_b_id, close_at),
        )
    elif game_key == "bail":
        created = await create_pair_thread(
            channel,
            f"{title} · 긴급 투입 구조선",
            [user_a_id, user_b_id],
            f"가라앉는 배 #{round_id} 긴급 투입 방",
            bail_pair_view(round_id, applied["pair"], extra),
            content=bail_pair_intro(user_a_id, user_b_id),
            embed=build_bail_pair_embed(title, applied["pair"], extra),
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
        if game_key == "bail":
            store_bail_thread_ids(
                round_id, {(user_a_id, user_b_id): (str(thread.id), str(message.id))}
            )
        elif round_type == "sand":
            store_sand_thread_ids(
                round_id, {(user_a_id, user_b_id): (str(thread.id), str(message.id))}
            )
        await interaction.followup.send(
            f"긴급 투입 완료! <@{user_a_id}>님과의 방이 열렸습니다.",
            ephemeral=True,
        )

    if game_key == "bail":
        await refresh_main_bail_board(round_id)
    elif round_type == "sand":
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
            return "상대의 포기에 함께 포기했습니다. 두 사람 모두 -3점입니다."
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
            return "모래를 덜다가 무너졌습니다. 나는 -3점, 먼저 포기한 상대는 +2점입니다."
        pair.update(
            {
                "phase": "resolved",
                "outcome": "plain_collapse",
                "result": {"collapsed_user": user_id},
                "collapse_attempt": attempt,
                "raw_difference": raw_difference,
            }
        )
        return "-3점입니다."

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
        return "마지막 모래 덜기에 성공했습니다. 나는 +5점, 먼저 포기한 상대는 -3점입니다."

    pair.update(
        {
            "successful_removals": attempt,
            "current_user_id": other_user_id,
        }
    )
    return "모래 덜기 성공!"


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
    private_message: str | None = None,
    public_message: str | None = None,
) -> None:
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row:
            await interaction.response.send_message("존재하지 않는 라운드입니다.", ephemeral=True)
            return
        channel_id = str(round_row["channel_id"])
        complete = sand_games_complete(round_extra(round_row))

    # 공개 메시지는 이 1:1 방의 두 사람 모두에게 보이게, 개인 안내(점수 등)는 본인만 보게 보낸다.
    if public_message:
        await interaction.response.send_message(public_message)
        if private_message:
            await interaction.followup.send(private_message, ephemeral=True)
    elif private_message:
        await interaction.response.send_message(private_message, ephemeral=True)
    else:
        await interaction.response.defer()

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
    await publish_event_summary_if_complete(round_row["event_session_id"])


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

    suffix = (
        " 두 사람의 선택이 끝났습니다. 차례는 아래 박스를 확인해주세요!"
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

    outcome = str(pair.get("outcome") or "")
    public_message = None
    if outcome == "plain_collapse":
        # 붕괴 안내는 방 모두에게, 점수(-1점)는 본인만.
        public_message = "모래를 덜다가 무너졌습니다."
    elif outcome == "quit_collapse":
        # 문장 전체를 방 모두에게 공개.
        public_message = private_message
        private_message = None
    await refresh_or_finish_sand_round(interaction, round_id, private_message, public_message)


# ---------------------------------------------------------------------------
# 가라앉는 배 (bail) — 협상 → 실행 2단계 메인게임
#
# 모래성과 같은 1:1 비공개 방 구조를 쓰지만 단계가 하나 더 있다.
#   registration → negotiation(교대 제안) → execution(동시 비공개 제출) → scored
# 협상에서 맺은 합의는 기록만 되고 점수에 영향이 없다. 그게 이 게임의 핵심 규칙이라
# 정산은 오직 execution 단계의 제출값만 본다.
# ---------------------------------------------------------------------------


def bail_rules_text() -> str:
    """규칙 안내문. 점수 상수를 조정하면 안내문도 따라 바뀌도록 런타임에 만든다."""
    return (
        f"**1** ─ 구조되면 보상금 **{BAIL_REWARD_POOL}점**이 나옵니다. 먼저 비공개 방에서 "
        "**그 돈을 어떻게 나눌지** 협상합니다. "
        f"(제안과 수락/거절을 번갈아 최대 {BAIL_NEGOTIATION_MAX_OFFERS}번, 수락하면 확정)\n"
        f"**2** ─ 배가 뜨려면 **두 사람이 합쳐 {BAIL_REQUIRED_SCOOPS}번** 물을 퍼내야 합니다. "
        "**누가 몇 번 퍼낼지는 아무도 약속하지 않습니다** — 각자 비공개로 제출합니다.\n"
        f"**3** ─ 협상이 결렬되면 보상금이 **{BAIL_FAILED_REWARD_POOL}점**으로 줄어 "
        f"각자 {BAIL_FAILED_REWARD_POOL // 2}점씩 받습니다.\n"
        f"**4** ─ 제출하지 않으면 마감 때 {BAIL_FALLBACK_SCOOPS}번으로 자동 제출됩니다.\n\n"
        f"> 🪣 퍼내기 1회당 **-{BAIL_SCOOP_COST}점**\n"
        f"> ⛵ 합이 **{BAIL_REQUIRED_SCOOPS}번 이상**이면 배가 뜬다 → "
        "**보상 몫 − 내가 퍼낸 횟수**\n"
        f"> 🌊 못 미치면 침몰 — 보상금을 못 받고 **몫이 많은 쪽 -{BAIL_SINK_PENALTY_HIGH}점 / "
        f"적은 쪽 -{BAIL_SINK_PENALTY_LOW}점** (같으면 각 -{BAIL_SINK_PENALTY_EVEN}점)"
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


def bail_negotiations_complete(extra: dict[str, Any]) -> bool:
    pairs = extra.get("bail_pairs", [])
    return bool(pairs) and all(
        str(pair.get("phase")) in {"submitting", "resolved"} for pair in pairs
    )


def bail_games_complete(extra: dict[str, Any]) -> bool:
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


def autocomplete_bail_pair(pair: dict[str, Any]) -> None:
    """마감 시점에 안 끝난 짝을 자동으로 확정한다.

    노동은 약속 대상이 아니라 기준으로 삼을 합의가 없다. 절반(BAIL_FALLBACK_SCOOPS)으로 채운다 —
    0으로 박으면 잠수 한 명이 상대까지 침몰시키고, 랜덤은 침몰 확률이 지나치게 높다.
    협상 중이었다면 결렬로 처리되어 감액된 보상금을 균등 분배받는다.
    """
    if str(pair.get("phase")) == "resolved":
        return

    pair["offer"] = None
    for key in ("scoop_a", "scoop_b"):
        if pair.get(key) is None:
            pair[key] = BAIL_FALLBACK_SCOOPS
    pair["auto_completed"] = True
    resolve_bail_pair(pair)


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
    """방 하나의 진행판을 지우고 맨 아래에 다시 붙인다.

    ephemeral 응답이 쌓여 버튼이 위로 밀리므로 모래성과 같은 방식으로 재부착한다.
    두 사람이 동시에 누르면 옛 진행판을 가리킬 수 있어, 락을 잡고 DB에서 id를 다시 읽는다.
    (락 dict는 모래성과 공유한다 — 키가 thread_id라 서로 충돌하지 않는다.)
    """
    async with bot.sand_board_lock(thread.id):
        with db() as conn:
            round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row:
            return
        extra = round_extra(round_row)
        pair = find_bail_pair(extra, pair_user_id)
        if not pair:
            return

        resolved = str(pair.get("phase")) == "resolved"
        embed = build_bail_pair_embed(str(round_row["title"]), pair, extra)
        view = bail_pair_view(round_id, pair, extra)

        board_message_id = str(pair.get("play_message_id") or "")
        if resolved:
            # 더 누를 버튼이 없으므로 제자리에서 비활성화만 한다.
            if board_message_id:
                try:
                    message = await thread.fetch_message(int(board_message_id))
                    await message.edit(embed=embed, view=view)
                except (discord.NotFound, discord.HTTPException, ValueError):
                    pass
            return

        if board_message_id:
            try:
                old_board = await thread.fetch_message(int(board_message_id))
                await old_board.delete()
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


async def refresh_all_bail_pair_boards(round_id: int) -> None:
    """모든 방의 진행판을 다시 그린다. 협상 → 실행 전이처럼 버튼 구성이 바뀔 때 쓴다."""
    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
    if not round_row:
        return
    for pair in round_extra(round_row).get("bail_pairs", []):
        thread_id = str(pair.get("thread_id") or "")
        if not thread_id:
            continue
        try:
            thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
        except (discord.HTTPException, ValueError):
            continue
        if not isinstance(thread, discord.Thread):
            continue
        await redraw_bail_pair_board(round_id, thread, str(pair["user_a_id"]))


async def refresh_main_bail_board(round_id: int) -> None:
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
        try:
            bot.sand_board_locks.pop(int(thread_id), None)
            thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
            if not isinstance(thread, discord.Thread):
                continue
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
        games_done = bail_games_complete(extra)

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
            bot.schedule_auto_close(round_id, float(transition["close_at"]))
            await refresh_all_bail_pair_boards(round_id)
            await refresh_main_bail_board(round_id)
            channel = await resolve_text_channel(channel_id)
            if channel is not None:
                try:
                    await channel.send(
                        "🪣 모든 방의 협상이 끝났습니다. **실행 단계**를 시작합니다 — "
                        f"각자 실제로 퍼낼 횟수를 비공개로 제출하세요. (마감 {close_time_text(transition['close_at'])})"
                    )
                except discord.HTTPException:
                    pass
            return

    await refresh_bail_pair_board(interaction, round_id)
    await refresh_main_bail_board(round_id)

    if not games_done:
        return
    result = close_round_record(round_id, interaction.guild, closed_by="모든 1:1 게임 종료")
    if not result:
        return
    task = bot.auto_close_tasks.pop(round_id, None)
    if task:
        task.cancel()
    await publish_bail_pair_results(round_id)

    # followup은 버튼을 누른 비공개 방으로 들어간다. 라운드 결과는 메인 채널에 알려야 한다.
    _, result_embed = result
    channel = await resolve_text_channel(channel_id)
    if channel is not None:
        try:
            await channel.send(embed=result_embed, view=planned_mini_game_view(round_id))
        except discord.HTTPException:
            pass
    await publish_event_summary_if_complete(round_row["event_session_id"])


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

        key = "scoop_a" if user_id == str(pair["user_a_id"]) else "scoop_b"
        if pair.get(key) is not None:
            await interaction.response.send_message(
                "이미 제출했습니다. 변경할 수 없습니다.", ephemeral=True
            )
            return
        pair[key] = scoops
        both_submitted = pair.get("scoop_a") is not None and pair.get("scoop_b") is not None
        if both_submitted:
            resolve_bail_pair(pair)
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
                "reward_share": bail_reward_share(pair, bail_side_key(pair, user_id)),
                "negotiation_failed": not pair.get("reward_split"),
            },
        )
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )

    suffix = (
        " 두 사람의 제출이 끝났습니다. 결과를 확인하세요!"
        if both_submitted
        else " 상대의 제출을 기다립니다."
    )
    await advance_or_finish_bail_round(
        interaction,
        round_id,
        private_message=f"**{scoops}번** 퍼내기로 제출했습니다.{suffix}",
    )


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


# 하루 횟수를 언제 차감할지의 기준.
#   playing/finished          : 게임이 성립했으므로 양쪽 모두 차감
#   betting 만료(accepted_at O): 베팅을 넣지 않은 쪽만 차감
#   pending 만료 / 거절        : 아무도 차감하지 않음 (거절은 언제나 공짜)
#
# betting 만료를 미입력자에게만 물리는 이유: 수락까지 받아놓고 베팅을 안 넣으면
# 상대는 동시 1결투 제한에 묶여 아무것도 못 한다. 차감이 없으면 무한 반복이 가능하다.
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


def get_roulette_stack(conn: sqlite3.Connection, user_id: str) -> int:
    row = conn.execute(
        "SELECT roulette_win_stack FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    return int(row["roulette_win_stack"]) if row else 0


def set_roulette_stack(
    conn: sqlite3.Connection,
    user_id: str,
    value: int,
    reason: str,
    before: int | None = None,
) -> None:
    """스택은 카운터라 덮어쓰면 이전 값이 사라진다. 덮기 직전에 활동 로그를 남긴다.

    reason: win | payout | loss | escaped
    """
    current = get_roulette_stack(conn, user_id) if before is None else before
    if current == value:
        return
    log_activity(
        conn,
        "roulette",
        "stack",
        actor_user_id=user_id,
        payload={"before": current, "after": value, "reason": reason},
    )
    conn.execute(
        "UPDATE users SET roulette_win_stack = ? WHERE user_id = ?", (value, user_id)
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
        if isinstance(client, EventBot):
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
    if isinstance(client, EventBot):
        # 제때 눌렀으니 이전 차례의 타이머는 취소한다. 취소되면 카운트다운 함수가
        # 자기가 뿌린 예고/카운트다운 메시지를 지우고 나가므로 채널에 흔적이 남지 않는다.
        if state == "blank" and game["expires_at"] is not None:
            client.schedule_roulette_turn(
                game_id, float(game["expires_at"]), int(game["shots_fired"])
            )
        else:
            client.cancel_roulette_turn(game_id)


class EventBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        # 멤버 캐시를 채워 member_name() 미스를 없앤다. Developer Portal의
        # SERVER MEMBERS INTENT 토글이 함께 ON이어야 로그인이 된다 (issue #19).
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.auto_close_tasks: dict[int, asyncio.Task[None]] = {}
        # 라운드 타이머와 키가 겹치면 안 된다(둘 다 int 키인데 round_id와 game_id는 별개다).
        self.roulette_turn_tasks: dict[int, asyncio.Task[None]] = {}
        self.auto_close_restored = False
        self.afk_coin_task: asyncio.Task[None] | None = None
        self.sand_board_locks: dict[int, asyncio.Lock] = {}
        self.anon_board_locks: dict[int, asyncio.Lock] = {}

    def sand_board_lock(self, thread_id: int) -> asyncio.Lock:
        """방별 진행판 재게시를 직렬화한다. 두 사람이 동시에 눌러도 진행판이 하나만 남는다."""
        return self.sand_board_locks.setdefault(thread_id, asyncio.Lock())

    def anon_board_lock(self, thread_id: int) -> asyncio.Lock:
        """익명 토론방 버튼판 재게시를 직렬화한다. 여러 명이 동시에 발언해도 버튼판이 하나만 남는다."""
        return self.anon_board_locks.setdefault(thread_id, asyncio.Lock())

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
            # 이걸 빠뜨리면 재시작 순간 진행 중이던 차례의 타이머가 사라져, 상대가
            # 방아쇠를 안 당기면 예치금이 영구 동결된다(자동 패배가 영영 안 온다).
            self.restore_roulette_turn_tasks()
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

    def schedule_roulette_turn(
        self, game_id: int, deadline: float, shot_index: int
    ) -> None:
        existing = self.roulette_turn_tasks.pop(game_id, None)
        if existing:
            existing.cancel()
        self.roulette_turn_tasks[game_id] = asyncio.create_task(
            self.watch_roulette_turn(game_id, deadline, shot_index)
        )

    def cancel_roulette_turn(self, game_id: int) -> None:
        task = self.roulette_turn_tasks.pop(game_id, None)
        if task:
            task.cancel()

    def restore_roulette_turn_tasks(self) -> None:
        """진행 중이던 차례 타이머를 DB에서 복구한다.

        라운드 쪽 복구(restore_auto_close_tasks)는 rounds.extra_json을 읽어서 룰렛에는
        쓸 수 없다. 룰렛은 별도 테이블이고 마감 시각을 expires_at에 들고 있다.
        """
        with db() as conn:
            rows = conn.execute(
                "SELECT id, expires_at, shots_fired FROM russian_roulette_games "
                "WHERE status = 'playing' AND expires_at IS NOT NULL"
            ).fetchall()

        for row in rows:
            self.schedule_roulette_turn(
                int(row["id"]), float(row["expires_at"]), int(row["shots_fired"])
            )

    async def watch_roulette_turn(
        self, game_id: int, deadline: float, shot_index: int
    ) -> None:
        """차례 제한 시간을 지키고, 넘기면 자동 패배로 마감한다."""
        try:
            await asyncio.sleep(
                max(0, deadline - time.time() - AUTO_CLOSE_WARNING_SECONDS)
            )
            with db() as conn:
                row = conn.execute(
                    "SELECT * FROM russian_roulette_games "
                    "WHERE id = ? AND status = 'playing'",
                    (game_id,),
                ).fetchone()
                game = dict(row) if row else None
            # 그새 격발돼서 차례가 넘어갔거나 게임이 끝났으면 낡은 타이머다.
            if not game or int(game["shots_fired"]) != shot_index:
                return

            channel = self.get_channel(int(game["channel_id"]))
            if channel is None:
                try:
                    channel = await self.fetch_channel(int(game["channel_id"]))
                except discord.HTTPException:
                    channel = None
            if channel is None:
                await asyncio.sleep(max(0, deadline - time.time()))
            else:
                await publish_auto_close_countdown(channel, "차례 종료", deadline)

            with db() as conn:
                finished = timeout_roulette_turn(conn, game_id, shot_index)
            if finished is None:
                return
            await refresh_roulette_message(self, finished)
        except asyncio.CancelledError:
            raise
        finally:
            if self.roulette_turn_tasks.get(game_id) is asyncio.current_task():
                self.roulette_turn_tasks.pop(game_id, None)

    async def auto_close_round(self, round_id: int, close_at: float) -> None:
        try:
            await asyncio.sleep(
                max(0, close_at - time.time() - AUTO_CLOSE_WARNING_SECONDS)
            )
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

            await publish_auto_close_countdown(
                channel,
                auto_close_action_label(row),
                close_at,
            )

            # 카운트다운 도중 수동 마감이나 긴급 연장으로 상태가 바뀔 수 있다.
            # 취소 신호가 늦게 도착해도 오래된 마감 시각으로 정산하지 않도록 다시 확인한다.
            with db() as conn:
                fresh_row = conn.execute(
                    "SELECT * FROM rounds WHERE id = ? AND status = 'open'",
                    (round_id,),
                ).fetchone()
            if not fresh_row:
                return
            try:
                current_close_at = float(round_extra(fresh_row).get("auto_close_at") or 0)
            except (TypeError, ValueError):
                return
            if abs(current_close_at - close_at) > 0.5:
                return
            row = fresh_row

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

            # 가라앉는 배는 단계가 하나 더 있다: 모집 → 협상 → 실행 → 정산.
            if round_game_key(row) == "bail":
                bail_phase = round_extra(row).get("bail_phase")
                if bail_phase == "registration":
                    transition = start_bail_negotiation(round_id)
                    if not transition:
                        return
                    await create_bail_pair_threads(transition)
                    await update_trust_main_message(transition)
                    if transition["state"] == "negotiation":
                        self.schedule_auto_close(round_id, float(transition["close_at"]))
                    return
                if bail_phase == "negotiation":
                    transition = start_bail_execution(round_id)
                    if not transition:
                        return
                    await refresh_all_bail_pair_boards(round_id)
                    await refresh_main_bail_board(round_id)
                    self.schedule_auto_close(round_id, float(transition["close_at"]))
                    if hasattr(channel, "send"):
                        try:
                            await channel.send(
                                "🪣 협상 시간이 끝났습니다. **실행 단계**를 시작합니다 — "
                                "각자 실제로 퍼낼 횟수를 비공개로 제출하세요. "
                                f"(마감 {close_time_text(transition['close_at'])})"
                            )
                        except discord.HTTPException:
                            pass
                    return

            row_extra = round_extra(row)
            if (
                row["type"] == "balance"
                and row_extra.get("mode") == "minority_balance"
                and row_extra.get("balance_phase") == "initial"
            ):
                transition = start_minority_balance_revote(round_id)
                if not transition:
                    return
                await publish_minority_balance_revote(transition)
                self.schedule_auto_close(round_id, float(transition["close_at"]))
                return

            result = close_round_record(round_id, guild, closed_by="자동 마감")
            if not result:
                return

            _, embed = result
            if row["type"] == "trust":
                await publish_trust_pair_results(round_id)
            elif round_game_key(row) == "bail":
                await publish_bail_pair_results(round_id)
            elif row["type"] == "sand":
                await publish_sand_pair_results(round_id)
            await archive_round_discussion_thread(row, reason="라운드 종료 후 영구 보관")
            if hasattr(channel, "send"):
                await channel.send(embed=embed, view=planned_mini_game_view(round_id))
            await publish_event_summary_if_complete(row["event_session_id"])
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
        if action in {
            "rouletteaccept",
            "roulettedecline",
            "roulettebet",
            "roulettescore",
            "roulettesettle",
            "roulettetrigger",
        }:
            try:
                game_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 러시안룰렛 게임 번호입니다.", ephemeral=True)
                return
            if action == "rouletteaccept":
                await handle_roulette_accept(interaction, game_id)
            elif action == "roulettedecline":
                await handle_roulette_decline(interaction, game_id)
            elif action == "roulettebet":
                await handle_roulette_bet(interaction, game_id)
            elif action == "roulettescore":
                await handle_roulette_score(interaction, game_id)
            elif action == "roulettesettle":
                await handle_roulette_settle(interaction, game_id)
            else:
                await handle_roulette_trigger(interaction, game_id)
            return

        if action == "eventjoin":
            try:
                event_session_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 이벤트 번호입니다.", ephemeral=True)
                return
            await handle_event_join(interaction, event_session_id)
            return

        if action == "eventcancel":
            try:
                event_session_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 이벤트 번호입니다.", ephemeral=True)
                return
            await handle_event_cancel(interaction, event_session_id)
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

        # 협상은 보상금(0~8)을, 실행은 퍼낼 횟수(0~6)를 고르므로 허용 범위가 다르다.
        if action in {"bailoffer", "bailscoop"}:
            choices = BAIL_REWARD_CHOICES if action == "bailoffer" else BAIL_SCOOP_CHOICES
            if len(parts) != 4 or parts[3] not in choices:
                await interaction.response.send_message(
                    "잘못된 보상금 제안입니다." if action == "bailoffer" else "잘못된 퍼내기 횟수입니다.",
                    ephemeral=True,
                )
                return
            try:
                round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return
            if action == "bailoffer":
                await handle_bail_offer(interaction, round_id, int(parts[3]))
            else:
                await handle_bail_scoop(interaction, round_id, int(parts[3]))
            return

        if action == "bailrespond":
            if len(parts) != 4 or parts[3] not in {"accept", "reject"}:
                await interaction.response.send_message("잘못된 협상 응답입니다.", ephemeral=True)
                return
            try:
                round_id = int(parts[2])
            except ValueError:
                await interaction.response.send_message("잘못된 라운드 번호입니다.", ephemeral=True)
                return
            await handle_bail_response(interaction, round_id, parts[3] == "accept")
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

            game_key = round_game_key(round_row)
            if game_key == "unique_number" and choice not in UNIQUE_NUMBER_CHOICES:
                await interaction.response.send_message(
                    "홍피겔만의 황금금고에서는 1~15 중 하나만 선택할 수 있습니다.",
                    ephemeral=True,
                )
                return

            ensure_user(conn, interaction.user)
            ts = now_text()
            # 덮어쓰기 전 값. 소수파 밸런스는 1차 선택이 재선택으로 덮이므로
            # 이 로그가 없으면 '누가 무엇에서 무엇으로 바꿨는지'가 영구히 사라진다.
            previous = conn.execute(
                "SELECT choice FROM votes WHERE round_id = ? AND user_id = ?",
                (round_id, str(interaction.user.id)),
            ).fetchone()
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
            log_activity(
                conn,
                "vote",
                "cast",
                event_session_id=round_row["event_session_id"],
                round_id=round_id,
                actor_user_id=str(interaction.user.id),
                payload={
                    "game_key": game_key,
                    "choice": choice,
                    "previous_choice": previous["choice"] if previous else None,
                    "changed": bool(previous and previous["choice"] != choice),
                    # 소수파 밸런스의 1차/재선택 구분. 어느 단계의 표인지 사후에 알아야 한다.
                    "phase": round_extra(round_row).get("balance_phase"),
                    "in_thread": str(interaction.channel_id)
                    != str(round_row["channel_id"]),
                },
            )

        await interaction.response.send_message(
            f"`{choice_display(choice, game_key)}` 선택을 저장했습니다. 마감 전까지 버튼을 다시 누르면 선택이 바뀝니다.",
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
    embed.add_field(name="🅰️", value=option_a, inline=True)
    embed.add_field(name="🅱️", value=option_b, inline=True)
    embed.add_field(
        name="📜 규칙",
        value=(
            "> 🔒 투표 현황은 마감 전까지 비공개입니다.\n"
            "> 🕶️ 토론 스레드에서 익명으로 몇 번이든 발언할 수 있습니다.\n"
            "> 🏆 마감 후 다수파 선택자에게 **+1점**이 지급됩니다.\n"
            "> ✨ 단, 한 선택지를 단 1명만 골랐다면 그 사람만 **+2점**, 다수파는 **0점**입니다.\n"
            "> ⚠️ 단, 한쪽이 0명이면 밸런스가 성립하지 않으므로 고른 사람 전원 **-2점**입니다."
        ),
        inline=False,
    )
    return embed


def build_minority_balance_revote_embed(
    round_row: sqlite3.Row | dict[str, Any],
    first_a_count: int,
    first_b_count: int,
    close_at: float,
) -> discord.Embed:
    """1차 집계 뒤에 처음으로 변형 규칙을 공개하는 재선택 안내."""
    embed = discord.Embed(
        title=f"1차 집계 · {round_row['question']}",
        description=(
            "첫 선택의 인원수가 공개되었습니다. 지금부터 같은 선택지로 다시 선택합니다.\n"
            "**1차 선택은 그대로 유지됩니다.** 선택을 바꾸고 싶은 경우에만 A/B 버튼을 다시 눌러주세요."
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name=f"🅰️ {round_row['option_a']}",
        value=f"**{first_a_count}명**",
        inline=True,
    )
    embed.add_field(
        name=f"🅱️ {round_row['option_b']}",
        value=f"**{first_b_count}명**",
        inline=True,
    )
    embed.add_field(
        name="🔓 재선택 규칙 공개",
        value=(
            "> 🏆 최종 집계에서 **인원이 더 적은 선택지**를 고른 사람 전원에게 **+1점**이 지급됩니다.\n"
            "> ⚠️ 두 선택지의 인원이 같거나, 아무도 고르지 않은 쪽이 소수라면 점수는 지급되지 않습니다.\n"
            "> 🔄 재선택 시간 동안에는 선택을 몇 번이든 바꿀 수 있으며, 누르지 않으면 1차 선택으로 최종 집계됩니다."
        ),
        inline=False,
    )
    embed.add_field(
        name="⏳ 재선택 마감",
        value=f"**{close_time_text(close_at)}**",
        inline=False,
    )
    return embed


def start_minority_balance_revote(round_id: int) -> dict[str, Any] | None:
    """1차 표를 유지한 채 같은 라운드를 재선택 단계로 전환한다."""
    with db() as conn:
        round_row = conn.execute(
            "SELECT * FROM rounds WHERE id = ? AND type = 'balance' AND status = 'open'",
            (round_id,),
        ).fetchone()
        if not round_row:
            return None

        extra = round_extra(round_row)
        if extra.get("mode") != "minority_balance" or extra.get("balance_phase") != "initial":
            return None

        counts = {"A": 0, "B": 0}
        for vote in conn.execute(
            "SELECT choice FROM votes WHERE round_id = ?",
            (round_id,),
        ).fetchall():
            choice = str(vote["choice"])
            if choice in counts:
                counts[choice] += 1

        duration_minutes = max(1, int(extra.get("duration_minutes") or 1))
        close_at = time.time() + duration_minutes * 60
        extra.update(
            {
                "balance_phase": "revote",
                "first_tally": counts,
                "first_tallied_at": now_text(),
                "auto_close_at": close_at,
            }
        )
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_id),
        )

        return {
            "round_id": round_id,
            "channel_id": str(round_row["channel_id"]),
            "discussion_thread_id": str(round_row["discussion_thread_id"] or ""),
            "question": str(round_row["question"]),
            "option_a": str(round_row["option_a"]),
            "option_b": str(round_row["option_b"]),
            "first_a_count": counts["A"],
            "first_b_count": counts["B"],
            "close_at": close_at,
        }


async def publish_minority_balance_revote(transition: dict[str, Any]) -> bool:
    """재선택 안내를 메인 채널에 게시하고 토론방의 고정 버튼도 새 마감으로 갱신한다."""
    round_id = int(transition["round_id"])
    close_at = float(transition["close_at"])
    channel = await resolve_text_channel(str(transition["channel_id"]))
    if channel is None:
        return False

    embed = build_minority_balance_revote_embed(
        transition,
        int(transition["first_a_count"]),
        int(transition["first_b_count"]),
        close_at,
    )
    try:
        await channel.send(embed=embed, view=vote_view(round_id, BALANCE_VOTE_CHOICES))
    except discord.HTTPException:
        return False

    thread_id = str(transition.get("discussion_thread_id") or "")
    if not thread_id:
        return True
    try:
        thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
    except (discord.HTTPException, ValueError):
        return True
    if not isinstance(thread, discord.Thread):
        return True

    with db() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if not round_row:
            return True
        extra = round_extra(round_row)
        old_button_message_id = str(extra.get("anon_button_message_id") or "")
        post_count = count_discussion_messages(conn, round_id)

    if old_button_message_id:
        try:
            old_button = await thread.fetch_message(int(old_button_message_id))
            await old_button.delete()
        except (discord.NotFound, discord.HTTPException, ValueError):
            pass
    try:
        new_button = await thread.send(
            embed=build_anonymous_guide_embed(post_count, close_at),
            view=thread_discussion_view(round_id, "minority_balance"),
        )
    except discord.HTTPException:
        return True

    with db() as conn:
        fresh_row = conn.execute("SELECT extra_json FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if fresh_row:
            extra = round_extra(fresh_row)
            extra["anon_button_message_id"] = str(new_button.id)
            conn.execute(
                "UPDATE rounds SET extra_json = ? WHERE id = ?",
                (json.dumps(extra, ensure_ascii=False), round_id),
            )
    return True


def build_defuse_embed(round_id: int, title: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=title,
        color=discord.Color.dark_red(),
    )
    embed.add_field(
        name="🔌 선택 가능한 선",
        value="　　".join(f"{color['emoji']} {color['name']}" for color in DEFUSE_COLORS),
        inline=False,
    )
    embed.add_field(
        name="📜 규칙",
        value=(
            "> 🎯 빨강, 파랑, 초록 중 하나를 선택합니다.\n"
            "> 💥 마감 후 가장 많은 표를 받은 선이 폭발하며, 그 선을 고른 사람은 **-1점**입니다.\n"
            "> ⚖️ 최다 득표가 동률이면 공동 1위인 선이 모두 폭발합니다.\n"
            "> 🕶️ 해체 토론방에서는 익명으로 몇 번이든 심리전과 블러핑을 할 수 있습니다."
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
        name="📈 예측 단계",
        value=" │ ".join(f"{level}강화" for level in CUTTING_LEVEL_CHOICES),
        inline=False,
    )
    embed.add_field(
        name="📜 규칙",
        value=(
            "> 🎯 처음 실패하기 직전의 강화 단계를 맞히는 게임입니다.\n"
            "> 🔨 투표 집계가 끝나면 운영자가 직접 세공을 진행합니다."
        ),
        inline=False,
    )
    # 확률과 보상은 상수에서 만든다. 손으로 적어두면 상수를 바꿨을 때 안내문만 옛날 값으로 남는다.
    # 나란히 붙여서 "확률 대비 보상"을 한눈에 비교하게 한다.
    embed.add_field(
        name="🎲 강화 성공 확률",
        value=f"```\n{cutting_success_rate_text()}\n```",
        inline=True,
    )
    embed.add_field(
        name="🏆 정답 보상",
        value=f"```\n{cutting_reward_text()}\n```",
        inline=True,
    )
    return embed


def build_thread_notice_embed(round_id: int, thread: discord.Thread, title: str, guide: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=f"{thread.mention}\n{guide}",
        color=discord.Color.gold(),
    )
    embed.add_field(name="👉 참여 방법", value="위 스레드를 눌러 의견을 남겨주세요.", inline=False)
    return embed


def build_top3_embed(round_id: int, title: str, candidates: list[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=title,
        color=discord.Color.orange(),
    )
    medals = ["🥇", "🥈", "🥉"]
    candidate_lines = [
        f"> {medals[index - 1] if index <= len(medals) else '▫️'} "
        f"<@{candidate['user_id']}> ─ **{candidate['score']}점**"
        for index, candidate in enumerate(candidates, start=1)
    ]
    embed.add_field(name="👑 후보", value="\n".join(candidate_lines), inline=False)
    embed.add_field(
        name="📜 규칙",
        value=(
            "> 🗳️ 현재 점수가 가장 높은 3명과 `선택하지 않는다` 중 하나에 투표합니다.\n"
            "> 🚫 후보로 오른 3명은 투표할 수 없습니다.\n"
            "> 🏆 `선택하지 않는다` 득표수가 세 후보 득표수 합보다 많으면 `선택하지 않는다`를 고른 사람 전원이 **+1점**입니다.\n"
            "> 🎯 그렇지 않으면 세 후보 중 가장 많이 선택된 1명이 **-1점**입니다.\n"
            "> ⚠️ 아무도 투표하지 않으면 이 라운드는 무효 처리됩니다."
        ),
        inline=False,
    )
    return embed


def unique_number_reward(number: int) -> int:
    """1~5는 1점, 6~10은 2점, 11~15는 3점이다."""
    if not 1 <= number <= 15:
        raise ValueError("홍피겔만의 황금금고의 숫자는 1~15여야 합니다.")
    return ((number - 1) // 5) + 1


def build_unique_number_embed(round_id: int, title: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description="1부터 15까지 중 하나를 선택하세요.",
        color=discord.Color.purple(),
    )
    embed.add_field(
        name="📜 규칙",
        value=(
            "> 👥 같은 숫자를 고른 사람이 2명 이상이면 그 숫자의 선택자는 모두 제외됩니다.\n"
            "> 🏆 중복되지 않은 숫자 중 **가장 높은 숫자**를 고른 단 한 명만 해당 구간의 점수를 받습니다.\n"
            "> ❌ 중복되지 않은 숫자가 없으면 점수는 지급되지 않습니다.\n"
            "> 🕶️ 토론 스레드에서 익명으로 몇 번이든 심리전을 펼칠 수 있습니다."
        ),
        inline=False,
    )
    embed.add_field(
        name="🎯 배점",
        value="```\n1~5     +1점\n6~10    +2점\n11~15   +3점\n```",
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
        name="🎮 진행 게임",
        value="딜레마의 섬",
        inline=False,
    )
    embed.add_field(
        name="📜 메인 규칙",
        value=(
            "무작위로 매칭된 두 명의 참가자가 신뢰와 배신을 선택합니다.\n"
            "```\n"
            "상호 신뢰 시   : 각 +2점\n"
            "한명만 배신 시 : 배신자 +5점, 상대 0점\n"
            "동시 배신 시   : 각 -3점\n"
            "```\n"
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
    close_at: float | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=title,
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="🎲 랜덤 매칭 완료",
        value=f"총 **{len(pairs)}세트**의 비공개 협상방을 열었습니다. 각자 초대된 방에서 대화와 선택을 진행하세요.",
        inline=False,
    )
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
    if emergency_user_id:
        embed.add_field(
            name="🚨 긴급 투입",
            value=emergency_join_notice(emergency_user_id),
            inline=False,
        )
    embed.add_field(
        name="⚔️ 선택 규칙 (3분)",
        value=(
            "비공개 협상방에서 매칭된 짝과 대화한 뒤 `신뢰` 또는 `배신`을 선택하세요. 서로의 선택은 마감 후 해당 방에서 공개됩니다.\n"
            "> 선택은 마감 전까지 **몇 번이든 바꿀 수 있습니다.** 끝까지 고르지 않으면 랜덤으로 배정됩니다."
        ),
        inline=False,
    )
    if close_at:
        embed.add_field(
            name="⏳ 선택 마감",
            value=f"**{close_time_text(close_at)}**",
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
        name="🎮 진행 게임",
        value="모래성 게임",
        inline=False,
    )
    embed.add_field(
        name="📜 메인 규칙",
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
    embed.add_field(name="⚔️ 진행 상황", value=sand_pair_status_text(pair), inline=False)
    embed.add_field(name="📜 규칙", value=SAND_RULES_TEXT, inline=False)
    if str(pair.get("phase")) != "resolved":
        embed.add_field(
            name="⏳ 제한 시간",
            value=(
                f"예정 마감: **{close_time_text(extra.get('play_close_at'))}**\n"
                "> 시간 안에 끝내지 못하면 남은 숫자와 행동이 랜덤으로 배정됩니다."
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
            name="🔢 서로의 숫자",
            value=(
                f"<@{pair['user_a_id']}>: **{number_a}**\n"
                f"<@{pair['user_b_id']}>: **{number_b}**\n"
                f"> 차이 **{abs(int(number_a) - int(number_b))}** → "
                f"**{abs(int(number_a) - int(number_b)) + 1}번째** 덜기에서 붕괴"
            ),
            inline=False,
        )
    embed.add_field(
        name="🏁 결과",
        value=f"**{SAND_OUTCOME_LABELS.get(str(pair.get('outcome')), '미완료')}**",
        inline=False,
    )
    embed.add_field(name="💰 정산", value=sand_pair_settlement_text(pair), inline=False)
    if pair.get("auto_completed"):
        embed.add_field(name="🎲 자동 진행", value="제한 시간이 끝나 남은 선택이 랜덤으로 배정되었습니다.", inline=False)
    return embed


def build_sand_play_embed(round_row: sqlite3.Row, extra: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title=str(round_row["title"]),
        description="각자 초대된 1:1 비공개 방에서 숫자와 행동을 선택하세요. 선택 내용은 비공개입니다.",
        color=discord.Color.from_rgb(218, 165, 90),
    )
    lines = [
        f"**{index}.** <@{pair['user_a_id']}> ↔ <@{pair['user_b_id']}>\n"
        f"　└ {sand_pair_status_text(pair)}"
        for index, pair in enumerate(extra.get("sand_pairs", []), start=1)
    ]
    embed.add_field(name="⚔️ 1:1 진행 현황", value=field_text(lines), inline=False)
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
        embed.add_field(
            name="🚨 긴급 투입",
            value=emergency_join_notice(emergency_user_id),
            inline=False,
        )
    embed.add_field(
        name="⏳ 선택 제한 시간",
        value=(
            f"예정 마감: **{close_time_text(extra.get('play_close_at'))}**\n"
            "> 시간 안에 끝내지 못하면 남은 숫자와 행동이 랜덤으로 배정되어 자동으로 결과가 확정됩니다."
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


BAIL_EMBED_COLOR = discord.Color.from_rgb(60, 120, 180)


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
    submitted = int(pair.get("scoop_a") is not None) + int(pair.get("scoop_b") is not None)
    return f"퍼낼 횟수 제출 중 : {submitted}/2"


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
                "실제로 퍼낼 횟수를 고르세요. **번복 불가**이며 상대에게 공개되지 않습니다.\n"
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
        embed.add_field(
            name="⏳ 제한 시간",
            value=(
                f"{label}: **{close_time_text(close_at)}**\n"
                f"> 협상을 못 끝내면 결렬, 퍼내기를 제출하지 않으면 {BAIL_FALLBACK_SCOOPS}번으로 "
                "자동 제출됩니다."
            ),
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
            "**2단계 · 실행** — 각자 실제로 퍼낼 횟수를 비공개 제출"
            if round_phase == "execution"
            else "**1단계 · 협상** — 누가 몇 번 퍼낼지 교대로 제안"
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

    버튼은 한 줄에 5개가 상한이라 0~6 일곱 개를 4+3으로 나눠 배치한다.
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
                    row=index // 5,
                )
            )
        response_row = (len(BAIL_REWARD_CHOICES) + 4) // 5
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


def unique_number_vote_view(round_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for index, value in enumerate(UNIQUE_NUMBER_CHOICES):
        number = int(value)
        view.add_item(
            discord.ui.Button(
                label=value,
                style=(
                    discord.ButtonStyle.secondary
                    if number <= 5
                    else discord.ButtonStyle.primary
                    if number <= 10
                    else discord.ButtonStyle.success
                ),
                custom_id=f"eventbot:vote:{round_id}:{value}",
                row=index // 5,
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
    timestamp = int(float(close_at))
    return f"<t:{timestamp}:F>"


def auto_close_action_label(round_row: sqlite3.Row | dict[str, Any]) -> str:
    extra = round_extra(round_row)  # type: ignore[arg-type]
    if round_row["type"] == "trust" and extra.get("trust_phase") == "registration":
        return "참가 모집 종료 및 랜덤 매칭"
    if round_row["type"] == "sand" and extra.get("sand_phase") == "registration":
        return "참가 모집 종료 및 모래성 게임 시작"
    if extra.get("mode") == "bail":
        if extra.get("bail_phase") == "registration":
            return "참가 모집 종료 및 협상 시작"
        if extra.get("bail_phase") == "negotiation":
            return "협상 종료 및 실행 단계 시작"
    if extra.get("mode") == "minority_balance" and extra.get("balance_phase") == "revote":
        return "재선택 종료"
    game_key = round_game_key(round_row)  # type: ignore[arg-type]
    return f"{GAME_NAMES.get(game_key, game_key)} 종료"


async def publish_auto_close_countdown(
    channel: Any,
    action_label: str,
    close_at: float,
) -> None:
    """10초 전 공지를 남기고, 별도 메시지를 5→1 카운트다운으로 갱신한다."""
    remaining = close_at - time.time()
    if remaining <= 0 or not hasattr(channel, "send"):
        return

    warning_seconds = min(AUTO_CLOSE_WARNING_SECONDS, max(1, math.ceil(remaining)))
    warning_message = None
    countdown_message = None
    try:
        try:
            warning_message = await channel.send(
                f"⏳ **{action_label} {warning_seconds}초 전입니다.**"
            )
        except discord.HTTPException:
            warning_message = None

        for seconds in AUTO_CLOSE_COUNTDOWN_SECONDS:
            wait_seconds = close_at - time.time() - seconds
            if wait_seconds <= 0:
                continue
            await asyncio.sleep(wait_seconds)
            content = f"⏳ **{action_label}까지 {seconds}**"
            if countdown_message is None:
                try:
                    countdown_message = await channel.send(content)
                except discord.HTTPException:
                    countdown_message = None
            else:
                try:
                    await countdown_message.edit(content=content)
                except discord.HTTPException:
                    countdown_message = None

        await asyncio.sleep(max(0, close_at - time.time()))
        if countdown_message is not None:
            try:
                await countdown_message.edit(content=f"🏁 **{action_label}!**")
            except discord.HTTPException:
                pass
    except asyncio.CancelledError:
        # 수동 마감·긴급 연장으로 기존 타이머가 취소되면 낡은 두 메시지를 모두 치운다.
        for message in (warning_message, countdown_message):
            if message is None:
                continue
            try:
                await message.delete()
            except discord.HTTPException:
                pass
        raise


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
    embed.add_field(name="📍 현재 단계", value=f"**{current_level}강화**", inline=True)
    embed.add_field(name="🔝 최대 단계", value=f"**{max_level}강화**", inline=True)
    embed.add_field(
        name="🎲 다음 시도 확률",
        value=f"**{success_rate * 100:.0f}%**" if success_rate is not None else "최대 단계 도달",
        inline=True,
    )
    embed.add_field(
        name="📊 진척도",
        value=f"{cutting_progress_bar(current_level, max_level)}　　**{current_level}** / {max_level}강",
        inline=False,
    )
    embed.add_field(
        name="🔨 직전 시도",
        value=logs[-1] if logs else "아직 시도 없음",
        inline=False,
    )
    embed.add_field(
        name="ℹ️ 진행 방식",
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
        f"{level}강화 ────── {len(grouped.get(level, []))}명"
        for level in CUTTING_LEVEL_CHOICES
    ]
    embed = discord.Embed(
        title=f"결과 · {round_row['title']}",
        description=round_row["title"],
        color=discord.Color.green(),
    )
    max_level = int(extra.get("max_level", 6))
    embed.add_field(
        name="📊 진척도",
        value=f"{cutting_progress_bar(result_level, max_level)}　　**{result_level}** / {max_level}강",
        inline=False,
    )
    embed.add_field(name="🔨 마지막 시도", value=logs[-1] if logs else "-", inline=False)
    embed.add_field(name="🏁 최종 단계", value=f"**{result_level}강화**", inline=True)
    embed.add_field(name="🎯 정답자", value=f"**{len(correct_users)}명**", inline=True)
    embed.add_field(name="📈 예측 분포", value="```\n" + "\n".join(prediction_lines) + "\n```", inline=False)
    embed.add_field(name="👥 정답자 명단", value=mention_list(correct_users), inline=False)
    embed.add_field(
        name="💰 정산",
        value=f"{result_level}강화를 맞힌 **{len(correct_users)}명** 전원 **+{reward_points}점**",
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
            conn.commit()
            await publish_event_summary_if_complete(round_row["event_session_id"])
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
                conn.commit()
                await publish_event_summary_if_complete(round_row["event_session_id"])
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
        conn.commit()
        await publish_event_summary_if_complete(round_row["event_session_id"])


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

        if round_row["type"] == "balance":
            a_users = [row["user_id"] for row in votes if row["choice"] == "A"]
            b_users = [row["user_id"] for row in votes if row["choice"] == "B"]

            scored = False
            is_minority_balance = extra.get("mode") == "minority_balance"
            if is_minority_balance:
                if not a_users and not b_users:
                    settlement = "재선택한 사람이 없어 점수가 지급되지 않았습니다."
                elif not a_users or not b_users:
                    settlement = "인원이 더 적은 쪽에 선택자가 없어 점수가 지급되지 않았습니다."
                elif len(a_users) == len(b_users):
                    settlement = "두 선택지의 인원이 같아 점수가 지급되지 않았습니다."
                else:
                    winner = "A" if len(a_users) < len(b_users) else "B"
                    winner_users = a_users if winner == "A" else b_users
                    for user_id in winner_users:
                        add_score(
                            conn,
                            user_id,
                            member_name(guild, user_id),
                            1,
                            f"라운드 #{round_row['id']} 소수파 선택",
                            round_row["id"],
                        )
                    scored = True
                    settlement = f"소수파 `{winner}` 선택자 {len(winner_users)}명 전원 +1점"
            else:
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
                    minority = "B" if winner == "A" else "A"
                    minority_users = b_users if winner == "A" else a_users
                    if len(minority_users) == 1:
                        minority_user_id = minority_users[0]
                        add_score(
                            conn,
                            minority_user_id,
                            member_name(guild, minority_user_id),
                            2,
                            f"라운드 #{round_row['id']} 단독 소수파 선택",
                            round_row["id"],
                        )
                    else:
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
                    if len(minority_users) == 1:
                        settlement = (
                            f"소수파 `{minority}` 단독 선택자만 +2점 · "
                            f"다수파 `{winner}` 선택자는 점수 없음"
                        )
                    else:
                        settlement = f"다수파 `{winner}` 선택자 전원 +1점"

            embed = discord.Embed(
                title=(
                    f"최종 결과 · {round_row['title']}"
                    if is_minority_balance
                    else f"결과 · {round_row['title']}"
                ),
                description=round_row["question"],
                color=discord.Color.green() if scored else discord.Color.light_grey(),
            )
            # 누가 무엇을 골랐는지는 공개하지 않는다. 인원수만.
            embed.add_field(
                name=f"🅰️ {round_row['option_a']}",
                value=f"**{len(a_users)}명**",
                inline=True,
            )
            embed.add_field(
                name=f"🅱️ {round_row['option_b']}",
                value=f"**{len(b_users)}명**",
                inline=True,
            )
            embed.add_field(name="💰 정산", value=settlement, inline=False)
        elif round_row["type"] == "bomb":
            wires: dict[str, str] = extra["wires"]
            grouped: dict[str, list[str]] = {}
            for row in votes:
                if row["choice"] in wires:
                    grouped.setdefault(row["choice"], []).append(row["user_id"])

            if extra.get("settlement_rule") == "top_vote_penalty":
                highest_vote_count = max((len(users) for users in grouped.values()), default=0)
                exploded_wire_ids = {
                    wire_id
                    for wire_id, users in grouped.items()
                    if highest_vote_count > 0 and len(users) == highest_vote_count
                }
                penalized_users = [
                    user_id
                    for wire_id in exploded_wire_ids
                    for user_id in grouped.get(wire_id, [])
                ]
                for user_id in penalized_users:
                    add_score(
                        conn,
                        user_id,
                        member_name(guild, user_id),
                        -1,
                        f"라운드 #{round_row['id']} 최다 득표 선 선택",
                        round_row["id"],
                    )

                embed = discord.Embed(
                    title=f"결과 · {round_row['title']}",
                    description=round_row["title"],
                    color=discord.Color.red() if penalized_users else discord.Color.light_grey(),
                )
                exploded_names = ", ".join(
                    wires[wire_id] for wire_id in wires if wire_id in exploded_wire_ids
                )
                embed.add_field(
                    name="💥 폭발한 선",
                    value=f"**{exploded_names}**" if exploded_wire_ids else "없음 (투표 없음)",
                    inline=False,
                )
                add_field_divider(embed)
                # 세 선을 inline 필드로 나란히(가로) 배치한다. 폭발선은 위 헤드라인이 알려주므로
                # 이름은 색만, 값은 선택자 멘션을 세로로 쌓아 칸을 좁게 유지한다(모바일에서 가로로 잘 붙게).
                for wire_id, wire_name in wires.items():
                    users = grouped.get(wire_id, [])
                    embed.add_field(
                        name=wire_name,
                        value=mention_list(users, sep="\n"),
                        inline=True,
                    )
                add_field_divider(embed)

                if not penalized_users:
                    settlement = "투표가 없어 점수 변동이 없습니다."
                elif len(exploded_wire_ids) == 1:
                    settlement = (
                        f"최다 득표 선을 고른 {len(penalized_users)}명 전원 -1점"
                    )
                else:
                    settlement = (
                        f"최다 득표 {highest_vote_count}표 동률로 공동 1위 "
                        f"{len(exploded_wire_ids)}개 선이 모두 폭발했습니다. "
                        f"선택자 {len(penalized_users)}명 전원 -1점"
                    )
                embed.add_field(
                    name="💰 정산",
                    value=settlement,
                    inline=False,
                )
            else:
                # 배포 시점에 이미 열려 있던 구방식 라운드는 기존 안전선 규칙으로 마감한다.
                safe_wire_ids = set(extra["safe_wire_ids"])
                safe_users = [
                    row["user_id"] for row in votes if row["choice"] in safe_wire_ids
                ]
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
                    value=(
                        f"{'해체 성공자' if is_defuse else '안전선 선택자'} "
                        f"{len(safe_users)}명 전원 +1점"
                    ),
                    inline=False,
                )
        elif round_row["type"] == "grid" and extra.get("mode") == "unique_number":
            grouped: dict[int, list[str]] = {}
            for row in votes:
                if str(row["choice"]) not in UNIQUE_NUMBER_CHOICES:
                    continue
                grouped.setdefault(int(row["choice"]), []).append(str(row["user_id"]))

            unique_numbers = [number for number, users in grouped.items() if len(users) == 1]
            winning_number = max(unique_numbers, default=None)
            winner_id = grouped[winning_number][0] if winning_number is not None else None
            reward = unique_number_reward(winning_number) if winning_number is not None else 0

            if winner_id is not None and winning_number is not None:
                add_score(
                    conn,
                    winner_id,
                    member_name(guild, winner_id),
                    reward,
                    f"라운드 #{round_row['id']} 최고 유일 숫자 {winning_number}",
                    round_row["id"],
                )

            # 마커 이모지+라벨을 줄 맨 앞에 둬서 우승/중복/유일이 한눈에 구분되게 한다.
            result_lines = []
            for number in sorted(grouped, reverse=True):
                users = grouped[number]
                if number == winning_number:
                    result_lines.append(
                        f"🏆최고 유일 숫자 : {number} · {mention_list(users)}"
                    )
                elif len(users) > 1:
                    result_lines.append(
                        f"❌중복 숫자 선택 : {number} · {mention_list(users)}"
                    )
                else:
                    result_lines.append(
                        f"▫️유일하기만 한 숫자 : {number} · {mention_list(users)}"
                    )

            embed = discord.Embed(
                title=f"결과 · {round_row['title']}",
                description=round_row["title"],
                color=discord.Color.green() if winner_id else discord.Color.light_grey(),
            )
            embed.add_field(
                name="🔢 숫자별 결과",
                value=field_text(result_lines, empty="선택한 사람이 없습니다."),
                inline=False,
            )
            add_field_divider(embed)
            if winner_id is None:
                settlement = (
                    "선택한 사람이 없어 점수가 지급되지 않았습니다."
                    if not grouped
                    else "모든 선택 숫자가 중복되어 점수가 지급되지 않았습니다."
                )
            else:
                settlement = f"<@{winner_id}>님이 최고 유일 숫자 **{winning_number}**으로 **+{reward}점**"
            embed.add_field(name="💰 정산", value=settlement, inline=False)
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
                    # 본인이 고른 표와 섞이면 안 된다. votes만 보면 구분이 안 되므로 여기서 남긴다.
                    log_activity(
                        conn,
                        "vote",
                        "auto_assigned",
                        event_session_id=round_row["event_session_id"],
                        round_id=round_row["id"],
                        actor_user_id=user_id,
                        payload={"choice": assigned, "reason": "마감까지 미선택"},
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
                            2,
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
                            -3,
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
                        5,
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
            # 라벨 폭을 한글 2칸 기준으로 맞춰 코드블록 안에서 콜론이 세로로 정렬되게 한다.
            distribution_lines = [
                f"신뢰      : {outcome_counts['mutual']}",
                f"단독 배신 : {outcome_counts['single']}",
                f"동시 배신 : {outcome_counts['double']}",
            ]
            if outcome_counts["incomplete"]:
                distribution_lines.append(f"미선택    : {outcome_counts['incomplete']}")
            embed.add_field(
                name="📊 전체 결과 분포",
                value="```\n" + "\n".join(distribution_lines) + "\n```",
                inline=False,
            )
            if auto_assigned:
                embed.add_field(
                    name="🎲 미선택 랜덤 배정",
                    value=f"**{auto_assigned}명**",
                    inline=False,
                )
            unmatched_user_id = str(extra.get("unmatched_user_id") or "")
            if unmatched_user_id:
                embed.add_field(
                    name="⏳ 매칭 대기",
                    value="**1명**",
                    inline=False,
                )
            embed.add_field(
                name="💰 정산",
                value="상호 신뢰는 각 +2점, 단독 배신은 배신자 +5점·상대 0점, 동시 배신은 각 -3점입니다.",
                inline=False,
            )
        elif round_game_key(round_row) == "bail":
            # 생존은 제출된 퍼내기 합만으로 결정되고, 보상은 협상에서 확정된 배분을 따른다.
            bail_lines: list[str] = []
            bail_auto_count = 0
            for pair in extra.get("bail_pairs", []):
                if str(pair.get("phase")) != "resolved":
                    autocomplete_bail_pair(pair)
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
            embed.add_field(
                name="💰 정산",
                value=(
                    f"생존하면 **보상 몫 − 퍼낸 횟수**(1회당 -{BAIL_SCOOP_COST}점)입니다.\n"
                    f"침몰하면 보상금을 못 받고 몫이 많은 쪽 -{BAIL_SINK_PENALTY_HIGH}점 / "
                    f"적은 쪽 -{BAIL_SINK_PENALTY_LOW}점 (같으면 각 -{BAIL_SINK_PENALTY_EVEN}점).\n"
                    "> ⚠️ 보상은 더 받고 노동은 덜 한 사람"
                ),
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
                            conn, user_id, member_name(guild, user_id), -3,
                            f"라운드 #{round_row['id']} 모래성 둘 다 포기", round_row["id"],
                        )
                elif outcome == "quit_success":
                    winner_id = str(result["winner"])
                    quitter_id = str(result["quitter"])
                    add_score(
                        conn, winner_id, member_name(guild, winner_id), 5,
                        f"라운드 #{round_row['id']} 모래성 포기 후 덜기 성공", round_row["id"],
                    )
                    add_score(
                        conn, quitter_id, member_name(guild, quitter_id), -3,
                        f"라운드 #{round_row['id']} 모래성 단독 포기", round_row["id"],
                    )
                elif outcome == "quit_collapse":
                    winner_id = str(result["winner"])
                    collapsed_user_id = str(result["collapsed_user"])
                    add_score(
                        conn, winner_id, member_name(guild, winner_id), 2,
                        f"라운드 #{round_row['id']} 모래성 상대 붕괴", round_row["id"],
                    )
                    add_score(
                        conn, collapsed_user_id, member_name(guild, collapsed_user_id), -3,
                        f"라운드 #{round_row['id']} 모래성 마지막 덜기 붕괴", round_row["id"],
                    )
                elif outcome == "plain_collapse":
                    collapsed_user_id = str(result["collapsed_user"])
                    add_score(
                        conn, collapsed_user_id, member_name(guild, collapsed_user_id), -3,
                        f"라운드 #{round_row['id']} 모래성 일반 붕괴", round_row["id"],
                    )
                else:
                    result_lines.append(
                        f"**{len(result_lines) + 1}.** <@{user_a_id}> ↔ <@{user_b_id}>\n"
                        f"　└ 제한 시간 내 미완료 (0점)"
                    )
                    continue

                completed_count += 1
                auto_mark = " · 자동 진행" if pair.get("auto_completed") else ""
                result_lines.append(
                    f"**{len(result_lines) + 1}.** <@{user_a_id}> ↔ <@{user_b_id}>\n"
                    f"　└ **{SAND_OUTCOME_LABELS[outcome]}** "
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
            embed.add_field(name="⚔️ 짝별 결과", value=field_text(result_lines), inline=False)
            unmatched_user_id = str(extra.get("unmatched_user_id") or "")
            if unmatched_user_id:
                embed.add_field(
                    name="⏳ 매칭 대기",
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
                lines.append(f"{level}강화 ────── {len(users)}명")

            embed = discord.Embed(
                title=f"집계 · {round_row['title']}",
                description=round_row["title"],
                color=discord.Color.teal(),
            )
            embed.add_field(
                name="📊 강화 단계별 예측",
                value="```\n" + "\n".join(lines) + "\n```",
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
                    f"<@{user_id}> ────── **{len(grouped.get(user_id, []))}표**"
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
            embed.add_field(name="🗳️ 후보별 득표", value="\n".join(candidate_lines), inline=False)
            embed.add_field(name="🚫 선택하지 않는다", value=f"**{none_votes}표**", inline=True)
            embed.add_field(name="👑 후보 득표 합", value=f"**{candidate_vote_sum}표**", inline=True)
            embed.add_field(name="🏁 결과", value=outcome, inline=False)
            embed.add_field(name="💰 정산", value=settlement, inline=False)
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


async def ensure_pair_thread_permissions(
    interaction: discord.Interaction,
    게임: str,
    channel: discord.TextChannel,
) -> bool:
    """딜레마의 섬·모래성은 짝마다 비공개 스레드를 연다. 시작 전에 권한을 확인한다."""
    game_name = GAME_NAMES[게임]
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("서버에서 봇 권한을 확인하지 못했습니다.", ephemeral=True)
        return False

    bot_member = guild.me
    if bot_member is None:
        await interaction.followup.send("서버에서 봇 권한을 확인하지 못했습니다.", ephemeral=True)
        return False

    permissions = channel.permissions_for(bot_member)
    if (
        permissions.create_private_threads
        and permissions.send_messages_in_threads
        and permissions.manage_threads
    ):
        return True

    await interaction.followup.send(
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
            duration = parse_duration_minutes(str(self.duration.value))
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


@bot.tree.command(name="오픈", description="새 이벤트 참가 모집을 열고 진행 흐름을 안내합니다.")
@app_commands.default_permissions()
@admin_only()
async def open_event(interaction: discord.Interaction) -> None:
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


@bot.tree.command(name="start", description="메인게임과 미니게임을 골라 이벤트를 시작합니다.")
@app_commands.default_permissions()
@admin_only()
@app_commands.choices(
    메인게임=[
        app_commands.Choice(name="딜레마의 섬", value="trust"),
        app_commands.Choice(name="모래성 게임", value="sand"),
        app_commands.Choice(name="가라앉는 배", value="bail"),
    ],
    미니게임=[
        app_commands.Choice(name="밸런스 게임", value="balance"),
        app_commands.Choice(name="소수파 밸런스 (1차 위장)", value="minority_balance"),
        app_commands.Choice(name="몰리면 터진다!", value="defuse4"),
        app_commands.Choice(name="돌 세공 예측", value="cutting"),
        app_commands.Choice(name="상위 3명 견제", value="top3"),
        app_commands.Choice(name="홍피겔만의 황금금고", value="unique_number"),
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
        app_commands.Choice(name="소수파 밸런스 (1차 위장)", value="minority_balance"),
        app_commands.Choice(name="몰리면 터진다!", value="defuse4"),
        app_commands.Choice(name="돌 세공 예측", value="cutting"),
        app_commands.Choice(name="상위 3명 견제", value="top3"),
        app_commands.Choice(name="홍피겔만의 황금금고", value="unique_number"),
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
    main_channel = await resolve_text_channel(main_channel_id)
    if main_channel is None:
        await interaction.followup.send(
            "이벤트를 시작한 메인 텍스트 채널을 찾을 수 없습니다.",
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
    extra: dict[str, Any] = {
        "auto_close_at": close_at,
        "duration_minutes": int(진행시간분),
    }
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

        if 게임 in {"balance", "minority_balance"}:
            if not 선택_a or not 선택_b:
                await interaction.followup.send(
                    "밸런스 게임은 `선택_a`와 `선택_b`가 필요합니다.",
                    ephemeral=True,
                )
                return

            if 게임 == "minority_balance":
                extra.update({"mode": "minority_balance", "balance_phase": "initial"})

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
                    main_channel_id,
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_balance_embed(round_id, 제목, 선택_a, 선택_b)
            view = vote_view(round_id, BALANCE_VOTE_CHOICES)
        elif 게임 == "defuse4":
            wire_map = {
                color["id"]: f"{color['emoji']} {color['name']}"
                for color in DEFUSE_COLORS
            }
            extra.update(
                {
                    "mode": "defuse4",
                    "wires": wire_map,
                    "settlement_rule": "top_vote_penalty",
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
                    main_channel_id,
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_defuse_embed(round_id, 제목)
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
                    main_channel_id,
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
                    main_channel_id,
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_sand_registration_embed(round_id, 제목, int(진행시간분))
            view = discord.utils.MISSING
        elif 게임 == "bail":
            # rounds.type CHECK 제약을 못 고쳐서 type은 sand를 재사용하고 mode로 갈라 쓴다.
            extra.update(
                {
                    "mode": "bail",
                    "bail_phase": "registration",
                    "registration_minutes": int(진행시간분),
                    "registration_close_at": close_at,
                    "negotiation_seconds": int(
                        협상시간분 or BAIL_DEFAULT_NEGOTIATION_MINUTES
                    ) * 60,
                    "execution_seconds": int(실행시간분 or BAIL_DEFAULT_EXECUTION_MINUTES) * 60,
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
                    main_channel_id,
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_bail_registration_embed(round_id, 제목, int(진행시간분))
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
                await interaction.followup.send(
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
                    main_channel_id,
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_top3_embed(round_id, 제목, candidates)
            view = top3_vote_view(round_id, candidates)
        elif 게임 == "unique_number":
            # BASE_SCHEMA의 불변 CHECK 제약에 남아 있는 레거시 grid 슬롯을 사용하고,
            # mode로 과거 제거된 grid 라운드와 새 게임을 명확히 구분한다.
            extra.update({"mode": "unique_number"})
            cursor = conn.execute(
                """
                INSERT INTO rounds
                    (type, title, question, status, channel_id, extra_json, created_at)
                VALUES ('grid', ?, ?, 'open', ?, ?, ?)
                """,
                (
                    제목,
                    제목,
                    main_channel_id,
                    json.dumps(extra, ensure_ascii=False),
                    now_text(),
                ),
            )
            round_id = int(cursor.lastrowid)
            embed = build_unique_number_embed(round_id, 제목)
            view = unique_number_vote_view(round_id)
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
                    main_channel_id,
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
    else:
        embed.add_field(
            name="자동 마감",
            value=f"{진행시간분}분 후 자동으로 종료됩니다.\n예정 시각: {close_time_text(close_at)}",
            inline=False,
        )
    message = await main_channel.send(embed=embed, view=view)

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
    await interaction.followup.send(
        f"게임 메시지를 <#{main_channel_id}>에 게시했습니다.",
        ephemeral=True,
    )


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

    if round_game_key(round_row) == "bail" and main_game_phase(round_row) in {
        "registration",
        "negotiation",
    }:
        # 짝마다 비공개 방을 여느라 API 호출이 여러 번이다. 3초 응답 제한을 넘기므로 먼저 defer한다.
        await interaction.response.defer(ephemeral=True)
        round_id = int(round_row["id"])
        registration = main_game_phase(round_row) == "registration"
        transition = (
            start_bail_negotiation(round_id) if registration else start_bail_execution(round_id)
        )
        if not transition:
            await interaction.followup.send("현재 단계를 마감하지 못했습니다.", ephemeral=True)
            return
        task = bot.auto_close_tasks.pop(round_id, None)
        if task:
            task.cancel()
        if transition["state"] == "cancelled":
            await update_trust_main_message(transition)
            await interaction.followup.send("참여자가 부족해 가라앉는 배를 취소했습니다.", ephemeral=True)
            return
        if registration:
            await create_bail_pair_threads(transition)
            await update_trust_main_message(transition)
            await interaction.followup.send(
                "참여자 모집을 마감하고 1:1 협상 단계를 시작했습니다.", ephemeral=True
            )
        else:
            await refresh_all_bail_pair_boards(round_id)
            await refresh_main_bail_board(round_id)
            await interaction.followup.send("협상을 마감하고 실행 단계를 시작했습니다.", ephemeral=True)
        bot.schedule_auto_close(round_id, float(transition["close_at"]))
        return

    round_extra_row = round_extra(round_row)
    if (
        round_row["type"] == "balance"
        and round_extra_row.get("mode") == "minority_balance"
        and round_extra_row.get("balance_phase") == "initial"
    ):
        await interaction.response.defer(ephemeral=True)
        transition = start_minority_balance_revote(int(round_row["id"]))
        if not transition:
            await interaction.followup.send("1차 집계를 마감하지 못했습니다.", ephemeral=True)
            return
        task = bot.auto_close_tasks.pop(int(round_row["id"]), None)
        if task:
            task.cancel()
        published = await publish_minority_balance_revote(transition)
        bot.schedule_auto_close(int(round_row["id"]), float(transition["close_at"]))
        if published:
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

    task = bot.auto_close_tasks.pop(int(round_row["id"]), None)
    if task:
        task.cancel()

    channel_id, embed = result
    if publishes_pair_results:
        if is_trust_decision:
            await publish_trust_pair_results(int(round_row["id"]))
        elif is_bail_execution:
            await publish_bail_pair_results(int(round_row["id"]))
        else:
            await publish_sand_pair_results(int(round_row["id"]))
    await archive_round_discussion_thread(round_row, reason="라운드 종료 후 영구 보관")

    channel = await resolve_text_channel(channel_id)
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


@bot.tree.command(name="이벤트종료", description="개발 중인 현재 이벤트를 남은 게임과 관계없이 강제로 종료합니다.")
@app_commands.default_permissions()
@admin_only()
async def force_end_event(interaction: discord.Interaction) -> None:
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
                    thread = bot.get_channel(pair_thread_id) or await bot.fetch_channel(pair_thread_id)
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
            channel = bot.get_channel(int(event_row["channel_id"])) or await bot.fetch_channel(
                int(event_row["channel_id"])
            )
            if hasattr(channel, "fetch_message"):
                message = await channel.fetch_message(int(event_row["message_id"]))
                await message.edit(view=None)
        except (discord.HTTPException, ValueError):
            pass

    if force_summary:
        channel_id, summary_embed = force_summary
        channel = await resolve_text_channel(channel_id)
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


@bot.tree.command(name="세공진행", description="가장 최근 마감된 돌 세공 예측의 관리자 진행 패널을 엽니다.")
@app_commands.default_permissions()
@admin_only()
async def run_cutting(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
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
            await interaction.followup.send("세공을 진행할 마감된 돌 세공 라운드가 없습니다.", ephemeral=True)
            return
        if round_row["status"] != "closed":
            await interaction.followup.send("먼저 투표 집계를 마감한 뒤 `/세공진행`을 실행해주세요.", ephemeral=True)
            return

        extra.setdefault("cutting_current_level", 0)
        extra.setdefault("cutting_logs", [])
        extra.setdefault("cutting_started_at", now_text())
        conn.execute(
            "UPDATE rounds SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), round_row["id"]),
        )

    embed = build_cutting_progress_embed(round_row, extra)
    channel_id = str(round_row["channel_id"])
    channel = await resolve_text_channel(channel_id)
    if channel is None:
        await interaction.followup.send("이벤트 메인 채널을 찾을 수 없습니다.", ephemeral=True)
        return
    await channel.send(embed=embed, view=cutting_action_view(int(round_row["id"])))
    await interaction.followup.send(
        f"세공 진행 패널을 <#{channel_id}>에 게시했습니다.",
        ephemeral=True,
    )


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
        phase = main_game_phase(round_row)
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


@bot.tree.command(name="러시안룰렛", description="한 플레이어에게 점수를 건 1:1 러시안룰렛을 신청합니다.")
@app_commands.describe(플레이어="결투를 신청할 플레이어")
async def russian_roulette(
    interaction: discord.Interaction, 플레이어: discord.Member
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "서버의 공개 텍스트 채널에서만 러시안룰렛을 시작할 수 있습니다.", ephemeral=True
        )
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message(
            "러시안룰렛은 일반 텍스트 채널에서만 시작할 수 있습니다.",
            ephemeral=True,
        )
        return
    if not interaction.channel.permissions_for(플레이어).view_channel:
        await interaction.response.send_message(
            "선택한 상대방이 이 채널을 볼 수 없어 결투 요청을 받을 수 없습니다. "
            "상대방이 접근할 수 있는 채널에서 실행해주세요.",
            ephemeral=True,
        )
        return
    if 플레이어.bot:
        await interaction.response.send_message("봇에게는 결투를 신청할 수 없습니다.", ephemeral=True)
        return
    if 플레이어.id == interaction.user.id:
        await interaction.response.send_message("자기 자신에게는 결투를 신청할 수 없습니다.", ephemeral=True)
        return

    challenger_id = str(interaction.user.id)
    target_id = str(플레이어.id)
    with db() as conn:
        ensure_user(conn, interaction.user)
        ensure_user(conn, 플레이어)
        expire_stale_roulette_games(conn)

        if active_roulette_game_for_user(conn, challenger_id):
            await interaction.response.send_message(
                "이미 진행 중이거나 응답을 기다리는 러시안룰렛이 있습니다.", ephemeral=True
            )
            return
        target_active = active_roulette_game_for_user(conn, target_id)
        if target_active is not None:
            await interaction.response.send_message(
                "상대방이 이미 다른 러시안룰렛에 참여 중입니다.", ephemeral=True
            )
            return

        score_rows = conn.execute(
            "SELECT user_id, total_score FROM users WHERE user_id IN (?, ?)",
            (challenger_id, target_id),
        ).fetchall()
        scores = {str(row["user_id"]): int(row["total_score"]) for row in score_rows}
        challenger_score = scores.get(challenger_id, 0)
        target_score = scores.get(target_id, 0)
        if is_roulette_credit_user(challenger_score):
            await interaction.response.send_message(
                f"러시안룰렛을 신청하려면 최소 {ROULETTE_MIN_BET}점이 필요합니다. "
                f"(현재 {challenger_score}점)\n"
                "점수가 부족해도 다른 사람의 신청을 **수락**할 수는 있습니다.",
                ephemeral=True,
            )
            return

        # 상대의 잔여 횟수는 여기서 보지 않는다. 신청 단계에서 걸러주면 그 실패가
        # 상대의 오늘 활동을 알려주는 무료 조회가 된다. 수락 시점에 상대 본인이 확인한다.
        if count_roulette_games_today(conn, challenger_id) >= ROULETTE_DAILY_LIMIT:
            await interaction.response.send_message(
                f"오늘 러시안룰렛을 {ROULETTE_DAILY_LIMIT}번 모두 사용했습니다. "
                "내일 다시 도전해주세요.",
                ephemeral=True,
            )
            return
        if (
            count_roulette_games_with_opponent_today(conn, challenger_id, target_id)
            >= ROULETTE_PAIR_DAILY_LIMIT
        ):
            await interaction.response.send_message(
                "오늘 이 상대와는 이미 겨뤘습니다. 같은 상대와는 하루 "
                f"{ROULETTE_PAIR_DAILY_LIMIT}번까지입니다.",
                ephemeral=True,
            )
            return

        cursor = conn.execute(
            """
            INSERT INTO russian_roulette_games (
                guild_id, channel_id, challenger_id, target_id,
                challenger_name, target_name, status, created_at, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                str(interaction.guild.id),
                str(interaction.channel.id),
                challenger_id,
                target_id,
                getattr(interaction.user, "display_name", interaction.user.name),
                플레이어.display_name,
                now_text(),
                time.time() + ROULETTE_CHALLENGE_SECONDS,
            ),
        )
        game_id = int(cursor.lastrowid)
        log_activity(
            conn,
            "roulette",
            "challenge",
            actor_user_id=challenger_id,
            target_user_id=target_id,
            payload={
                "game_id": game_id,
                "challenger_score": challenger_score,
                "target_score": target_score,
            },
        )
        game = dict(
            conn.execute(
                "SELECT * FROM russian_roulette_games WHERE id = ?", (game_id,)
            ).fetchone()
        )

    try:
        await interaction.response.send_message(
            content=f"<@{target_id}>님, 러시안룰렛 결투 요청이 도착했습니다.",
            embed=build_roulette_embed(game),
            view=roulette_view(game),
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True, replied_user=False
            ),
        )
        message = await interaction.original_response()
    except discord.HTTPException:
        with db() as conn:
            conn.execute(
                "UPDATE russian_roulette_games SET status = 'expired', finished_at = ? WHERE id = ?",
                (now_text(), game_id),
            )
        raise

    with db() as conn:
        conn.execute(
            "UPDATE russian_roulette_games SET message_id = ? WHERE id = ?",
            (str(message.id), game_id),
        )


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


async def send_ranking(
    interaction: discord.Interaction,
    rank: int | None,
    *,
    reveal_users: bool,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버 안에서만 랭킹을 공개할 수 있습니다.", ephemeral=True)
        return

    await interaction.response.defer()

    current_bot_id = str(interaction.client.user.id) if interaction.client.user else ""
    with db() as conn:
        candidates = fetch_ranking_rows(conn, current_bot_id)

    requested_count = rank if rank is not None else 10
    rows = []
    try:
        for candidate in candidates:
            if await current_guild_member(interaction.guild, str(candidate["user_id"])) is None:
                continue
            rows.append(candidate)
            if len(rows) == requested_count:
                break
    except discord.HTTPException:
        await interaction.followup.send(
            "현재 서버 멤버를 확인하지 못했습니다. 잠시 후 다시 시도해주세요.",
            ephemeral=True,
        )
        return

    if not rows:
        await interaction.followup.send(
            "현재 서버에 게임 참여 기록이 있는 유저가 없습니다.",
            ephemeral=True,
        )
        return

    if rank is not None and len(rows) < rank:
        await interaction.followup.send(
            f"현재 서버 랭킹에는 {rank}등이 없습니다. 참가자는 {len(rows)}명입니다.",
            ephemeral=True,
        )
        return

    ranking_label = "랭킹" if reveal_users else "익명 랭킹"
    formatter = format_public_ranking_lines if reveal_users else format_anonymous_ranking_lines
    if rank is None:
        title = f"현재 {ranking_label} TOP 10"
        ranking_lines = formatter(rows)
    else:
        title = f"현재 {ranking_label} {rank}등"
        ranking_lines = formatter([rows[rank - 1]], start_rank=rank)

    embed = discord.Embed(
        title=title,
        description="\n".join(ranking_lines),
        color=discord.Color.gold(),
    )
    await interaction.followup.send(
        embed=embed,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="랭킹", description="이름이 표시되는 TOP 10 또는 특정 등수를 공개합니다.")
@app_commands.default_permissions()
@admin_only()
@app_commands.describe(등수="이 등수만 공개합니다. 생략하면 TOP 10을 공개합니다.")
async def show_ranking(
    interaction: discord.Interaction,
    등수: app_commands.Range[int, 1, 1000] | None = None,
) -> None:
    await send_ranking(interaction, 등수, reveal_users=True)


@bot.tree.command(name="익명랭킹", description="이름을 숨긴 TOP 10 또는 특정 등수를 공개합니다.")
@app_commands.default_permissions()
@admin_only()
@app_commands.describe(등수="이 등수만 익명으로 공개합니다. 생략하면 TOP 10을 공개합니다.")
async def show_anonymous_ranking(
    interaction: discord.Interaction,
    등수: app_commands.Range[int, 1, 1000] | None = None,
) -> None:
    await send_ranking(interaction, 등수, reveal_users=False)


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
