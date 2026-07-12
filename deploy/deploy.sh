#!/usr/bin/env bash
# 릴리스 배포. GitHub Actions self-hosted runner(yumcbot 계정)가 호출한다.
#
#   yumcbot-deploy <git-sha> <source-dir> [--force]
#
# 흐름:
#   유휴 확인 -> 릴리스 생성 -> DB 백업 -> 심볼릭 링크 원자적 교체 -> 재시작
#   -> (실패 시) 이전 릴리스로 자동 롤백
#
# 재시작은 Type=notify라 봇이 디스코드 로그인을 마쳐야 성공으로 끝난다.
# 즉 `systemctl restart`의 종료 코드가 곧 헬스체크다.
set -Eeuo pipefail

APP_ROOT=/opt/yumcbot
RELEASES="$APP_ROOT/releases"
CURRENT="$APP_ROOT/current"
DATA_DIR=/var/lib/yumcbot/data
BACKUP_DIR=/var/lib/yumcbot/backups
DB_PATH="$DATA_DIR/event_game.sqlite3"
SERVICE=yumcbot
PYTHON=python3.12
KEEP_RELEASES=5

SHA="${1:-}"
SRC="${2:-}"
FORCE="${3:-}"

if [[ -z "$SHA" || -z "$SRC" ]]; then
    echo "usage: yumcbot-deploy <git-sha> <source-dir> [--force]" >&2
    exit 64
fi
if [[ ! -d "$SRC" ]]; then
    echo "소스 디렉터리가 없습니다: $SRC" >&2
    exit 64
fi

NEW="$RELEASES/$SHA"
PREVIOUS=""
[[ -L "$CURRENT" ]] && PREVIOUS="$(readlink -f "$CURRENT")"

log() { echo "[deploy] $*"; }

db_version() {
    [[ -f "$DB_PATH" ]] || { echo "none"; return; }
    "$PYTHON" - "$DB_PATH" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], timeout=5.0)
print(conn.execute("PRAGMA user_version").fetchone()[0])
conn.close()
PY
}

# ---------------------------------------------------------------- 1. 배포 게이트
if [[ "$FORCE" == "--force" ]]; then
    log "force 지정됨. 진행 중인 이벤트 확인을 건너뜁니다."
else
    log "진행 중인 이벤트 확인"
    "$PYTHON" "$SRC/deploy/check_idle.py" --db "$DB_PATH"
fi

# ------------------------------------------------------------- 2. 릴리스 준비
log "릴리스 생성: $NEW"
rm -rf "$NEW"
mkdir -p "$NEW"
rsync -a --delete \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude 'data/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    "$SRC"/ "$NEW"/

log "가상환경 + 의존성 설치"
"$PYTHON" -m venv "$NEW/.venv"
"$NEW/.venv/bin/python" -m pip install --quiet --upgrade pip
"$NEW/.venv/bin/python" -m pip install --quiet -r "$NEW/requirements.txt"

# --------------------------------------------------------------- 3. DB 백업
BACKUP=""
VERSION_BEFORE="$(db_version)"
if [[ -f "$DB_PATH" ]]; then
    BACKUP="$BACKUP_DIR/pre-$SHA-$(date +%Y%m%d-%H%M%S).sqlite3"
    log "DB 백업: $BACKUP (schema v$VERSION_BEFORE)"
    # 온라인 백업 API. 봇이 돌고 있어도 안전하고 WAL도 함께 반영된다.
    "$PYTHON" - "$DB_PATH" "$BACKUP" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1], timeout=10.0)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close()
src.close()
PY
fi

# --------------------------------------------------- 4. 심볼릭 링크 원자적 교체
log "current -> $SHA"
ln -sfn "$NEW" "$CURRENT.tmp"
mv -Tf "$CURRENT.tmp" "$CURRENT"   # rename(2). 중간 상태가 없다.

# ------------------------------------------------------------------ 5. 재시작
log "서비스 재시작 (봇 로그인까지 대기)"
if sudo systemctl restart "$SERVICE"; then
    log "기동 성공. 배포 완료: $SHA"
else
    # ------------------------------------------------------------ 6. 자동 롤백
    echo "[deploy] 기동 실패. 롤백합니다." >&2
    journalctl -u "$SERVICE" -n 40 --no-pager >&2 || true

    if [[ -z "$PREVIOUS" ]]; then
        echo "[deploy] 이전 릴리스가 없어 롤백할 수 없습니다. 수동 확인 필요." >&2
        exit 1
    fi

    sudo systemctl stop "$SERVICE" || true

    # 마이그레이션이 돌아 스키마가 바뀐 경우에만 DB를 되돌린다.
    # 코드 버그로 인한 기동 실패라면 DB는 멀쩡하므로 손대지 않는다.
    VERSION_AFTER="$(db_version)"
    if [[ -n "$BACKUP" && "$VERSION_AFTER" != "$VERSION_BEFORE" ]]; then
        echo "[deploy] 스키마가 v$VERSION_BEFORE -> v$VERSION_AFTER 로 바뀌었습니다. DB를 되돌립니다." >&2
        rm -f "$DB_PATH" "$DB_PATH-wal" "$DB_PATH-shm"
        cp "$BACKUP" "$DB_PATH"
    fi

    echo "[deploy] current -> $(basename "$PREVIOUS")" >&2
    ln -sfn "$PREVIOUS" "$CURRENT.tmp"
    mv -Tf "$CURRENT.tmp" "$CURRENT"
    sudo systemctl start "$SERVICE"

    echo "[deploy] 롤백 완료. 배포는 실패했습니다: $SHA" >&2
    exit 1
fi

# ------------------------------------------------------- 7. 오래된 릴리스 정리
log "오래된 릴리스 정리 (최근 $KEEP_RELEASES개 유지)"
KEEP_PATH="$(readlink -f "$CURRENT")"
# shellcheck disable=SC2012
ls -1dt "$RELEASES"/*/ 2>/dev/null | tail -n +$((KEEP_RELEASES + 1)) | while read -r old; do
    old="${old%/}"
    [[ "$(readlink -f "$old")" == "$KEEP_PATH" ]] && continue
    log "  삭제: $(basename "$old")"
    rm -rf "$old"
done

log "완료."
