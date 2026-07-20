"""YUMCBOT 활동 로그 조회기.

activity_logs(+score_logs, event_sessions, rounds)를 읽어 사람이 읽기 좋은 표로 출력한다.
DB는 읽기 전용으로만 연다 — 봇이 돌고 있는 중에 실행해도 안전하다.

사용:
    python yumcbot_log.py summary <이름|ID>
    python yumcbot_log.py detail  <이름|ID> [--date 2026-07-21]
    python yumcbot_log.py scores  <이름|ID> [--date 2026-07-21]
    python yumcbot_log.py event   <이벤트ID>
    python yumcbot_log.py events  [--user <이름|ID>]
    python yumcbot_log.py whois   <이름>
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ─────────────────────────── 표시용 라벨 ───────────────────────────

ACTION_LABELS = {
    "event:open": "이벤트 오픈",
    "event:start": "이벤트 시작",
    "event:close": "이벤트 종료",
    "participation:join": "참여 등록",
    "participation:cancel": "참여 취소",
    "round:create": "라운드 생성",
    "match:pair_matched": "1:1 매칭",
    "match:pairing_complete": "매칭 완료",
    "match:emergency_join": "긴급 투입",
    "vote:cast": "투표",
    "vote:trust_decision": "협상 선택",
    "vote:auto_assigned": "자동 배정",
    "score:round_settled": "라운드 정산",
    "thread:anon_post": "익명 발언",
    "roulette:challenge": "룰렛 신청",
    "roulette:accept": "룰렛 수락",
    "roulette:decline": "룰렛 거절",
    "roulette:bet": "룰렛 베팅",
    "roulette:start": "룰렛 시작",
    "roulette:shot": "룰렛 격발",
}

GAME_LABELS = {
    "trust": "딜레마의 섬",
    "sand": "모래성",
    "balance": "밸런스",
    "minority_balance": "소수파 밸런스",
    "defuse4": "3선 폭탄",
    "bomb": "3선 폭탄",
    "top3": "상위3 견제",
    "cutting": "돌세공",
    "unique_number": "유일 숫자",
    "grid": "유일 숫자",
}

CHOICE_LABELS = {"trust": "신뢰", "betray": "배신"}


def label_for(category: str, action: str) -> str:
    return ACTION_LABELS.get(f"{category}:{action}", f"{category}:{action}")


# ─────────────────────────── 폭 계산 (한글 정렬) ───────────────────────────


def dwidth(text: str) -> int:
    """한글·이모지는 터미널에서 2칸을 먹는다. 그걸 반영한 표시 폭."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(text))


def pad(text: str, width: int, align: str = "<") -> str:
    text = str(text)
    gap = max(0, width - dwidth(text))
    if align == ">":
        return " " * gap + text
    if align == "^":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def clip(text: str, width: int) -> str:
    text = str(text)
    if dwidth(text) <= width:
        return text
    out = ""
    for ch in text:
        if dwidth(out) + dwidth(ch) > width - 1:
            return out + "…"
        out += ch
    return out


def table(headers: list[str], rows: list[list], aligns: str | None = None) -> str:
    if not rows:
        return "  (없음)"
    cols = len(headers)
    aligns = aligns or "<" * cols
    widths = [dwidth(h) for h in headers]
    for row in rows:
        for i in range(cols):
            widths[i] = max(widths[i], dwidth(row[i]))
    line = "─┼─".join("─" * w for w in widths)
    out = [
        " │ ".join(pad(headers[i], widths[i], "^") for i in range(cols)),
        line,
    ]
    for row in rows:
        out.append(" │ ".join(pad(row[i], widths[i], aligns[i]) for i in range(cols)))
    return "\n".join("  " + item for item in out)


def rule(title: str = "", char: str = "━", width: int = 76) -> str:
    if not title:
        return char * width
    return f"{char * 3} {title} " + char * max(0, width - dwidth(title) - 5)


# ─────────────────────────── DB 접근 ───────────────────────────


