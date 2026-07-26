# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Cursor, Codex 등) when working with code in this repository.

방학 이벤트용 Discord 봇. 이벤트마다 **메인게임 1개 + 미니게임 1개**를 진행하고 점수를 SQLite에 누적한다.
운영 봇은 Rocky Linux 서버에서 systemd로 24시간 돌고, `main` 머지 시 GitHub Actions가 자동 배포한다.

## 명령어

파이썬 **3.12** (운영 서버와 버전을 맞춘다).

```powershell
.\run.ps1                                   # 봇 실행 (.venv 없으면 자동 생성 + 의존성 설치)

python -m unittest discover -s tests -v     # 전체
python -m unittest tests.test_bot -v        # 파일 단위 (단일 테스트는 뒤에 .클래스.메서드)

python -m compileall -q bot.py deploy/check_idle.py   # CI가 도는 문법 검사
shellcheck deploy/*.sh                                # CI가 도는 배포 스크립트 검사
```

테스트는 `bot.DB_PATH`를 임시 디렉터리로 patch해서 돈다. 디스코드 연결 없이 순수 함수와 DB 로직만 검증한다.

## 개발자마다 전용 Discord 앱이 필요하다

같은 봇 토큰으로 두 프로세스를 띄우면 게이트웨이 세션이 둘 붙어서 **모든 버튼/명령이 두 번씩 처리된다**
(점수 2배 지급, 라운드 두 번 정산). 길드가 달라도 마찬가지다. 운영 봇 토큰을 로컬 `.env`에 넣지 말 것.

`.env`: `DISCORD_TOKEN`(본인 dev 봇), `GUILD_ID`(있으면 슬래시 명령어 즉시 동기화), `OPERATOR_ROLE_IDS`, `YUMCBOT_DATA_DIR`(선택).

