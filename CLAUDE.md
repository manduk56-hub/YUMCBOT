# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

방학 이벤트용 Discord 봇. 이벤트마다 **메인게임 1개 + 미니게임 1개**를 진행하고 점수를 SQLite에 누적한다.
운영 봇은 Rocky Linux 서버에서 systemd로 24시간 돌고, `main` 머지 시 GitHub Actions가 자동 배포한다.

## 명령어

파이썬 **3.12** (운영 서버와 버전을 맞춘다).

```powershell
.\run.ps1                                   # 봇 실행 (.venv 없으면 자동 생성 + 의존성 설치)

python -m unittest discover -s tests -v     # 전체 테스트
python -m unittest tests.test_bot -v        # 파일 단위
python -m unittest tests.test_bot.BotRegressionTests.test_sand_registration_transitions_to_playing   # 단일 테스트

python -m compileall -q bot.py deploy/check_idle.py   # CI가 도는 문법 검사
shellcheck deploy/*.sh                                # CI가 도는 배포 스크립트 검사
```

테스트는 `bot.DB_PATH`를 임시 디렉터리로 patch해서 돈다. 디스코드 연결 없이 순수 함수와 DB 로직만 검증한다.

## 개발자마다 전용 Discord 앱이 필요하다

같은 봇 토큰으로 두 프로세스를 띄우면 게이트웨이 세션이 둘 붙어서 **모든 버튼/명령이 두 번씩 처리된다**
(점수 2배 지급, 라운드 두 번 정산). 길드가 달라도 마찬가지다. 운영 봇 토큰을 로컬 `.env`에 넣지 말 것.

`.env`: `DISCORD_TOKEN`(본인 dev 봇), `GUILD_ID`(있으면 슬래시 명령어 즉시 동기화), `OPERATOR_ROLE_IDS`, `YUMCBOT_DATA_DIR`(선택).

## 아키텍처

전체 로직이 `bot.py` 한 파일(~4200줄)에 있다. 대략적인 구역 순서:

상수/게임 설정 → 스키마·마이그레이션 → DB 헬퍼 → 게임별 상태 전이 함수 → `EventBot` 클래스 →
embed/view 빌더 → `close_round_record`(정산) → 슬래시 명령어 → systemd 연동/엔트리포인트.

### 상태는 전부 SQLite에 있다 (재시작 안전성)

배포는 봇을 3~5초 재시작한다. 이게 안전한 이유가 곧 코드의 핵심 제약이다:

- **버튼은 `discord.ui.View` 콜백이 아니라 `EventBot.on_interaction`에서 `custom_id`를 파싱해 처리한다.**
  `custom_id` 형식은 `eventbot:<action>:<round_id>[:<choice>]`. 덕분에 이미 채널에 뿌려진 버튼이
  재시작 후에도 그대로 동작한다. **새 버튼을 추가할 때 View 콜백(`@discord.ui.button`)에 로직을 넣지 말 것.**
  뷰 빌더는 `custom_id`만 붙이고, 핸들러는 `on_interaction`의 디스패치에 등록한다.
- 라운드 상태를 메모리에 들고 있지 않는다. 게임별 진행 상태는 `rounds.extra_json`(JSON)에 넣는다
  (`trust_phase`, `sand_phase`, `auto_close_at`, `planned_mini_game` 등).
- 자동 마감 타이머는 부팅 시 `EventBot.restore_auto_close_tasks()`가 `extra_json.auto_close_at`으로 DB에서 복구한다.

### 이벤트/라운드 모델

`event_sessions`(`recruiting` → `active` → `closed`)가 상위, `rounds`가 그 아래 게임 하나.
`/오픈`으로 세션을 열고 `event_participants`에 참가자를 모은다. `can_join_round()`가
**`참여하기`를 누른 유저만** 투표/토론할 수 있게 막는다. `/start` 시점에 명단이 마감된다.

정산은 전부 `close_round_record()`의 타입별 분기에서 이뤄지고 `add_score()`가 `users.total_score`와
`score_logs`를 함께 갱신한다.

### 스레드를 잠그지 말 것 (버튼이 죽는다)