def discover_db(explicit: str | None = None) -> Path:
    """DB 경로를 순서대로 찾는다. 못 찾으면 뭘 어떻게 하면 되는지 알려주고 종료."""
    # --db로 명시했으면 그것만 쓴다. 없으면 에러 — 조용히 다른 DB를 읽으면
    # 엉뚱한 데이터를 보고도 맞는 줄 알게 된다.
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            sys.exit(f"지정한 DB 파일이 없습니다: {path}")
        return path

    candidates: list[Path] = []
    env = os.getenv("YUMCBOT_LOG_DB")
    if env:
        candidates.append(Path(env))

    here = Path(__file__).resolve()
    for base in [Path.cwd(), *here.parents]:
        candidates.append(base / "data" / "event_game.sqlite3")

    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        candidates.extend(sorted(downloads.glob("*.sqlite3"), key=lambda p: -p.stat().st_mtime))

    seen: set[Path] = set()
    for path in candidates:
        path = path.expanduser()
        if path in seen:
            continue
        seen.add(path)
        if path.is_file():
            return path

    sys.exit(
        "DB 파일을 찾지 못했습니다.\n"
        "  다음 중 하나를 하세요:\n"
        "   1) 환경변수 지정:  $env:YUMCBOT_LOG_DB = 'C:\\경로\\event_game.sqlite3'\n"
        "   2) --db 옵션으로 직접 전달\n"
        "   3) 디스코드 /데이터백업 으로 받은 파일을 ~/Downloads 에 두기\n"
        "   4) 저장소의 data/event_game.sqlite3 (dev 봇을 한 번 돌리면 생깁니다)"
    )


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "activity_logs" not in tables:
        sys.exit(
            f"이 DB에는 activity_logs 테이블이 없습니다: {path}\n"
            "  스키마 v5 이전의 DB입니다. 봇을 한 번 실행하면 마이그레이션이 적용됩니다."
        )
    return conn


# ─────────────────────────── 유저 조회 ───────────────────────────


def find_users(conn: sqlite3.Connection, needle: str) -> list[sqlite3.Row]:
    """ID 정확일치를 먼저 보고, 없으면 이름 부분일치."""
    exact = conn.execute(
        "SELECT user_id, display_name, total_score FROM users WHERE user_id = ?", (needle,)
    ).fetchall()
    if exact:
        return exact
    return conn.execute(
        """
        SELECT u.user_id, u.display_name, u.total_score,
               (SELECT MAX(created_at) FROM activity_logs
                 WHERE actor_user_id = u.user_id OR target_user_id = u.user_id) AS last_seen
        FROM users u
        WHERE u.display_name LIKE ?
        ORDER BY u.total_score DESC
        """,
        (f"%{needle}%",),
    ).fetchall()


def resolve_user(conn: sqlite3.Connection, needle: str) -> sqlite3.Row:
    matches = find_users(conn, needle)
    if not matches:
        sys.exit(f"'{needle}' 에 해당하는 유저를 찾지 못했습니다. (whois 로 검색해보세요)")
    if len(matches) == 1:
        return matches[0]

    print(f"'{needle}' 후보가 {len(matches)}명입니다. ID로 다시 지정해주세요.\n")
    print(print_candidates(matches))
    sys.exit(0)


def print_candidates(matches: list[sqlite3.Row]) -> str:
    rows = []
    for user in matches:
        last = user["last_seen"] if "last_seen" in user.keys() else None
        rows.append(
            [user["user_id"], user["display_name"], f"{user['total_score']}점", (last or "-")[:10]]
        )
    return table(["ID", "이름", "총점", "최근활동"], rows, "<<>>")


# ─────────────────────────── 공통 조회 ───────────────────────────


def user_logs(conn, user_id: str, date: str | None = None) -> list[sqlite3.Row]:
    query = """
        SELECT * FROM activity_logs
        WHERE (actor_user_id = :uid OR target_user_id = :uid)
    """
    params: dict = {"uid": user_id}
    if date:
        query += " AND date(created_at) = :date"
        params["date"] = date
    return conn.execute(query + " ORDER BY id", params).fetchall()


