#!/usr/bin/env bash
# kbot 재시작.
#
# 텔레그램은 한 토큰에 한 폴링만 허용한다. 옛 프로세스가 살아 있는 채로
# 새로 띄우면 Conflict 가 나고, 실운영에서는 주문이 두 번 나갈 수 있다.
# 완전히 죽은 것을 확인한 뒤에만 새로 시작한다.
set -e
cd "$(dirname "$0")"

pkill -f "venv/bin/python main.py" 2>/dev/null || true
for _ in $(seq 1 15); do
    pgrep -f "venv/bin/python main.py" >/dev/null || break
    sleep 1
done
if pgrep -f "venv/bin/python main.py" >/dev/null; then
    echo "종료되지 않아 강제 종료합니다."
    pkill -9 -f "venv/bin/python main.py" || true
    sleep 3
fi

mkdir -p logs
nohup env KBOT_DRY_RUN="${KBOT_DRY_RUN:-false}" venv/bin/python main.py \
    > logs/kbot.log 2>&1 &
echo "시작 (PID $!)"
sleep 10
grep -E "스케줄러 시작|일정 등록|Conflict|오류" logs/kbot.log | tail -5 || true
echo "--- 실행 중인 프로세스 ---"
pgrep -af "venv/bin/python main.py" || echo "없음"