봇이 `members` 특권 인텐트를 쓴다(정산 시 이름 캐시 미스로 `display_name`이 오염되던 issue #19). **Developer Portal → 앱 → Bot → SERVER MEMBERS INTENT를 각자 ON** 해야 한다. 안 켜면 로그인 단계에서 `PrivilegedIntentsRequired`로 죽는다(운영은 헬스체크에 걸려 자동 롤백). 코드보다 포털이 먼저다.

## 아키텍처

전체 로직이 `bot.py` 한 파일에 있고, 구역 순서는 대략:
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
`can_join_round()`가 **`참여하기`를 누른 유저만** 투표/토론하게 막고, `/start` 시점에 명단이 마감된다.
정산은 전부 `close_round_record()`의 타입별 분기에서 이뤄지며 `add_score()`가
`users.total_score`와 `score_logs`를 함께 갱신한다.

### 스레드에서 버튼이 죽는 두 가지 경우

**① 잠근 스레드.** `archived`/`locked` 상태에서는 `스레드 관리` 권한이 없는 유저의 버튼 클릭을 디스코드가 막는다.
익명 토론방을 잠갔더니 "관리자만 익명 발언이 된다"는 버그가 났었다. 스레드는 채널 권한을 상속해 개별 덮어쓰기가
불가능하므로 일반 채팅 차단은 **`on_message`의 메시지 삭제로만** 한다. 잠그는 건 라운드가 끝나 버튼이 필요 없을
때뿐이다(`publish_trust_pair_results` / `publish_sand_pair_results`). 자동 보관도 같은 문제라 스레드 생성 시
`auto_archive_duration=1440`을 명시한다.

**② 스레드의 시작 메시지.** 토론 스레드는 라운드 메시지에서 파생되므로(`message.create_thread`) 그 메시지가
시작 메시지가 되는데, 실체가 부모 채널에 있어 스레드 안에서는 회색으로 뜨고 눌리지 않는다. 그래서 밸런스·소수파
밸런스·3선 폭탄 게임은 **같은 `custom_id`의 선택 버튼을 스레드 안에도 따로 보낸다**(`thread_discussion_view()`).
`votes`는 `(round_id, user_id)` upsert라 어디서 누르든 마지막 하나만 남는다. 버튼은 한 줄에 5개가 상한이다.

### 1:1 비공개 방 (딜레마의 섬 · 모래성 · 가라앉는 배)

메인게임 셋 모두 짝마다 비공개 스레드를 열고 그 안에 게임 버튼을 둔다 (`create_pair_thread()`).

**방에 초대하는 것은 짝 두 사람뿐이다. 운영자를 참관용으로 넣지 말 것.** 운영자도 게임에 참여할 수 있어서
모든 방에 넣으면 1:1이 성립하지 않는다. 운영자는 `스레드 관리` 권한으로 필요한 방만 골라 본다
(서버 설정에서 역할에 주는 것 — 코드가 아니다).

- 버튼 핸들러는 누른 곳이 본인 짝의 방인지 검증한다 (`handle_trust_decision`, `sand_pair_thread_matches`).
- 방 안 버튼의 `interaction.followup`은 그 방으로 가므로 **라운드 결과는 메인 채널로 직접 보낸다.**
- ephemeral 응답이 쌓여 진행판을 위로 밀어내므로 모래성은 상호작용마다 진행판을 지우고 맨 아래에
  다시 붙인다(`refresh_sand_pair_board`). 두 사람이 동시에 누르면 `interaction.message`가 이미 사라진
  옛 진행판일 수 있어, `EventBot.sand_board_lock(thread_id)`을 잡고 DB에서 id를 다시 읽는다.
- 홀수 인원은 `extra_json.unmatched_user_id`로 남고 **긴급 투입** 버튼으로 아무나 짝이 된다
  (`apply_emergency_join`). **누가 투입됐는지는 메인 채널에만 공개하고, 각 방에는 시간 연장만 알린다** —
  상대가 누구인지가 게임의 일부라 다른 방에 이름이 새면 안 된다.

**가라앉는 배만 단계가 하나 더 있다**: `registration → negotiation → execution → scored`.
그래서 자동 마감 타이머가 한 번 더 돈다 — `on_ready` 복구 콜백과 `/라운드마감` 양쪽에 전이 분기가 있어야
한다(`start_bail_negotiation` / `start_bail_execution`). 그리고 협상 → 실행 전이에서 버튼 구성이 바뀌므로
**모든 방의 진행판을 다시 그려야 한다**(`refresh_all_bail_pair_boards`) — 누른 사람 방만 갱신하는
모래성과 다른 점이다. 모든 짝이 협상을 먼저 마치면 남은 시간을 기다리지 않고 조기 전이한다.

이 게임의 핵심 규칙은 **협상에서 맺은 합의에 구속력이 없다**는 것이다. 정산은 실행 단계의 제출값만 보고
(`resolve_bail_pair`), 합의는 결과 공개용 기록으로만 남는다(`bail_promise_text`가 합의와 실제를 나란히
보여준다). 합의를 점수에 반영하면 게임이 성립하지 않으므로 정산 분기에서 `agreement`를 읽지 말 것.

### 게임 타입 이름 주의

`rounds.type` CHECK 제약(`balance,bomb,grid,cutting,top3,trust,sand`)은 **실제 게임 목록이 아니다.**
`BASE_SCHEMA`가 불변이라 제약을 못 고쳐서, 이름이 어긋난 채 `extra_json.mode`로 갈라 쓴다:

| 게임 | `type` | `extra_json.mode` | 비고 |
| --- | --- | --- | --- |
| 3선 폭탄 게임 | `bomb` | `defuse4` | 새 라운드는 `settlement_rule = "top_vote_penalty"`로 구방식과 구분 |
| 소수파 밸런스 | `balance` | `minority_balance` | |
| 유일 숫자 게임 | `grid` | `unique_number` | `grid`는 제거된 게임이라 이름만 재사용. `mode` 없는 옛 라운드는 점수 없이 마감 |
| 가라앉는 배 | `sand` | `bail` | **모래성과 같은 타입을 쓴다.** 아래 주의 참고 |

권위 있는 목록은 `MAIN_GAME_TYPES` / `MINI_GAME_TYPES` / `GAME_NAMES` 상수다. CHECK 제약이 아니다.
이 상수들은 **게임 키 집합**이지 `rounds.type` 값 집합이 아니다 (`MAIN_GAME_TYPES`에 `bail`이 있지만
그런 `type` 값은 DB에 없다). 게임을 갈라야 할 때는 `round_game_key(round_row)`를 쓴다.

**`bail`은 `sand`와 타입이 겹치는 첫 사례다.** `type == "sand"`로 분기하는 코드는 모래성과 가라앉는 배를
구분하지 못한다. 새 코드를 넣을 때 다음을 확인한다:

- 정산(`close_round_record`)은 `round_game_key(...) == "bail"` 분기를 **sand 분기보다 먼저** 둔다.
- 단계 조회는 `main_game_phase(round_row)`를 쓴다 (`trust_phase`/`sand_phase`/`bail_phase`를 한 곳에 모아둠).
- 짝 이력(`recent_match_pairs`)은 타입으로 조회하므로 **두 게임이 이력을 공유한다.** 의도된 동작이다 —
  같은 사람과 연속으로 만나는 것을 두 게임에 걸쳐 회피한다.
- 긴급 투입은 `round_game_key`를 넘긴다(`apply_emergency_join(extra, game_key, ...)`).

### 러시안룰렛 (상시 1:1)

이벤트/라운드 모델 밖에서 도는 유저 간 게임이다(`russian_roulette_games`). **점수가 유저 사이를
오가는 유일한 경로**라 순위 조작이 여기로만 들어온다. 아래 제약이 전부 그 방어선이라 하나씩 떼면
안 된다.

**양쪽 베팅액은 반드시 같다.** 각자 1~3점을 비공개로 넣고 `submit_roulette_bet()`이 **낮은 쪽으로
통일**한다. 서로 다른 금액을 허용하면 적게 건 쪽이 항상 +EV라(적게 걸고 상대의 큰 금액을 노림)
"무조건 최소액"이 유일한 정답이 되어 베팅이라는 선택 자체가 사라진다. 같은 이유로 **입력한 금액은
둘 다 넣을 때까지 공개하지 않는다** — 먼저 넣은 쪽이 일방적으로 불리해진다.

**신용 유저**(`is_roulette_credit_user()`, 보유 점수 < `ROULETTE_MIN_BET`)는 신청은 못 하고 수락만
된다. 입력값과 무관하게 최소액으로 눌리고(모달 없이 즉시 확정), **이겨도 점수가 오르지 않는 대신**
연승 스택이 쌓여 `ROULETTE_STACK_GOAL`연승마다 보상을 받는다. 0점도 포함이다 — 마이너스만 잡으면
정확히 0점인 유저가 어느 규칙에도 못 들어간다.

- 신용 여부는 **베팅 시점에 `*_negative` 컬럼으로 박아둔다.** 예치가 점수를 깎아서 정산 때 다시
  계산할 수 없다.
- **시간 초과 승리는 스택에 세지 않는다.** 실탄 위치는 못 고르지만 *안 누르는 것*은 고를 수 있어서,
  짜고 잠수해주면 확률 없이 스택을 채울 수 있다.
- 스택은 신용 상태가 끊기지 않았다는 뜻이라 **탈출하면 무효**다. 탈출은 메인게임 정산으로도
  일어나므로 `add_score()`가 매번 확인한다(`reset_roulette_stack_if_escaped`).

**차례 3분 제한, 넘기면 자동 패배.** 무승부 환불로 두면 잠수가 지배 전략이 된다 — 탄창을 다시 돌리지
않아 뒤 차례일수록 확률이 나쁘고 마지막 차례는 격발 시 100% 죽는다. 제한이 아예 없으면 예치금이
영구 동결되고, 동시 1결투 제한 때문에 상대까지 묶이며, `check_idle`이 배포도 영구 차단한다.

- 타이머는 **라운드 쪽(`restore_auto_close_tasks`)과 별개다.** 그쪽은 `rounds.extra_json`을 읽어서
  못 쓴다. `restore_roulette_turn_tasks()`를 `on_ready`에 **반드시 등록**한다. 빠뜨리면 재시작 순간
  타이머가 사라져 위 동결이 그대로 재현된다.
- 태스크 dict도 따로 쓴다. 둘 다 int 키인데 `round_id`와 `game_id`는 별개다.
- 차례 식별은 **마감 시각이 아니라 `shots_fired`**로 한다. 격발 직후 잡히는 다음 마감이 직전 마감과
  사실상 같은 값일 수 있어, 시각으로 비교하면 낡은 타이머가 살아 있는 차례를 죽인다.

**횟수 제한** — 하루 `ROULETTE_DAILY_LIMIT`회, 같은 상대 `ROULETTE_PAIR_DAILY_LIMIT`회, KST 자정 리셋.
무작위 게임도 무제한으로 반복하면 결과를 고를 수 있게 되므로, 횟수가 곧 이전 가능 총량의 상한이다.
차감 기준(`_ROULETTE_CONSUMED_SQL`)은 그리핑을 막으려고 갈라져 있다:

| 상황 | 차감 |
| --- | --- |
| `playing`/`finished` | 양쪽 다 |
| 베팅 단계 만료 | **베팅을 안 넣은 쪽만** — 수락 받아놓고 방치해 상대를 묶는 짓이 자기 횟수를 태우게 |
| pending 만료 / 거절 | **없음** — 거절은 언제나 공짜여야 표적 그리핑이 안 된다 |

같은 상대 제한은 성립한 판만 센다. 베팅 미입력 만료까지 세면 신청만 걸어두고 그 상대와의 하루 한 판을
태워버릴 수 있다. 같은 이유로 **`pending`은 신청자만 잠근다**(`active_roulette_game_for_user`) —
받는 쪽까지 잠그면 신청 방치만으로 상대를 공짜로 배제할 수 있다.

**점수는 비공개다**(`/지목`에 잠수 코인 30개). 그래서 **성립 실패 사유를 신청자에게 알리지 않는다.**
알려주면 금액을 바꿔가며 신청하는 것만으로 상대 점수를 캐는 무료 조회 수단이 된다. 상대 사정으로
못 여는 경우는 `"결투가 성립하지 않았습니다"` 하나로만 답하고, 구체적인 사유는 해당 본인에게만
ephemeral로 준다. 같은 이유로 **공개 결과 embed는 신용 유저가 이겨도 일반 유저와 문구가 같다** —
실제 점수 변동과 스택은 결과 메시지의 `내 정산 확인` 버튼으로 본인만 본다. 이 버튼은 **모든 판에**
붙어야 한다. 신용 유저의 판에만 붙으면 버튼 존재 자체가 신호가 된다.

> ephemeral은 그 유저 본인의 인터랙션에만 응답할 수 있다. 신용 유저가 이기는 경우는 상대가 방아쇠를
> 당겼거나(인터랙션 주인이 상대) 시간 초과(인터랙션 없음)라, 이긴 순간에 쏘는 것이 불가능해서 버튼을
> 남기는 형태가 됐다.

### 활동 로그 (`activity_logs`)

사후 분석(SQL·에이전트)용 **append-only 이벤트 스트림**. `log_activity()` 하나로만 쓴다.
**절대 UPDATE/DELETE 하지 않는다.**

존재 이유가 곧 사용 기준이다. 기존 테이블 대부분은 *현재 상태*라 덮어쓰기·삭제가 일어난다:

| 상태 테이블 | 잃어버리는 것 |
| --- | --- |
| `votes` (choice upsert) | 선택 **변경 이력**. 소수파 밸런스 1차 선택이 재선택으로 덮인다 |
| `event_participants` (취소 시 DELETE) | 참여했다 취소한 사실 |
| `russian_roulette_games` (`shots_fired` 카운터) | 누가 몇 번째에 격발했는지 |
| `russian_roulette_games` (`*_bet`은 낮은 쪽으로 통일 후 덮임) | 각자 **원래 얼마를 걸었는지** |
| `users.roulette_win_stack` (카운터) | 몇까지 쌓였다가 왜 초기화됐는지 (`set_roulette_stack()`이 남긴다) |
| `extra_json` (통째로 교체) | 진행 중간 상태 |
| `event_sessions` | `recruiting→active` 전이 시각, 정상 완료/강제 마감 구분 |

**그래서 상태를 덮어쓰기 직전에 로그를 남긴다.** 새 기능을 추가할 때 그 기능이 위 표처럼
상태를 덮어쓴다면 `log_activity()` 호출을 함께 넣어야 한다.

이미 append-only인 것(`score_logs`, `discussion_messages`)은 **내용을 중복 저장하지 않는다.**
익명 발언은 `payload.discussion_message_id`로 참조만 남겨 타임라인 순서만 확보한다.
점수도 개별 델타는 `score_logs`에 그대로 두고, 라운드당 `score:round_settled` 요약 1건만 남긴다
(요약은 분기별 `settlement` 문구가 아니라 실제 적용된 `score_logs`를 역산해서 만든다).

`payload_json`에 게임별 세부를 담으므로 **새 게임이 추가돼도 스키마 마이그레이션이 필요 없다.**

**규칙 스냅샷**: 라운드 생성/정산 시 `game_settings_snapshot()`이 튜닝 가능한 상수
(`CUTTING_REWARD_POINTS` 등)를 값으로 남긴다. 밸런스 몰표 -2점처럼 `close_round_record`에
하드코딩된 규칙은 값으로 뜰 수 없어서 `bot_version`(배포 커밋 SHA)을 항상 함께 남긴다 —
그 커밋을 열면 당시 정산 규칙을 그대로 확인할 수 있다. `BOT_VERSION`은 릴리스 디렉터리
이름(`/opt/yumcbot/releases/<sha>/`)에서 뽑는다.

운영자용 조회 UI는 아직 없다 (`bot.py`의 `TODO(activity-log)`). 서버에서 직접 조회한다.

### 스키마 변경

**`BASE_SCHEMA`(v1)는 절대 수정하지 않는다.** `tests/test_schema.py`가 SHA256 체크섬으로 고정해두었다.
수정하면 새로 만든 DB와 운영 DB의 스키마가 조용히 갈라진다.

스키마 추가는 `MIGRATIONS`에 다음 번호로 등록한다. `SCHEMA_VERSION`은 자동 계산되고,
부팅 시 `init_db()`가 `PRAGMA user_version`을 보고 밀린 것만 적용한다.

```python
MIGRATIONS: dict[int, list[str]] = {
    6: ["ALTER TABLE rounds ADD COLUMN memo TEXT"],   # 현재 최신이 v5이므로 다음은 6
}
```

롤백 가능해야 하므로 **반드시 추가 형태**(`ADD COLUMN`, `CREATE TABLE`)로만. 컬럼 삭제/이름 변경/
제약 변경은 구버전 코드로 되돌아갈 수 없게 만든다. 마이그레이션을 빠뜨리면 `verify_schema()`가 부팅을
막는다 — 이게 없으면 봇이 로그인까지 성공해놓고(배포 헬스체크 통과) 게임 도중 `no such column`으로 터진다.

**기존 테이블의 PK는 바꿀 수 없다.** `discussion_posts`의 1인 1회 제약을 풀 때 그 테이블을 고치지 않고
`discussion_messages`를 새로 만든 것이 그래서다 (`discussion_posts`는 과거 데이터용으로 남아 있을 뿐이다).

### systemd 연동

서비스가 `Type=notify`라서, 봇이 디스코드 로그인 + 타이머 복구를 마치고 `on_ready`에서
`sd_notify("READY=1")`을 보내야 `systemctl restart`가 성공으로 끝난다. **재시작 명령의 종료 코드가 곧 헬스체크다.**
기동에 실패하면 배포 스크립트가 이전 릴리스로 자동 롤백한다.

## DB 위치

| | 경로 |
| --- | --- |
| 로컬 | `data/event_game.sqlite3` (gitignore) |
| 운영 | `/var/lib/yumcbot/data/event_game.sqlite3` — 코드 트리(`/opt/yumcbot`) 밖이라 배포로 안 덮인다 |

## 브랜치 / 배포

```
feature/*  각자 로컬에서 본인 dev 봇으로 개발
develop    통합 브랜치. 배포 없음
main       운영 봇. PR 머지 시 자동 배포
```

배포 전 `deploy/check_idle.py`가 **진행 중인 라운드/이벤트가 있으면 배포를 거부한다**
(Actions에서 `force: true`로 무시 가능).

`deploy/` 아래 스크립트와 `yumcbot.service`는 **배포로 갱신되지 않는다.** 배포가 갈아끼우는 건 앱 코드뿐이다.
수정했다면 서버에서 `bootstrap.sh`를 다시 돌려야 한다. 서버 셋업/배포 동작 상세와 **서버 실물 스펙(Rocky Linux 8.10, i5-11600K, 125GiB RAM, KST)**은 `deploy/README.md`.

`.gitattributes`가 `*.sh` / `*.service` / `deploy/*.py`를 LF로 고정한다. CRLF로 체크아웃되면 shebang이 깨진다.

### main에 직접 커밋했다면 그 자리에서 develop으로 되돌린다

머지가 `develop → main` 한 방향뿐이라 **main에서 갈라진 커밋은 develop으로 돌아올 경로가 없다.**
이후 PR을 아무리 머지해도 방향이 반대라 영영 반영되지 않는다. 증상도 없다 — 테스트는 통과하고
3-way 머지라 파일이 덮이지도 않아서, 조용히 갈라진 채 쌓이다가 같은 파일을 건드릴 때 터진다
(`8bb32ae`의 `unixepoch()` 호환 수정이 main에만 3일간 떠 있었다).

```powershell
git merge origin/main                    # 핫픽스 직후 develop에서 바로
git log --oneline develop..origin/main   # 상시 점검: 비어 있어야 정상
```

브랜치를 비교할 때 `main..develop`(develop의 새 작업)만 보지 말고 **반대 방향도 함께 본다.**