def user_scores(conn, user_id: str, date: str | None = None) -> list[sqlite3.Row]:
    query = "SELECT * FROM score_logs WHERE user_id = :uid"
    params: dict = {"uid": user_id}
    if date:
        query += " AND date(created_at) = :date"
        params["date"] = date
    return conn.execute(query + " ORDER BY id", params).fetchall()


def name_map(conn) -> dict[str, str]:
    return {
        row["user_id"]: row["display_name"]
        for row in conn.execute("SELECT user_id, display_name FROM users")
    }


def who(names: dict[str, str], user_id: str | None) -> str:
    if not user_id:
        return "-"
    return f"{names.get(user_id, '?')}({user_id})"


def payload_of(row: sqlite3.Row) -> dict:
    try:
        return json.loads(row["payload_json"]) or {}
    except (json.JSONDecodeError, TypeError):
        return {}


def describe(row: sqlite3.Row, names: dict[str, str]) -> str:
    """행 하나를 한 줄 설명으로. 표에서 '내용' 칸에 들어간다."""
    p = payload_of(row)
    key = f"{row['category']}:{row['action']}"
    game = GAME_LABELS.get(str(p.get("game_key")), p.get("game_key"))

    if key in ("vote:cast", "vote:trust_decision"):
        choice = CHOICE_LABELS.get(str(p.get("choice")), p.get("choice"))
        prev = p.get("previous_choice")
        if prev:
            prev = CHOICE_LABELS.get(str(prev), prev)
            return f"{game or ''} {prev} → {choice}  ⟵ 변경".strip()
        return f"{game or ''} {choice} 선택".strip()
    if key == "vote:auto_assigned":
        choice = CHOICE_LABELS.get(str(p.get("choice")), p.get("choice"))
        return f"미선택 → {choice} 자동배정"
    if key == "roulette:shot":
        if p.get("result") == "fired":
            return (
                f"{p.get('shot_index')}번째 발사 · 승 {who(names, p.get('winner_id'))}"
                f" / 패 {who(names, p.get('loser_id'))} · {p.get('pot')}점"
            )
        return f"{p.get('shot_index')}번째 빈 탄"
    if key == "roulette:bet":
        return f"{p.get('amount')}점 베팅 (보유 {p.get('balance')}점)"
    if key == "roulette:start":
        bets = p.get("bets") or {}
        return f"판돈 {p.get('pot')}점 · 베팅 {'/'.join(str(v) for v in bets.values())}"
    if key == "roulette:challenge":
        return f"신청 (내 {p.get('challenger_score')}점 vs 상대 {p.get('target_score')}점)"
    if key == "round:create":
        return f"{p.get('game_name') or game} · '{p.get('title')}' ({p.get('duration_minutes')}분)"
    if key == "score:round_settled":
        dist = p.get("vote_distribution") or {}
        dist_text = ", ".join(f"{k}:{v}" for k, v in dist.items())
        return f"{game} · 투표 {p.get('vote_count')}표 ({dist_text}) · 합계 {p.get('total_delta'):+d}점"
    if key == "event:start":
        return f"참가 {p.get('participant_count')}명 · 첫 게임 {GAME_LABELS.get(str(p.get('first_game')), p.get('first_game'))}"
    if key == "event:open":
        return f"참가 모집 시작 (봇 {p.get('bot_version', '?')})"
    if key == "event:close":
        reason = "정상 완료" if p.get("reason") == "completed" else "강제 마감"
        return f"{reason} · 참가 {p.get('participant_count') or '-'}명"
    if key in ("participation:join", "participation:cancel"):
        return f"현재 {p.get('participant_count')}명"
    if key == "match:pair_matched":
        return f"{game} 짝 배정"
    if key == "match:pairing_complete":
        return f"{game} {p.get('pair_count')}쌍 · 등록 {p.get('registered_count')}명"
    if key == "match:emergency_join":
        return f"{game} 긴급 투입 (+{p.get('extension_seconds')}초)"
    if key == "thread:anon_post":
        return f"익명 {p.get('anon_number')}번 · {p.get('content_length')}자"
    return json.dumps(p, ensure_ascii=False)[:60] if p else ""