**`archived` 또는 `locked` 상태의 스레드에서는 `스레드 관리` 권한이 없는 유저의 버튼 클릭을 디스코드가 막는다.**
익명 토론방을 잠가두었더니 "관리자만 익명 발언이 된다 / 스레드 안 버튼이 안 눌린다"는 버그가 났었다.
스레드는 채널 권한을 상속해서 개별 권한 덮어쓰기가 불가능하므로, 일반 채팅 차단은 **`on_message`의 메시지 삭제로만** 한다.
잠그는 건 라운드가 끝나 버튼이 더 필요 없을 때뿐이다 (`publish_trust_pair_results` / `publish_sand_pair_results`).

진행 중에 자동 보관돼도 같은 문제가 나므로 스레드 생성 시 `auto_archive_duration=1440`을 명시한다.

### 스레드 시작 메시지의 버튼은 눌리지 않는다

토론 스레드는 라운드 메시지에서 파생시키므로(`message.create_thread`) 그 메시지가 스레드의 **시작 메시지**가 된다.
시작 메시지의 실체는 부모 채널에 있어서, 스레드 안에서는 그 버튼이 회색으로 뜨고 눌리지 않는다.
그래서 밸런스·소수파 밸런스·3선 폭탄 게임은 **같은 `custom_id`의 선택 버튼을 스레드 안에도 따로 보낸다**
(`thread_discussion_view()` — 선택지 + 익명 발언하기, 발언마다 맨 아래로 재게시).
`votes`는 `(round_id, user_id)` upsert라 채널에서 누르든 스레드에서 누르든 마지막 선택 하나만 남는다.
버튼은 한 줄에 5개가 상한이다 — 3선 폭탄 게임은 색 3 + 익명 1을 한 줄에 둔다.

### 1:1 비공개 방 (딜레마의 섬 · 모래성)

두 메인게임 모두 짝마다 비공개 스레드를 열고 그 안에 게임 버튼을 둔다. 공용 헬퍼는 `create_pair_thread()`다.

**방에 초대하는 것은 짝 두 사람뿐이다. 운영자를 참관용으로 넣지 말 것.** 운영자도 게임에 참여할 수 있어서
모든 방에 넣으면 1:1이 성립하지 않는다. 운영자는 `스레드 관리` 권한으로 스레드 목록에서 필요한 방만 골라 본다
(디스코드 서버 설정에서 운영자 역할에 그 권한을 준다 — 코드가 아니다).

버튼 핸들러는 누른 곳이 본인 짝의 방인지 반드시 검증한다 (`handle_trust_decision`, `sand_pair_thread_matches`).
방 안에서 누른 버튼의 `interaction.followup`은 그 방으로 들어가므로, **라운드 결과는 메인 채널로 직접 보내야 한다.**

버튼을 누를 때마다 개인 응답(ephemeral)이 방에 쌓여 버튼이 달린 진행판을 위로 밀어낸다. 그래서 모래성은
상호작용마다 방의 진행판을 지우고 맨 아래에 다시 붙인다(`refresh_sand_pair_board`, 현재 id는
`sand_pairs[].play_message_id`). 숫자 선택은 두 사람이 동시에 누를 수 있어 `interaction.message`가 이미
사라진 옛 진행판일 수 있으므로, `EventBot.sand_board_lock(thread_id)`을 잡고 DB에서 id를 다시 읽는다.

참가자가 홀수면 `build_random_trust_pairs()`가 한 명을 `extra_json.unmatched_user_id`로 남긴다.
그 사람에게는 메인 메시지에 **긴급 투입** 버튼이 붙고, 게임에 참여하지 않은 아무나 눌러 짝이 될 수 있다
(`apply_emergency_join`). 누른 사람은 `event_participants`에 자동 등록되고, 전체 마감이 1분 밀린다.
**누가 투입됐는지는 메인 채널에만 공개한다.** 각 방에는 시간 연장 사실만 알린다 — 방마다 상대가 누구인지가
게임의 일부이므로 다른 방에 이름이 새면 안 된다.

### 게임 타입 이름 주의

`rounds.type` CHECK 제약은 `('balance','bomb','grid','cutting','top3','trust','sand')`인데, 실제 게임 목록과 다르다.

