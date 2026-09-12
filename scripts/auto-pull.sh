#!/bin/bash
# scripts/auto-pull.sh
# GitHub 자동 동기화 + Docker 재시작

cd ~/kbot || exit 1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] GitHub 동기화 시작"

# 변경사항 확인
git fetch origin main

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "변경사항 발견, 업데이트 중..."
    git pull origin main
    
    # Docker 재빌드 및 재시작
    docker-compose down
    docker-compose up -d --build
    
    echo "업데이트 완료 및 서비스 재시작"
else
    echo "변경사항 없음"
fi
