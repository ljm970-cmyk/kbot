#!/bin/bash
# scripts/setup.sh
# VM 최초 설치 (한번만 실행)

set -e

echo "========================================="
echo "무한매수법 V4.0 - VM 초기 설치"
echo "========================================="

# 1. 기본 패키지
sudo apt update
sudo apt install -y git docker.io docker-compose sqlite3

# 2. Docker 권한
sudo usermod -aG docker $USER
sudo systemctl enable docker

# 3. 타임존
sudo timedatectl set-timezone Asia/Seoul

# 4. 프로젝트 clone
cd ~
if [ ! -d "kbot" ]; then
    git clone https://github.com/ljm970-cmyk/kbot.git
fi
cd kbot

# 5. 디렉토리 생성
mkdir -p data logs

# 6. DB 초기화
sqlite3 data/infinite_buy.db < database/schema.sql

# 7. .env 파일 생성 안내
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "⚠️  .env 파일을 수정하세요: nano .env"
fi

echo "========================================="
echo "설치 완료. 다음 단계:"
echo "1. nano .env  (실제 키 입력)"
echo "2. docker-compose up -d"
echo "========================================="
