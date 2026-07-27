"""SQLite 연결과 스키마 관리.

상태를 전부 DB에 두는 것이 이 봇의 재시작 안전성 전체를 떠받친다(AGENTS.md 참고).
그래서 스키마 규칙이 엄격하다:

  1. BASE_SCHEMA(v1)는 절대 수정하지 않는다. tests/test_schema.py가 SHA256으로 고정한다.
  2. 스키마 추가는 MIGRATIONS에 다음 번호로. 반드시 '추가' 형태(ADD COLUMN, CREATE TABLE)로만.
  3. verify_schema()가 부팅 시점에 누락을 잡는다. 못 잡으면 봇이 로그인까지 성공해놓고
     (=배포 헬스체크 통과) 게임 도중에 'no such column'으로 터진다.

DB 경로를 config.DB_PATH로 **매번 조회**하는 것은 의도적이다. 모듈 로드 시점에
`from .config import DB_PATH`로 값을 복사하면 테스트가 경로를 갈아끼울 수 없어
실제 운영 DB를 건드리게 된다 (tests/support.py의 patch 지점이 여기에 맞춰져 있다).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from . import config


@contextmanager
def db() -> sqlite3.Connection:
    db_path = config.DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0)
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
    # 야구는 한 라운드에서 3번의 선택 이력을 모두 보존해야 하므로, (round_id, user_id)를
    # 덮어쓰는 votes 대신 투구 번호까지 PK에 포함한 전용 append 형태 테이블을 사용한다.
    7: [
        """
        CREATE TABLE IF NOT EXISTS baseball_swings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id INTEGER NOT NULL,
            pitch_number INTEGER NOT NULL CHECK (pitch_number BETWEEN 1 AND 3),
            user_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            swung_at REAL NOT NULL,
            elapsed_seconds REAL NOT NULL,
            rank INTEGER,
            score INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE (round_id, pitch_number, user_id),
            FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_baseball_pitch_order
        ON baseball_swings(round_id, pitch_number, id)
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
