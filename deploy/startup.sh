#!/bin/bash
# ============================================================
# kbot VM 초기 설정 스크립트
#
# 실행:
#   chmod +x deploy/startup.sh
#   sudo ./deploy/startup.sh
# ============================================================

set -e

echo "========================================"
echo "kbot VM 초기 설정"
echo "========================================"

# 변수
BOT_USER="ubuntu"
APP_DIR="/opt/kbot"
LOG_DIR="/var/log/kbot"
VENV_DIR="$APP_DIR/venv"

# ------------------------------------------------------------
# 1. 시스템 업데이트
# ------------------------------------------------------------
echo "→ 시스템 업데이트..."
apt-get update
apt-get install -y python3-pip python3-venv git

# ------------------------------------------------------------
# 2. 디렉토리 생성
# ------------------------------------------------------------
echo "→ 디렉토리 생성..."
mkdir -p $APP_DIR
mkdir -p $LOG_DIR
mkdir -p $APP_DIR/data/{config,state,orders,fills}

# 권한 설정
chown -R $BOT_USER:$BOT_USER $APP_DIR
chown -R $BOT_USER:$BOT_USER $LOG_DIR

# ------------------------------------------------------------
# 3. 가상환경 (GitHub clone 후 실행 가정)
# ------------------------------------------------------------
echo "→ 가상환경 설정..."
# cd $APP_DIR
# python3 -m venv venv
# source venv/bin/activate
# pip install -r requirements.txt

echo "  (GitHub clone 후 수동 실행: python3 -m venv venv && pip install -r requirements.txt)"

# ------------------------------------------------------------
# 4. .env 파일 확인
# ------------------------------------------------------------
if [ ! -f "$APP_DIR/.env" ]; then
    echo "⚠️  $APP_DIR/.env 파일 없음"
    echo "   scp .env user@vm:$APP_DIR/.env 후 chmod 600 설정 필요"
fi

# ------------------------------------------------------------
# 5. systemd 서비스 등록
# ------------------------------------------------------------
if [ -f "$APP_DIR/deploy/kbot.service" ]; then
    echo "→ systemd 서비스 등록..."
    cp $APP_DIR/deploy/kbot.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable kbot
    echo "  systemctl start kbot 로 시작"
else
    echo "⚠️  kbot.service 파일 없음 (GitHub clone 후 실행)"
fi

# ------------------------------------------------------------
# 완료
# ------------------------------------------------------------
echo ""
echo "========================================"
echo "설정 완료"
echo "========================================"
echo "다음 단계:"
echo "  1. GitHub clone: git clone https://github.com/yourname/kbot.git $APP_DIR"
echo "  2. 의존성 설치: cd $APP_DIR && python3 -m venv venv && pip install -r requirements.txt"
echo "  3. .env 복사: scp .env $BOT_USER@vm:$APP_DIR/.env"
echo "  4. 권한: chmod 600 $APP_DIR/.env"
echo "  5. 시작: sudo systemctl start kbot"
echo "  6. 로그: sudo journalctl -u kbot -f"
echo "========================================"
