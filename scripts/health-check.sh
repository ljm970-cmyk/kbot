#!/bin/bash
# scripts/health-check.sh
# 상태 진단

echo "=== 무한매수법 V4.0 상태 진단 ==="

# Docker 상태
echo "[Docker]"
docker ps --format "table {{.Names}}\t{{.Status}}"

# 디스크
echo ""
echo "[디스크]"
df -h | grep -E "(Filesystem|/dev/)"

# 메모리
echo ""
echo "[메모리]"
free -h

# 최근 로그
echo ""
echo "[최근 로그]"
tail -n 20 logs/app.log 2>/dev/null || echo "로그 파일 없음"

# DB 상태
echo ""
echo "[DB 크기]"
ls -lh data/*.db 2>/dev/null || echo "DB 파일 없음"
