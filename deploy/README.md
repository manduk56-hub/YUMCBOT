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
sudo dnf install -y git
sudo git clone -b main https://github.com/manduk56-hub/YUMCBOT.git /usr/local/src/YUMCBOT
sudo bash /usr/local/src/YUMCBOT/deploy/bootstrap.sh
sudo vi /etc/yumcbot/yumcbot.env   # DISCORD_TOKEN(운영 봇), GUILD_ID 입력
```

이 clone은 **`bootstrap.sh`를 돌리기 위한 것일 뿐**이고 봇 실행과는 무관합니다.
`/tmp`가 아니라 `/usr/local/src`에 두는 이유는, `deploy/`를 수정했을 때
`git pull` 후 `bootstrap.sh`를 다시 돌려야 하기 때문입니다.

`bootstrap.sh`가 만드는 것:

| 경로 | 용도 |
|---|---|
| `/opt/yumcbot/releases/<sha>/` | 릴리스별 코드 + 전용 venv |
| `/opt/yumcbot/current` | 현재 릴리스를 가리키는 심볼릭 링크 |
| `/opt/yumcbot/runner/` | GitHub Actions self-hosted runner |
| `/var/lib/yumcbot/data/` | **DB. 코드 디렉터리 밖이라 배포로 덮이지 않는다** |
| `/var/lib/yumcbot/backups/` | 배포 직전 DB 백업 |
| `/etc/yumcbot/yumcbot.env` | 운영 토큰 (root:yumcbot 0640) |
| `/usr/local/bin/yumcbot-deploy` | 배포 스크립트 |
| `/usr/local/bin/yumcbot-setup-runner` | 러너 등록 스크립트 |
| `/home/yumcbot-app`, `/home/yumcbot-data` | 위 두 경로를 가리키는 이정표 링크 (사람용) |

**코드(`/opt`) · 데이터(`/var/lib`) · 설정(`/etc`)을 분리**한 것이 핵심입니다.
배포는 `/opt/yumcbot/releases/`에 새 디렉터리를 만들고 `current` 링크만 갈아끼웁니다.
DB가 코드 트리 안에 있었다면 배포할 때마다 빈 DB로 시작하게 됩니다.

`/home/yumcbot-*` 링크는 사람이 다른 서비스들과 나란히 찾으라고 둔 것입니다.
봇 프로세스는 이 링크를 타지 않습니다 (systemd `ProtectHome=true`).

> **백업 대상에 `/var/lib/yumcbot/data` 를 추가하세요.**
> `/home`만 백업하는 정책이면 DB가 빠집니다.

`deploy/` 아래 스크립트나 `yumcbot.service`를 수정하면, **배포로는 갱신되지 않습니다.**
배포가 갈아끼우는 건 `/opt/yumcbot/releases/` 아래 앱 코드뿐입니다.
서버에서 직접 갱신해야 합니다:

```bash
sudo git -C /usr/local/src/YUMCBOT pull
sudo bash /usr/local/src/YUMCBOT/deploy/bootstrap.sh
```

(멱등하게 작성돼 있어 재실행해도 DB와 `.env`는 건드리지 않습니다.)

### GitHub Actions self-hosted runner 등록

서버가 GitHub로 **아웃바운드 연결만** 합니다. 공인 IP도, 포트 개방도, SSH 키도,
GitHub Secrets도 필요 없습니다. 워크플로우 어디에도 서버 주소가 들어가지 않습니다.

1. GitHub 저장소 → Settings → Actions → Runners → **New self-hosted runner** → Linux x64
2. 화면에 `AXXXX...` 형태의 **등록 토큰**이 표시됩니다 (유효기간 1시간)
3. 서버에서:

```bash
sudo /usr/local/bin/yumcbot-setup-runner <등록토큰>
```

**반드시 전체 경로로 부르세요.** RHEL 계열은 `sudo`의 `secure_path`에 `/usr/local/bin`이
없어서 이름만 쓰면 `command not found`가 납니다.

이 한 줄이 러너 다운로드 → `yumcbot` 계정으로 등록 → systemd 서비스 등록까지 처리합니다.
끝나면 Runners 목록에 `Idle` 상태로 나타납니다.

러너는 등록 시점의 PATH를 `/opt/yumcbot/runner/.path`에 굳혀두고 잡 실행에 그대로 씁니다.
그래서 `deploy.yml`은 `yumcbot-deploy`를 **절대 경로로** 호출합니다.

등록 토큰은 `config.sh`가 **영구 자격증명(`.credentials`)으로 교환**하는 순간 역할이
끝납니다. 따로 보관할 필요가 없습니다.

**러너를 봇과 같은 `yumcbot` 계정으로 돌리는 이유:** 배포 스크립트가 `/opt/yumcbot/releases`에
릴리스를 쓰는데, 계정이 같으면 별도 권한이 필요 없습니다. 권한이 필요한 건 서비스 재시작뿐이고
그것만 sudoers로 좁게 열어뒀습니다 (`systemctl restart yumcbot` 등).

라벨 `yumcbot-prod`가 `deploy.yml`의 `runs-on`과 일치해야 합니다. 안 맞으면 deploy 잡이
러너를 찾지 못하고 계속 대기합니다.

```bash
sudo /opt/yumcbot/runner/svc.sh status   # 러너 상태
```

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
