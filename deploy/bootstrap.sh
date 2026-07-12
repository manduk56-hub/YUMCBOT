#!/usr/bin/env bash
# 서버 최초 1회 셋업 (Rocky Linux). root로 실행한다.
#
#   sudo bash deploy/bootstrap.sh
#
# deploy/ 아래 스크립트를 수정한 뒤에는 다시 실행해서 서버 쪽 사본을 갱신한다.
# (재실행해도 안전하도록 멱등하게 작성돼 있다. DB와 .env는 절대 덮어쓰지 않는다.)
set -Eeuo pipefail

APP_USER=yumcbot
APP_ROOT=/opt/yumcbot
STATE_ROOT=/var/lib/yumcbot
CONF_DIR=/etc/yumcbot
ENV_FILE="$CONF_DIR/yumcbot.env"
PYTHON=python3.12

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "root로 실행하세요: sudo bash deploy/bootstrap.sh" >&2
    exit 1
fi

echo "==> 패키지 설치"
dnf install -y "$PYTHON" "$PYTHON-devel" git rsync policycoreutils

echo "==> 서비스 계정 $APP_USER"
if ! id -u "$APP_USER" &>/dev/null; then
    useradd --system --home-dir "$APP_ROOT" --shell /usr/sbin/nologin "$APP_USER"
fi
# 봇 서비스의 journal 로그를 배포 스크립트가 읽을 수 있어야 한다.
usermod -aG systemd-journal "$APP_USER"

echo "==> 디렉터리"
install -d -o "$APP_USER" -g "$APP_USER" -m 755 "$APP_ROOT" "$APP_ROOT/releases"
# DB는 코드 디렉터리 밖에 둔다. 배포가 releases/를 갈아끼워도 데이터는 그대로다.
install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$STATE_ROOT" "$STATE_ROOT/data" "$STATE_ROOT/backups"
install -d -o root -g "$APP_USER" -m 750 "$CONF_DIR"

echo "==> 환경변수 파일"
if [[ -f "$ENV_FILE" ]]; then
    echo "    이미 있음. 건드리지 않습니다: $ENV_FILE"
else
    cat >"$ENV_FILE" <<'EOF'
# 운영 봇 토큰. Discord Developer Portal에서 발급.
# 개발자 각자의 dev 봇 토큰과 절대 섞지 말 것 (같은 토큰 중복 실행 시 모든 이벤트가 2번 처리됨).
DISCORD_TOKEN=

# 운영 디스코드 서버 ID
GUILD_ID=

# 운영자 역할 ID (쉼표 구분, 선택)
OPERATOR_ROLE_IDS=

# DB 위치. 코드 디렉터리 밖이어야 배포로 덮이지 않는다.
YUMCBOT_DATA_DIR=/var/lib/yumcbot/data
EOF
    chown root:"$APP_USER" "$ENV_FILE"
    chmod 640 "$ENV_FILE"
    echo "    생성됨: $ENV_FILE  <- DISCORD_TOKEN과 GUILD_ID를 채우세요"
fi

echo "==> 배포 스크립트 설치"
install -o root -g root -m 755 "$HERE/deploy.sh" /usr/local/bin/yumcbot-deploy

echo "==> systemd 유닛"
install -o root -g root -m 644 "$HERE/yumcbot.service" /etc/systemd/system/yumcbot.service
systemctl daemon-reload
systemctl enable yumcbot.service

echo "==> sudoers (yumcbot 계정이 자기 서비스만 제어)"
cat >/etc/sudoers.d/yumcbot <<'EOF'
yumcbot ALL=(root) NOPASSWD: /usr/bin/systemctl restart yumcbot, \
                             /usr/bin/systemctl start yumcbot, \
                             /usr/bin/systemctl stop yumcbot, \
                             /usr/bin/systemctl is-active yumcbot, \
                             /usr/bin/systemctl show yumcbot *
EOF
chmod 440 /etc/sudoers.d/yumcbot
visudo -cf /etc/sudoers.d/yumcbot

if command -v restorecon &>/dev/null; then
    echo "==> SELinux 컨텍스트 복원"
    restorecon -R "$APP_ROOT" "$STATE_ROOT" || true
fi

echo
echo "완료. 다음 순서로 마무리하세요:"
echo "  1) $ENV_FILE 에 DISCORD_TOKEN, GUILD_ID 입력"
echo "  2) GitHub Actions self-hosted runner를 $APP_USER 계정으로 등록 (deploy/README.md 참고)"
echo "  3) main 브랜치에 push하면 자동 배포됩니다"
