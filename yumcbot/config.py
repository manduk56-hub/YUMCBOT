"""경로·환경변수와 게임 튜닝 상수.

이 모듈은 아무것도 import 하지 않는다(표준 라이브러리 + dotenv 제외). 의존 방향의
최하단이라, 여기서 다른 yumcbot 모듈을 부르는 순간 순환 import가 시작된다.

값을 바꿀 때: 게임 규칙 상수는 activity_logs의 규칙 스냅샷(game_settings_snapshot)에
값으로 실려 나가므로, 여기 있는 것은 사후에 "그때 얼마였는지"를 추적할 수 있다.
반대로 close_round_record에 하드코딩된 규칙은 못 그런다 — 그래서 튜닝 대상은
가급적 이 파일로 끌어올린다.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


# 저장소 루트. 이 파일이 yumcbot/ 아래에 있으므로 한 단계 더 올라가야 한다.
# (bot.py에 있을 때는 .parent 하나였다. .env / data / assets 가 전부 루트 기준이고,
#  _detect_bot_version()은 이 디렉터리 이름을 릴리스 SHA로 읽는다.)
BASE_DIR = Path(__file__).resolve().parent.parent

# 배포 환경(systemd)이 넘겨준 실제 환경변수가 우선이고, 없으면 로컬 .env로 채운다.
load_dotenv(BASE_DIR / ".env")

# 운영 서버는 DB를 코드 디렉터리 밖(/var/lib/yumcbot/data)에 두어 배포로 덮이지 않게 한다.
DATA_DIR = Path(os.getenv("YUMCBOT_DATA_DIR") or (BASE_DIR / "data")).resolve()
DB_PATH = DATA_DIR / (os.getenv("YUMCBOT_DB_NAME") or "event_game.sqlite3")
KST_LABEL = "Asia/Seoul"


def _detect_bot_version() -> str:
    """활동 로그에 남길 코드 버전. '어느 코드로 돈 게임인가'를 사후에 알기 위한 것.

    배포는 릴리스를 /opt/yumcbot/releases/<sha>/ 에 풀고 current 링크를 갈아끼운다.
    따라서 운영에서는 코드 디렉터리 이름이 곧 커밋 SHA다 (deploy/deploy.sh 참고).
    로컬 체크아웃에서는 그렇지 않으므로 'dev'로 둔다. 부팅 시 한 번만 계산한다.
    """
    override = os.getenv("YUMCBOT_VERSION")
    if override:
        return override
    name = BASE_DIR.name
    if len(name) >= 7 and all(char in "0123456789abcdef" for char in name):
        return name[:12]
    return "dev"


BOT_VERSION = _detect_bot_version()
DEFUSE_COLORS = [
    {"id": "red", "name": "빨강", "emoji": "🟥"},
    {"id": "blue", "name": "파랑", "emoji": "🟦"},
    {"id": "green", "name": "초록", "emoji": "🟩"},
]
DEFUSE_COLOR_BY_ID = {color["id"]: color for color in DEFUSE_COLORS}
# 배포 전에 열린 구방식 라운드의 버튼 응답 문구를 위해서만 남긴다.
DEFUSE_COLOR_BY_ID.update(
    {
        "black": {"id": "black", "name": "검정", "emoji": "⬛"},
        "white": {"id": "white", "name": "흰색", "emoji": "⬜"},
    }
)
CUTTING_LEVEL_CHOICES = [str(index) for index in range(0, 7)]
CUTTING_REWARD_POINTS = {
    0: 3,
    1: 3,
    2: 1,
    3: 1,
    4: 1,
    5: 2,
    6: 6,
}
CUTTING_SUCCESS_RATES = {
    0: 0.9,
    1: 0.9,
    2: 0.6,
    3: 0.6,
    4: 0.5,
    5: 0.15,
}
TOP3_NONE_CHOICE = "__none__"
UNIQUE_NUMBER_CHOICES = [str(index) for index in range(1, 16)]
BASEBALL_TOTAL_PITCHES = 3
BASEBALL_SWING_SECONDS = 3.0
BASEBALL_RESULT_SECONDS = 7.0
BASEBALL_WINDUP_SECONDS = 3.0
BASEBALL_MISSED_SWING_SECONDS = 7.0
BASEBALL_BUTTON_GRID_SIZE = 3
BASEBALL_BUTTON_SLOT_COUNT = BASEBALL_BUTTON_GRID_SIZE**2
BASEBALL_RANK_POINTS = {1: 3, 2: 2, 3: 1}
BASEBALL_IMAGE_FILENAME = "baseball-batter.png"
BASEBALL_IMAGE_PATH = BASE_DIR / "assets" / BASEBALL_IMAGE_FILENAME
TRUST_REGISTRATION_DEFAULT_MINUTES = 2
TRUST_DECISION_SECONDS = 180
MATCH_HISTORY_ROUNDS = 3  # 메인게임 1:1 매칭에서 이 라운드 수만큼 직전 짝을 회피 (trust·sand 공유)
TRUST_OUTCOME_LABELS = {
    "mutual": "신뢰",
    "single": "단독 배신",
    "double": "동시 배신",
    "incomplete": "미선택",
}
# 게임 목록(MAIN_GAME_TYPES / MINI_GAME_TYPES / GAME_NAMES)은 yumcbot.games.registry에
# 있다. 여기 두면 config가 최하단 레이어라는 성질이 깨진다 — 레지스트리는 정산 훅을
# 들고 있어서 discord에 의존하는데, config는 아무것도 import 하지 않아야 한다.
BALANCE_VOTE_CHOICES = [("A 선택", "A"), ("B 선택", "B")]
ANONYMOUS_EMBED_COLORS = (
    # 가까운 번호끼리 색상·명도 차이가 크게 나도록 고대비 순서로 배치한다.
    0xD1382A,
    0x0AD10A,
    0x940CF5,
    0x31C4F5,
    0xD1B00A,
    0xAD3D91,
    0x3DAD75,
    0x3172F5,
    0xAD753D,
    0xF556F5,
    0xF50C6D,
    0x3D6CAD,
    0xF5810C,
    0x74AD23,
    0xAD3D59,
    0xF50CA7,
    0x8D49D1,
    0x0AD1D1,
    0x2AD162,
    0xF50C0C,
)
ANONYMOUS_GUIDE_TEXT = (
    "아래 버튼으로 익명 발언을 남겨주세요. 일반 메시지는 익명 보호를 위해 삭제됩니다.\n"
    "발언 횟수 제한은 없으며, 같은 라운드에서는 계속 같은 색상으로 표시됩니다."
)
SAND_NUMBER_CHOICES = [str(index) for index in range(1, 11)]
SAND_PLAY_SECONDS = 600
SAND_RULES_TEXT = (
    "**1** ─ 각 참여자가 1~10 중 모래성이 무너질 횟수를 정합니다.\n"
    "**2** ─ 두 수의 차이만큼은 안전하게 덜 수 있고, 그 횟수를 초과해서 더는 순간 무너집니다. "
    "(예: 9와 7이면 차이가 2 → 2번까지는 성공, 3번째에 붕괴. 차이가 0이면 첫 덜기에서 붕괴)\n"
    "**3** ─ 참여자는 번갈아가며 `포기하기` 또는 `모래 덜기`를 결정합니다.\n"
    "**4** ─ 숫자를 안 내거나 제한 시간 안에 끝내지 못하면 마감 때 랜덤으로 자동 진행됩니다.\n\n"
    "> 🏳️ 둘 다 포기 선택 시: **서로 -3점**\n"
    "> ⚖️ 한 명이 포기(A), 다른 한 명이 모래 덜기(B) 선택 시:\n"
    "> 　├ 덜기 성공: A는 **-3점**, B는 **+5점**\n"
    "> 　└ 무너짐: B는 **-3점**, A는 **+2점**\n"
    "> 💥 서로 덜기를 하다 무너진 경우: 그 턴의 사람이 **-3점**"
)
SAND_OUTCOME_LABELS = {
    "mutual_quit": "둘 다 포기",
    "quit_success": "포기 후 상대 덜기 성공",
    "quit_collapse": "포기 후 상대 모래성 붕괴",
    "plain_collapse": "일반 모래성 붕괴",
}
# 가라앉는 배: 협상 → 실행 2단계 메인게임. rounds.type CHECK 제약을 못 고쳐서
# type='sand' + extra_json.mode='bail'로 저장한다 (grid→unique_number와 같은 방식).
#
# 협상하는 것은 **구조 보상금의 배분**이고(수락하면 확정), 누가 몇 번 퍼낼지는 아무도 약속하지 않는다.
# 그래서 배신은 "보상은 챙기고 노동은 덜 하기"가 된다.
BAIL_REWARD_POOL = 10  # 배가 뜨면 두 사람이 나눠 받는 구조 보상금
BAIL_REWARD_CHOICES = [str(index) for index in range(BAIL_REWARD_POOL + 1)]  # 0~10
BAIL_FAILED_REWARD_POOL = 6  # 협상 결렬 시 감액된 보상금 (둘이 균등 분배)
BAIL_REQUIRED_SCOOPS = 6  # 배가 뜨는 데 필요한 두 사람 합계 퍼내기 횟수
BAIL_SCOOP_CHOICES = [str(index) for index in range(BAIL_REQUIRED_SCOOPS + 1)]  # 0~6
BAIL_SCOOP_COST = 1  # 퍼내기 1회당 차감
# 침몰 페널티는 보상 몫에 비례한다. 많이 받기로 한 쪽이 더 크게 잃어야
# "협상에서 욕심낸 만큼 침몰 리스크를 진다"가 성립하고, 적게 받은 쪽에 배신 카드가 생긴다.
BAIL_SINK_PENALTY_HIGH = 4  # 보상 몫이 많은 쪽
BAIL_SINK_PENALTY_LOW = 2  # 보상 몫이 적은 쪽
BAIL_SINK_PENALTY_EVEN = 3  # 몫이 같을 때 (결렬 포함)
BAIL_NEGOTIATION_MAX_OFFERS = 3  # 교대 제안 최대 횟수
BAIL_DEFAULT_NEGOTIATION_MINUTES = 5
BAIL_DEFAULT_EXECUTION_MINUTES = 3
BAIL_FALLBACK_SCOOPS = BAIL_REQUIRED_SCOOPS // 2  # 실행 단계 미제출 시의 자동 제출값
BAIL_OUTCOME_LABELS = {
    "survived": "생존",
    "sunk": "침몰",
}
EVENT_STATUS_LABELS = {
    "recruiting": "참가 모집 중",
    "active": "게임 진행 중",
    "closed": "종료",
}
# 참가자가 홀수라 짝이 없는 사람이 생겼을 때, 새 참가자가 긴급 투입되면 전체 마감이 이만큼 밀린다.
EMERGENCY_EXTENSION_SECONDS = 60
AUTO_CLOSE_WARNING_SECONDS = 10
AUTO_CLOSE_COUNTDOWN_SECONDS = (5, 4, 3, 2, 1)
AFK_TICK_SECONDS = 60
AFK_COIN_PER_TICK = 1
SCORE_REVEAL_COST = 30
ROULETTE_CHALLENGE_SECONDS = 300
ROULETTE_BETTING_SECONDS = 180
# 자기 차례에 방아쇠를 안 당기면 자동 패배. 없으면 예치금이 영구 동결되고
# 동시 1결투 제한 때문에 상대까지 묶인다(배포도 check_idle에서 영구 차단된다).
ROULETTE_TURN_SECONDS = 180
ROULETTE_MIN_BET = 1
ROULETTE_MAX_BET = 3
ROULETTE_DAILY_LIMIT = 3
ROULETTE_PAIR_DAILY_LIMIT = 1
# 최소 베팅액도 못 내는 유저(=신용 유저)는 강제로 1점을 걸고, 이기면 점수가 오르지 않는
# 대신 연승 스택이 쌓인다. 스택이 목표치에 닿으면 보상 점수를 받고 스택은 0으로 돌아간다.
ROULETTE_STACK_GOAL = 2
ROULETTE_STACK_REWARD = 2