def scope_of(row: sqlite3.Row) -> str:
    parts = []
    if row["event_session_id"]:
        parts.append(f"E{row['event_session_id']}")
    if row["round_id"]:
        parts.append(f"R{row['round_id']}")
    return "/".join(parts) or "-"


# ─────────────────────────── 명령: summary ───────────────────────────


def cmd_summary(conn, args) -> None:
    user = resolve_user(conn, args.user)
    uid = user["user_id"]
    names = name_map(conn)
    logs = user_logs(conn, uid)
    scores = user_scores(conn, uid)

    print(rule())
    print(f"  {user['display_name']} ({uid})   ·   현재 보유 {user['total_score']}점")
    print(rule())

    if not logs and not scores:
        print("  기록된 활동이 없습니다.")
        return

    days = sorted({row["created_at"][:10] for row in logs})
    events = sorted({row["event_session_id"] for row in logs if row["event_session_id"]})
    rounds = sorted({row["round_id"] for row in logs if row["round_id"]})

    # 룰렛 전적
    wins = losses = 0
    for row in logs:
        p = payload_of(row)
        if row["category"] == "roulette" and row["action"] == "shot" and p.get("result") == "fired":
            if p.get("winner_id") == uid:
                wins += 1
            elif p.get("loser_id") == uid:
                losses += 1

    changed = sum(1 for row in logs if payload_of(row).get("changed"))
    gained = sum(row["delta"] for row in scores if row["delta"] > 0)
    lost = sum(row["delta"] for row in scores if row["delta"] < 0)

    info = [
        ["활동 기간", f"{days[0]} ~ {days[-1]}  ({len(days)}일)" if days else "-"],
        ["참여 이벤트", f"{len(events)}회  {'· '.join('E' + str(e) for e in events[-5:])}"],
        ["플레이 라운드", f"{len(rounds)}개"],
        ["룰렛 전적", f"{wins}승 {losses}패" if (wins or losses) else "없음"],
        ["선택 변경", f"{changed}회"],
        ["점수", f"획득 +{gained} / 손실 {lost} / 합계 {gained + lost:+d}"],
        ["활동 로그", f"{len(logs)}건"],
    ]
    print()
    print(table(["항목", "값"], info))

    # 일자별
    by_day: dict[str, list] = defaultdict(list)
    for row in logs:
        by_day[row["created_at"][:10]].append(row)
    day_score: dict[str, int] = defaultdict(int)
    for row in scores:
        day_score[row["created_at"][:10]] += row["delta"]

    rows = []
    for day in sorted(by_day, reverse=True)[:14]:
        entries = by_day[day]
        games = sorted(
            {
                GAME_LABELS.get(str(payload_of(item).get("game_key")), "")
                for item in entries
                if payload_of(item).get("game_key")
            }
            - {""}
        )
        evs = sorted({item["event_session_id"] for item in entries if item["event_session_id"]})
        rows.append(
            [
                day,
                f"{len(entries)}건",
                f"{day_score.get(day, 0):+d}점",
                ", ".join("E" + str(e) for e in evs) or "-",
                clip(", ".join(games) or "-", 28),
            ]
        )
    print()
    print(rule("일자별", "─"))
    print(table(["날짜", "로그", "점수", "이벤트", "게임"], rows, "<>><<"))

    print()
    print(rule("최근 활동 5건", "─"))
    rows = [
        [
            row["created_at"][5:16],
            label_for(row["category"], row["action"]),
            scope_of(row),
            clip(describe(row, names), 46),
        ]
        for row in logs[-5:]
    ]
    print(table(["시각", "행동", "범위", "내용"], rows))

    print()
    print("  더 보기:")
    print(f"    상세 로그   detail {uid}" + (f" --date {days[-1]}" if days else ""))
    print(f"    점수 변동   scores {uid}")
    if events:
        print(f"    이벤트 전체 event {events[-1]}")


# ─────────────────────────── 명령: detail ───────────────────────────


