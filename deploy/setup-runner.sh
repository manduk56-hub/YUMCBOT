#!/usr/bin/env bash
# GitHub Actions self-hosted runner 등록. root로 실행한다.
#
#   sudo yumcbot-setup-runner <등록토큰>
#
# 등록토큰은 GitHub 저장소 -> Settings -> Actions -> Runners -> New self-hosted runner
# 화면에 표시되는 'AXXXX...' 값이다. 1시간만 유효하고, 등록이 끝나면 버려진다.
# (config.sh가 이 토큰을 영구 자격증명으로 교환해 .credentials에 저장한다.)
#
# 서버는 GitHub로 아웃바운드 연결만 한다. 공인 IP도, 포트 개방도, SSH 키도 필요 없다.
set -Eeuo pipefail

APP_USER=yumcbot
RUNNER_DIR=/opt/yumcbot/runner
REPO_URL="${REPO_URL:-https://github.com/manduk56-hub/YUMCBOT}"
# deploy.yml의 `runs-on: [self-hosted, linux, yumcbot-prod]` 와 반드시 일치해야 한다.
LABELS="${LABELS:-yumcbot-prod}"
PYTHON=python3.12

TOKEN="${1:-}"

if [[ $EUID -ne 0 ]]; then
    echo "root로 실행하세요: sudo yumcbot-setup-runner <등록토큰>" >&2
    exit 1
fi
if [[ -z "$TOKEN" ]]; then
    echo "usage: yumcbot-setup-runner <등록토큰>" >&2
    echo "  GitHub 저장소 -> Settings -> Actions -> Runners -> New self-hosted runner" >&2
    exit 64
fi
if [[ -f "$RUNNER_DIR/.credentials" ]]; then
    echo "이미 러너가 등록돼 있습니다: $RUNNER_DIR" >&2
    echo "다시 등록하려면 먼저 해제하세요:" >&2
    echo "  sudo $RUNNER_DIR/svc.sh stop && sudo $RUNNER_DIR/svc.sh uninstall" >&2
    echo "  sudo -u $APP_USER $RUNNER_DIR/config.sh remove --token <제거토큰>" >&2
    exit 1
fi

echo "==> 최신 러너 버전 확인"
VERSION="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest |
    "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["tag_name"].lstrip("v"))')"
echo "    v$VERSION"

TARBALL="actions-runner-linux-x64-${VERSION}.tar.gz"
URL="https://github.com/actions/runner/releases/download/v${VERSION}/${TARBALL}"

echo "==> 다운로드 및 압축 해제: $RUNNER_DIR"
install -d -o "$APP_USER" -g "$APP_USER" -m 755 "$RUNNER_DIR"
curl -fsSL -o "/tmp/$TARBALL" "$URL"
tar xzf "/tmp/$TARBALL" -C "$RUNNER_DIR"
rm -f "/tmp/$TARBALL"
chown -R "$APP_USER:$APP_USER" "$RUNNER_DIR"

echo "==> 러너 등록 (labels: $LABELS)"
# 봇을 실행하는 계정과 동일하게 돌린다. 그래야 배포 스크립트가 /opt/yumcbot/releases 를
# 별도 권한 없이 쓸 수 있다. 권한이 필요한 건 systemctl 재시작뿐이고, 그건 sudoers로 좁게 열어놨다.
sudo -u "$APP_USER" "$RUNNER_DIR/config.sh" \
    --url "$REPO_URL" \
    --token "$TOKEN" \
    --labels "$LABELS" \
    --name "$(hostname -s)" \
    --work _work \
    --unattended \
    --replace

echo "==> systemd 서비스 등록 (재부팅 후 자동 시작)"
"$RUNNER_DIR/svc.sh" install "$APP_USER"
"$RUNNER_DIR/svc.sh" start

echo
echo "완료. GitHub 저장소 -> Settings -> Actions -> Runners 에서"
echo "'$(hostname -s)' 가 Idle 상태로 보이면 정상입니다."
echo
echo "상태 확인:  sudo $RUNNER_DIR/svc.sh status"
