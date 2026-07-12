# 배포 가이드

## 구조

```
main    ─ 운영 봇. push하면 GitHub Actions가 서버에 자동 배포
develop ─ 통합 브랜치. 배포 없음. 각자 pull 받아 로컬에서 검증
feature ─ 개발자별 로컬 작업. 각자의 dev 봇으로 테스트
```

**같은 봇 토큰을 두 프로세스에서 동시에 실행하면 안 됩니다.** 디스코드 게이트웨이는
토큰 단위로 세션을 붙이기 때문에, 두 프로세스가 뜨면 버튼 클릭과 슬래시 명령이
**각각 한 번씩, 총 두 번** 처리됩니다. 점수가 2배로 들어가고 라운드가 두 번 정산됩니다.
길드가 달라도 마찬가지입니다. 그래서 개발자마다 **자기 전용 Discord 앱**이 필요합니다.

---

## 1. 개발자 로컬 셋업 (각자 1회)

1. [Discord Developer Portal](https://discord.com/developers/applications)에서 **자기 앱을 새로 생성**
   (예: `YUMCBOT-dev-이름`) → Bot 탭에서 토큰 발급
2. 테스트용 디스코드 서버를 하나 만들고 그 봇만 초대
   (필요 권한은 루트 `README.md`의 "Discord 개발자 포털 설정" 참고)
3. 파이썬 **3.12** 설치 (서버와 버전을 맞춘다)
4. 저장소에서:

```powershell
Copy-Item .env.example .env
# .env 에 자기 dev 봇 토큰과 테스트 서버 ID 입력
.\run.ps1
```

`GUILD_ID`를 넣으면 슬래시 명령어가 즉시 동기화됩니다 (비우면 전역 등록이라 최대 1시간).

이후 개발 루프는 기존과 동일합니다. **코드 수정 → `.\run.ps1` → 테스트.**

---

## 2. 서버 셋업 (Rocky Linux, 1회)

```bash
git clone https://github.com/manduk56-hub/YUMCBOT.git /tmp/yumcbot
sudo bash /tmp/yumcbot/deploy/bootstrap.sh
sudo vi /etc/yumcbot/yumcbot.env   # DISCORD_TOKEN(운영 봇), GUILD_ID 입력
```

`bootstrap.sh`가 만드는 것:

| 경로 | 용도 |
|---|---|
| `/opt/yumcbot/releases/<sha>/` | 릴리스별 코드 + 전용 venv |
| `/opt/yumcbot/current` | 현재 릴리스를 가리키는 심볼릭 링크 |
| `/var/lib/yumcbot/data/` | **DB. 코드 디렉터리 밖이라 배포로 덮이지 않는다** |
| `/var/lib/yumcbot/backups/` | 배포 직전 DB 백업 |
| `/etc/yumcbot/yumcbot.env` | 운영 토큰 (root:yumcbot 0640) |
| `/usr/local/bin/yumcbot-deploy` | 배포 스크립트 |

`deploy/` 아래 스크립트를 수정하면 서버에서 `bootstrap.sh`를 **다시 실행**해야 사본이 갱신됩니다.
(멱등하게 작성돼 있어 재실행해도 DB와 `.env`는 건드리지 않습니다.)

### GitHub Actions self-hosted runner 등록

서버가 GitHub로 아웃바운드 연결만 하므로 공인 IP나 포트 개방이 필요 없습니다.

GitHub 저장소 → Settings → Actions → Runners → New self-hosted runner (Linux x64)에서
토큰을 받아 **`yumcbot` 계정으로** 등록합니다:

```bash
sudo -u yumcbot -H bash
mkdir -p /opt/yumcbot/runner && cd /opt/yumcbot/runner
curl -o actions-runner.tar.gz -L <페이지에 표시된 URL>
tar xzf actions-runner.tar.gz
./config.sh --url https://github.com/manduk56-hub/YUMCBOT \
            --token <페이지에 표시된 토큰> \
            --labels yumcbot-prod \
            --unattended
exit

# 부팅 시 자동 시작
sudo /opt/yumcbot/runner/svc.sh install yumcbot
sudo /opt/yumcbot/runner/svc.sh start
```

라벨 `yumcbot-prod`가 `deploy.yml`의 `runs-on`과 일치해야 합니다.

---

## 3. 배포 동작

`main`에 push되면:

1. **test** (GitHub 호스팅) — 문법 검사 + 테스트
2. **deploy** (서버컴 runner) — `yumcbot-deploy <sha> <workspace>`

`yumcbot-deploy`가 하는 일:

1. **배포 게이트** — `check_idle.py`로 진행 중인 라운드/이벤트가 있는지 확인.
   있으면 **배포하지 않고 실패**한다.
2. 릴리스 생성 (`releases/<sha>` + 전용 venv에 의존성 설치)
3. **DB 백업** (온라인 백업 API. 봇이 돌고 있어도 안전)
4. `current` 심볼릭 링크를 `rename(2)`로 원자적 교체
5. `systemctl restart yumcbot`

서비스가 `Type=notify`라서, 봇이 **디스코드 로그인을 마치고 `READY=1`을 보내야**
`systemctl restart`가 성공으로 끝납니다. 즉 재시작 명령의 종료 코드가 곧 헬스체크입니다.

**기동에 실패하면 자동 롤백:**

- 심볼릭 링크를 이전 릴리스로 되돌리고 재시작
- 마이그레이션이 실행돼 스키마 버전이 바뀐 경우에만 DB도 백업본으로 복구
  (코드 버그로 인한 실패라면 DB는 멀쩡하므로 손대지 않는다)

### 다운타임

**재시작 3~5초.** 그 사이 버튼을 누른 사람만 "상호작용 실패"를 보고, 다시 누르면 정상입니다.

봇이 재시작에 안전한 이유:

- 버튼이 `discord.ui.View` 콜백이 아니라 `on_interaction`에서 `custom_id`를 파싱해
  DB를 조회하는 방식이라, 이미 채널에 뿌려진 버튼이 재시작 후에도 그대로 동작한다
- 라운드 상태는 전부 SQLite에 있다 (메모리에 들고 있지 않다)
- 자동 마감 타이머는 부팅 시 `restore_auto_close_tasks()`가 DB에서 복구한다

### 이벤트 진행 중에 급하게 배포해야 한다면

Actions → Deploy → Run workflow → **force: true**.
배포 게이트를 건너뜁니다. 참가자들이 버튼 실패를 볼 수 있습니다.

---

## 4. 스키마 변경 규칙

`BASE_SCHEMA`(v1)는 **절대 수정하지 않습니다.** 고치면 새로 만든 DB와 이미 굴러가는
운영 DB의 스키마가 조용히 갈라집니다. `tests/test_schema.py`가 체크섬으로 이걸 막습니다.

컬럼을 추가하려면 `bot.py`의 `MIGRATIONS`에 등록합니다:

```python
MIGRATIONS: dict[int, list[str]] = {
    2: ["ALTER TABLE rounds ADD COLUMN memo TEXT"],
}
```

`SCHEMA_VERSION`은 자동으로 계산되고, 봇이 부팅할 때 `PRAGMA user_version`을 보고
밀린 마이그레이션만 순서대로 적용합니다.

롤백이 가능하려면 마이그레이션은 반드시 **추가 형태**여야 합니다 (`ADD COLUMN` 등).
컬럼을 지우거나 이름을 바꾸면 구버전 코드로 되돌아갈 수 없습니다.

**마이그레이션을 빠뜨리면** `verify_schema()`가 부팅을 막습니다. 이게 없으면 봇은
정상 로그인까지 해놓고 게임 도중에 `no such column`으로 터집니다 — 헬스체크도 통과해
버려서 최악입니다.

---

## 5. 운영 명령어

```bash
sudo systemctl status yumcbot
sudo journalctl -u yumcbot -f            # 실시간 로그
sudo systemctl restart yumcbot

ls -l /opt/yumcbot/current                # 현재 릴리스
ls -1t /opt/yumcbot/releases              # 릴리스 목록 (최근 5개 유지)
ls -lt /var/lib/yumcbot/backups           # DB 백업
```

### 수동 롤백

```bash
sudo -u yumcbot ln -sfn /opt/yumcbot/releases/<이전-sha> /opt/yumcbot/current.tmp
sudo -u yumcbot mv -Tf /opt/yumcbot/current.tmp /opt/yumcbot/current
sudo systemctl restart yumcbot
```

---

## 6. 브랜치 보호 설정 (GitHub Settings → Branches)

**`main`**
- Require a pull request before merging (승인 1명 이상)
- Require status checks to pass → `test`
- Do not allow bypassing the above settings

**`develop`**
- Require a pull request before merging
- Require status checks to pass → `test`