def cmd_detail(conn, args) -> None:
    user = resolve_user(conn, args.user)
    uid = user["user_id"]
    names = name_map(conn)
    logs = user_logs(conn, uid, args.date)

    scope_text = f"{args.date} " if args.date else "전체 기간 "
    print(rule())
    print(f"  {user['display_name']} ({uid})  ·  {scope_text}상세 로그  ·  {len(logs)}건")
    print(rule())

    if not logs:
        print("  해당 기간에 기록이 없습니다.")
        return

    by_day: dict[str, list] = defaultdict(list)
    for row in logs:
        by_day[row["created_at"][:10]].append(row)

    for day in sorted(by_day):
        entries = by_day[day]
        print()
        print(rule(f"{day}  ({len(entries)}건)", "─"))
        rows = [
            [
                row["created_at"][11:19],
                label_for(row["category"], row["action"]),
                scope_of(row),
                who(names, row["target_user_id"]) if row["target_user_id"] else "-",
                describe(row, names),
            ]
            for row in entries
        ]
        print(table(["시각", "행동", "범위", "상대", "내용"], rows))

    if args.raw:
        print()
        print(rule("원본 payload", "─"))
        for row in logs:
            print(f"  [{row['id']}] {row['created_at']} {row['category']}:{row['action']}")
            print(f"      {row['payload_json']}")

    scores = user_scores(conn, uid, args.date)
    if scores:
        print()
        print(rule("점수 변동", "─"))
        print(score_table(scores))


# ─────────────────────────── 명령: scores ───────────────────────────


def score_table(scores: list[sqlite3.Row]) -> str:
    rows = [
        [
            row["created_at"][5:19],
            f"R{row['round_id']}" if row["round_id"] else "-",
            f"{row['delta']:+d}",
            row["reason"],
        ]
        for row in scores
    ]
    total = sum(row["delta"] for row in scores)
    rows.append(["", "", f"{total:+d}", "합계"])
    return table(["시각", "라운드", "점수", "사유"], rows, "<<><")


def cmd_scores(conn, args) -> None:
    user = resolve_user(conn, args.user)
    scores = user_scores(conn, user["user_id"], args.date)

    print(rule())
    print(
        f"  {user['display_name']} ({user['user_id']})  ·  점수 변동"
        f"  ·  현재 보유 {user['total_score']}점"
    )
    print(rule())
    if not scores:
        print("  기록이 없습니다.")
        return
    print()
    print(score_table(scores))


# ─────────────────────────── 명령: event ───────────────────────────


def cmd_event(conn, args) -> None:
    names = name_map(conn)
    ev = conn.execute("SELECT * FROM event_sessions WHERE id = ?", (args.event_id,)).fetchone()
    if not ev:
        sys.exit(f"이벤트 {args.event_id} 를 찾지 못했습니다.")

    logs = conn.execute(
        "SELECT * FROM activity_logs WHERE event_session_id = ? ORDER BY id", (args.event_id,)
    ).fetchall()
    rounds = conn.execute(
        "SELECT * FROM rounds WHERE event_session_id = ? ORDER BY id", (args.event_id,)
    ).fetchall()
    parts = conn.execute(
        "SELECT user_id FROM event_participants WHERE event_session_id = ?", (args.event_id,)
    ).fetchall()

    print(rule())
    print(f"  이벤트 E{args.event_id}  ·  {ev['status']}  ·  {ev['created_at']}")
    print(rule())

    info = [
        ["상태", ev["status"]],
        ["시작", ev["created_at"]],
        ["종료", ev["closed_at"] or "-"],
        ["현재 참가자", f"{len(parts)}명"],
        ["라운드", " → ".join(f"R{r['id']} {GAME_LABELS.get(game_key(r), r['type'])}" for r in rounds) or "-"],
        ["활동 로그", f"{len(logs)}건"],
    ]
    print()
    print(table(["항목", "값"], info))

    if rounds:
        print()
        print(rule("라운드", "─"))
        rows = []
        for r in rounds:
            settled = conn.execute(
                "SELECT COALESCE(SUM(delta),0) AS total, COUNT(DISTINCT user_id) AS people"
                " FROM score_logs WHERE round_id = ?",
                (r["id"],),
            ).fetchone()
            rows.append(
                [
                    f"R{r['id']}",
                    GAME_LABELS.get(game_key(r), r["type"]),
                    clip(r["question"] or r["title"], 26),
                    r["status"],
                    f"{settled['people']}명",
                    f"{settled['total']:+d}",
                ]
            )
        print(table(["ID", "게임", "제목", "상태", "정산", "합계"], rows, "<<<<>>"))

    print()
    print(rule("전체 타임라인", "─"))
    rows = [
        [
            row["created_at"][5:19],
            label_for(row["category"], row["action"]),
            scope_of(row),
            who(names, row["actor_user_id"]),
            clip(describe(row, names), 42),
        ]
        for row in logs
    ]
    print(table(["시각", "행동", "범위", "주체", "내용"], rows))


