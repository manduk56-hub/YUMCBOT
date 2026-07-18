#!/usr/bin/env bash
# run.ps1과 동일하게 .venv를 구성하고 테스트를 돌린다 (리눅스/로컬 개발용).
# .venv가 없으면 만들고 requirements.txt를 설치한 뒤 unittest를 실행한다.
#
#   ./test.sh                                          # 전체 테스트
#   ./test.sh tests.test_bot                           # 파일 단위
#   ./test.sh tests.test_bot.BotRegressionTests.test_x # 단일 테스트
#
# 파이썬 인터프리터는 PYTHON_BIN 환경변수로 바꿀 수 있다 (기본 python3.12).
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_ROOT/.venv"
PYTHON="$VENV/bin/python"

# .venv가 없거나, 있더라도 의존성이 덜 깔린 껍데기면 새로 구성한다.
if [ ! -x "$PYTHON" ] || ! "$PYTHON" -c "import discord, dotenv" 2>/dev/null; then
    echo "Setting up virtual environment..."
    [ -x "$PYTHON" ] || "${PYTHON_BIN:-python3.12}" -m venv "$VENV"
    "$PYTHON" -m ensurepip -U >/dev/null 2>&1 || true
    "$PYTHON" -m pip install -r "$PROJECT_ROOT/requirements.txt"
fi

cd "$PROJECT_ROOT"
if [ "$#" -gt 0 ]; then
    exec "$PYTHON" -m unittest "$@"
else
    exec "$PYTHON" -m unittest discover -s tests -v
fi