- **미니게임 "3선 폭탄 게임"의 레거시 코드상 이름은 `defuse4`이고 DB에는 `bomb`으로 저장된다** (`extra_json.mode = "defuse4"`). 새 라운드는 `extra_json.settlement_rule = "top_vote_penalty"`로 구방식 안전선 라운드와 구분한다.
- **소수파 밸런스는 DB에 `balance`로 저장하고 `extra_json.mode = "minority_balance"`로 구분한다.** `balance_phase`는 `initial` → `revote` 순서이며, 1차 `votes`를 유지한 채 변경 선택을 upsert하고 두 번째 마감에서만 소수파를 정산한다.
- **`grid`(관리자 시점 타일)는 제거된 게임이다.** `BASE_SCHEMA`가 불변이라 CHECK 제약에서 뺄 수 없어,
  새 **유일 숫자 게임**을 DB에 저장할 때만 `type = grid`, `extra_json.mode = unique_number`로 재사용한다.
  `mode`가 없는 옛 grid 라운드는 `close_round_record`의 마지막 `else`에서 점수 없이 마감한다.

권위 있는 목록은 `MAIN_GAME_TYPES` / `MINI_GAME_TYPES` / `GAME_NAMES` 상수다. CHECK 제약이 아니다.

### 스키마 변경

**`BASE_SCHEMA`(v1)는 절대 수정하지 않는다.** `tests/test_schema.py`가 SHA256 체크섬으로 고정해두었다.
수정하면 새로 만든 DB와 운영 DB의 스키마가 조용히 갈라진다.

스키마 추가는 `MIGRATIONS`에 등록한다. `SCHEMA_VERSION`은 자동 계산되고, 부팅 시 `init_db()`가
`PRAGMA user_version`을 보고 밀린 것만 적용한다. 현재 최신은 **v4**(`russian_roulette_games` 테이블).

```python
MIGRATIONS: dict[int, list[str]] = {
    4: ["ALTER TABLE rounds ADD COLUMN memo TEXT"],
}
```

롤백 가능해야 하므로 **반드시 추가 형태**(`ADD COLUMN`, `CREATE TABLE` 등)로만. 컬럼 삭제/이름 변경/
제약 변경은 구버전 코드로 되돌아갈 수 없게 만든다. 마이그레이션을 빠뜨리면 `verify_schema()`가 부팅을
막는다 — 이게 없으면 봇이 로그인까지 성공해놓고(배포 헬스체크 통과) 게임 도중 `no such column`으로 터진다.

기존 테이블의 PK를 바꿔야 하는 변경은 불가능하다. `discussion_posts`(PK `(round_id, user_id)`)의
1인 1회 제약을 풀 때 그 테이블을 고치는 대신 `discussion_messages`를 새로 만든 것이 그 사례다.
`discussion_posts`는 과거 데이터를 위해 남아 있을 뿐 더 이상 쓰지 않는다.

### systemd 연동

서비스가 `Type=notify`라서, 봇이 디스코드 로그인 + 타이머 복구를 마치고 `on_ready`에서
`sd_notify("READY=1")`을 보내야 `systemctl restart`가 성공으로 끝난다. **재시작 명령의 종료 코드가 곧 헬스체크다.**
기동에 실패하면 배포 스크립트가 이전 릴리스로 자동 롤백한다.

## DB 위치

| | 경로 |
|---|---|
| 로컬 | `data/event_game.sqlite3` (gitignore) |
| 운영 | `/var/lib/yumcbot/data/event_game.sqlite3` — 코드 트리(`/opt/yumcbot`) 밖이라 배포로 덮이지 않는다 |

## 브랜치 / 배포

```
feature/*  각자 로컬에서 본인 dev 봇으로 개발
develop    통합 브랜치. 배포 없음
main       운영 봇. PR 머지 시 자동 배포
```

배포 전 `deploy/check_idle.py`가 **진행 중인 라운드/이벤트가 있으면 배포를 거부한다**
(Actions에서 `force: true`로 무시 가능).

`deploy/` 아래 스크립트와 `yumcbot.service`는 **배포로 갱신되지 않는다.** 배포가 갈아끼우는 건 앱 코드뿐이다.
수정했다면 서버에서 `bootstrap.sh`를 다시 돌려야 한다. 서버 셋업/배포 동작 상세는 `deploy/README.md`.

`.gitattributes`가 `*.sh` / `*.service` / `deploy/*.py`를 LF로 고정한다. CRLF로 체크아웃되면 shebang이 깨진다.