def game_key(round_row: sqlite3.Row) -> str:
    try:
        extra = json.loads(round_row["extra_json"] or "{}")
    except json.JSONDecodeError:
        extra = {}
    return str(extra.get("mode") or round_row["type"])


# ─────────────────────────── 명령: events / whois ───────────────────────────


def cmd_events(conn, args) -> None:
    where, params = "", {}
    if args.user:
        user = resolve_user(conn, args.user)
        where = """
            WHERE e.id IN (SELECT event_session_id FROM event_participants WHERE user_id = :uid)
        """
        params["uid"] = user["user_id"]
        print(f"  {user['display_name']}({user['user_id']}) 가 참여한 이벤트\n")

    rows = conn.execute(
        f"""
        SELECT e.id, e.status, e.created_at, e.closed_at,
               (SELECT COUNT(*) FROM event_participants p WHERE p.event_session_id = e.id) AS people,
               (SELECT COUNT(*) FROM rounds r WHERE r.event_session_id = e.id) AS rounds
        FROM event_sessions e {where}
        ORDER BY e.id DESC LIMIT 30
        """,
        params,
    ).fetchall()

    print(rule())
    print(f"  이벤트 목록  ·  {len(rows)}건")
    print(rule())
    print()
    print(
        table(
            ["ID", "상태", "시작", "종료", "인원", "라운드"],
            [
                [
                    f"E{r['id']}",
                    r["status"],
                    r["created_at"][:16],
                    (r["closed_at"] or "-")[:16],
                    f"{r['people']}명",
                    f"{r['rounds']}개",
                ]
                for r in rows
            ],
            "<<<<>>",
        )
    )


def cmd_whois(conn, args) -> None:
    matches = find_users(conn, args.user)
    if not matches:
        sys.exit(f"'{args.user}' 검색 결과가 없습니다.")
    print(f"  '{args.user}' 검색 결과 {len(matches)}명\n")
    print(print_candidates(matches))


# ─────────────────────────── 엔트리 ───────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="YUMCBOT 활동 로그 조회")
    parser.add_argument("--db", help="DB 경로 (미지정 시 자동 탐지)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("summary", help="유저 요약")
    p.add_argument("user")

    p = sub.add_parser("detail", help="유저 상세 로그")
    p.add_argument("user")
    p.add_argument("--date", help="YYYY-MM-DD")
    p.add_argument("--raw", action="store_true", help="원본 payload도 출력")

    p = sub.add_parser("scores", help="점수 변동")
    p.add_argument("user")
    p.add_argument("--date", help="YYYY-MM-DD")

    p = sub.add_parser("event", help="이벤트 전체 타임라인")
    p.add_argument("event_id", type=int)

    p = sub.add_parser("events", help="이벤트 목록")
    p.add_argument("--user", help="이 유저가 참여한 것만")

    p = sub.add_parser("whois", help="이름으로 유저 검색")
    p.add_argument("user")

    args = parser.parse_args()
    db_path = discover_db(args.db)
    conn = connect(db_path)
    try:
        {
            "summary": cmd_summary,
            "detail": cmd_detail,
            "scores": cmd_scores,
            "event": cmd_event,
            "events": cmd_events,
            "whois": cmd_whois,
        }[args.cmd](conn, args)
        print(f"\n  DB: {db_path}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
