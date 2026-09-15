#!/usr/bin/env bash
# ============================================================
# kbot VM 설치 / 갱신 스크립트
#
#   ./deploy/setup.sh            설치 또는 갱신
#   ./deploy/setup.sh --service  systemd 등록까지
#
# sudo 로 실행하지 않는다. 패키지 설치 단계에서만 sudo 를 쓴다.
# 프로젝트는 이 스크립트가 있는 위치의 상위 디렉토리를 기준으로 한다.
# ============================================================

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$APP_DIR/venv"
PY="${PYTHON:-python3}"
INSTALL_SERVICE=0

for arg in "$@"; do
    case "$arg" in
        --service) INSTALL_SERVICE=1 ;;
        *) echo "알 수 없는 옵션: $arg"; exit 1 ;;
    esac
done

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
ok() { printf '\033[32m  ✓ %s\033[0m\n' "$*"; }

say "kbot 설치  ($APP_DIR)"

# ------------------------------------------------------------
# 1. 시스템 패키지
# ------------------------------------------------------------
if ! command -v "$PY" >/dev/null; then
    say "1. 파이썬 설치"
    sudo apt-get update -qq
    sudo apt-get install -y python3 python3-pip python3-venv
fi
PY_VER=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
say "1. 파이썬 $PY_VER"
"$PY" - <<'EOF'
import sys
if sys.version_info < (3, 11):
    sys.exit("파이썬 3.11 이상이 필요합니다 (zoneinfo, timezone 처리)")
EOF
ok "버전 확인"

# ------------------------------------------------------------
# 2. 타임존
# ------------------------------------------------------------
say "2. 타임존"
CUR_TZ=$(timedatectl show -p Timezone --value 2>/dev/null || echo unknown)
if [ "$CUR_TZ" != "Asia/Seoul" ]; then
    warn "현재 타임존: $CUR_TZ"
    sudo timedatectl set-timezone Asia/Seoul && ok "Asia/Seoul 로 변경"
else
    ok "Asia/Seoul"
fi

# ------------------------------------------------------------
# 3. 가상환경
# ------------------------------------------------------------
say "3. 가상환경"
if [ ! -d "$VENV_DIR" ]; then
    "$PY" -m venv "$VENV_DIR"
    ok "생성: $VENV_DIR"
else
    ok "기존 사용: $VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
ok "의존성 설치 완료"

# ------------------------------------------------------------
# 4. 디렉토리
# ------------------------------------------------------------
say "4. 디렉토리"
mkdir -p "$APP_DIR"/data/{config,state,orders,fills,archive,locks}
mkdir -p "$APP_DIR/logs"
ok "data/ logs/ 준비"

# ------------------------------------------------------------
# 5. .env
# ------------------------------------------------------------
say "5. 환경설정"
if [ ! -f "$APP_DIR/.env" ]; then
    if [ -f "$APP_DIR/.env.example" ]; then
        cp "$APP_DIR/.env.example" "$APP_DIR/.env"
        chmod 600 "$APP_DIR/.env"
        warn ".env 를 생성했습니다. 키를 채워 넣으세요: $APP_DIR/.env"
    else
        warn ".env 가 없습니다."
    fi
else
    chmod 600 "$APP_DIR/.env"
    ok ".env 확인 (권한 600)"
fi

# ------------------------------------------------------------
# 6. 테스트
# ------------------------------------------------------------
say "6. 테스트"
FAILED=0
for t in "$APP_DIR"/tests/test_*.py; do
    name=$(basename "$t" .py)
    if out=$("$VENV_DIR/bin/python" "$t" 2>&1); then
        printf '  %-24s %s\n' "$name" "$(echo "$out" | tail -1)"
    else
        printf '  \033[31m%-24s 실패\033[0m\n' "$name"
        echo "$out" | grep FAIL || true
        FAILED=1
    fi
done
[ "$FAILED" -eq 0 ] && ok "전부 통과" || warn "실패한 테스트가 있습니다"

# ------------------------------------------------------------
# 7. systemd
# ------------------------------------------------------------
if [ "$INSTALL_SERVICE" -eq 1 ]; then
    say "7. systemd 등록"
    SERVICE_SRC="$APP_DIR/deploy/kbot.service"
    TMP=$(mktemp)
    sed -e "s#/home/ljm970/kbot#$APP_DIR#g" \
        -e "s#^User=.*#User=$(id -un)#" \
        -e "s#^Group=.*#Group=$(id -gn)#" \
        "$SERVICE_SRC" > "$TMP"
    sudo cp "$TMP" /etc/systemd/system/kbot.service
    rm -f "$TMP"
    sudo systemctl daemon-reload
    ok "등록 완료 (경로·사용자 자동 치환)"
    echo "     sudo systemctl enable --now kbot"
    echo "     sudo journalctl -u kbot -f"
fi

# ------------------------------------------------------------
say "다음 단계"
cat <<EOF
  1. .env 에 키 입력
       KIWOOM_APP_KEY / KIWOOM_APP_SECRET
       TELEGRAM_BOT_TOKEN / TELEGRAM_ADMIN_ID

  2. 기동 점검 (주문 없음, 시세·예수금만 확인)
       $VENV_DIR/bin/python main.py --check

     SOXL 거래소구분이 틀리면 여기서 시세가 0으로 나옵니다.
     그때는 아래로 실제 값을 확인해 kiwoom/constants.py 의
     EXCHANGE_MAP 을 고치세요.
       $VENV_DIR/bin/python -c "
import asyncio
from config.settings import ConfigLoader
from kiwoom.api_client import KiwoomAPIClient
async def m():
    async with KiwoomAPIClient(ConfigLoader.load().kiwoom) as api:
        print('SOXL', await api.resolve_exchange('SOXL'))
asyncio.run(m())"

  3. 모의투자로 배관 확인
       $VENV_DIR/bin/python main.py --mock --check

  4. 주문 미전송 모드로 며칠 관찰
       $VENV_DIR/bin/python main.py --dry-run

  5. 실운영
       sudo systemctl enable --now kbot
EOF
