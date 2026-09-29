#!/usr/bin/env bash
# ================================================================
# kbot 배포
#
#   ./deploy.sh                    받기 → 테스트 → 반영 → 재시작 → 확인 → 커밋
#   ./deploy.sh "커밋 메시지"
#
# 받는 곳
#   ~/kbot.zip 이 있으면 그 zip, 없으면 GitHub (git pull)
#
# 안전장치
#   - 새 코드는 임시 폴더에서 테스트부터 돌린다. 실패하면 실제 코드는
#     건드리지 않고 멈춘다 (봇은 기존 코드로 계속 돈다).
#   - 반영한 zip 은 ~/kbot.zip.applied-<시각> 으로 치운다. 예전에 두 번
#     옛 zip 을 다시 풀었던 사고가 다시 나지 않게.
#   - 재시작 뒤 로그에서 실계좌 / 모의투자 / DRY RUN 표시를 직접 보여준다.
# ================================================================
set -euo pipefail
cd "$(dirname "$0")"
HOME_DIR="$(pwd)"
ZIP="$HOME/kbot.zip"
SERVICE="kbot"
MSG="${1:-배포 $(date '+%Y-%m-%d %H:%M')}"
PY="$HOME_DIR/venv/bin/python"
[ -x "$PY" ] || PY="python3"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
step()  { printf '\n\033[1m▶ %s\033[0m\n' "$*"; }

run_tests() {   # $1 = 테스트할 폴더
    if ( cd "$1" && "$PY" tests/run_all.py ) > /tmp/kbot_tests.txt 2>&1; then
        tail -2 /tmp/kbot_tests.txt
        return 0
    fi
    tail -4 /tmp/kbot_tests.txt
    return 1
}

# ----------------------------------------------------------------
step "1. 새 코드 받기"
# ----------------------------------------------------------------
if [ -f "$ZIP" ]; then
    SIZE=$(stat -c %s "$ZIP")
    WHEN=$(stat -c %y "$ZIP" | cut -d. -f1)
    echo "  zip: $ZIP ($SIZE 바이트, $WHEN)"

    TMP=$(mktemp -d)
    trap 'rm -rf "$TMP"' EXIT
    unzip -q -o "$ZIP" -d "$TMP"
    if [ -e "$TMP/.env" ] || [ -d "$TMP/data/state" ]; then
        red "  zip 에 .env 나 장부가 들어 있습니다. 덮어쓰지 않고 멈춥니다."
        exit 1
    fi

    step "2. 테스트 (임시 폴더)"
    if ! run_tests "$TMP"; then
        red "  테스트 실패 — 실제 코드는 바꾸지 않았습니다. 봇은 기존 코드로 계속 돕니다."
        red "  자세한 내용: cat /tmp/kbot_tests.txt"
        exit 1
    fi

    step "3. 반영"
    cp -a "$TMP"/. "$HOME_DIR"/
    mv "$ZIP" "$ZIP.applied-$(date +%Y%m%d-%H%M%S)"
    ls -1t "$HOME"/kbot.zip.applied-* 2>/dev/null | tail -n +4 | xargs -r rm -f   # 최근 3개만 보관
    echo "  반영 완료. zip 은 kbot.zip.applied-* 로 치웠습니다."
else
    echo "  ~/kbot.zip 없음 → GitHub 에서 받습니다"
    BEFORE=$(git rev-parse HEAD)
    git pull --ff-only
    if [ "$(git rev-parse HEAD)" = "$BEFORE" ]; then
        echo "  새로 받은 변경이 없습니다."
    fi

    step "2. 테스트"
    if ! run_tests "$HOME_DIR"; then
        red "  테스트 실패 — 받기 전 상태로 되돌립니다."
        git reset -q --hard "$BEFORE"
        red "  자세한 내용: cat /tmp/kbot_tests.txt"
        exit 1
    fi
    step "3. 반영"
    echo "  git pull 로 반영됨"
fi

# ----------------------------------------------------------------
step "4. 재시작"
# ----------------------------------------------------------------
SINCE=$(date '+%Y-%m-%d %H:%M:%S')
sudo systemctl restart "$SERVICE"
for _ in $(seq 1 20); do
    sleep 1
    sudo journalctl -u "$SERVICE" --since "$SINCE" --no-pager 2>/dev/null \
        | grep -qE "일정 등록|스케줄러 시작" && break
done

if ! systemctl is-active --quiet "$SERVICE"; then
    red "  봇이 떠 있지 않습니다!"
    sudo journalctl -u "$SERVICE" --since "$SINCE" --no-pager | tail -20
    exit 1
fi

# ----------------------------------------------------------------
step "5. 모드 확인"
# ----------------------------------------------------------------
MODE=$(sudo journalctl -u "$SERVICE" --since "$SINCE" --no-pager \
       | grep -oE "실계좌 · DRY RUN\(주문 미전송\)|모의투자 · DRY RUN\(주문 미전송\)|실계좌|모의투자" \
       | head -1 || true)
case "$MODE" in
    *"DRY RUN"*) printf '  \033[33m%s\033[0m — 주문이 나가지 않습니다\n' "$MODE" ;;
    "실계좌")    green "  실계좌 — 실제 주문이 나갑니다" ;;
    "")          red   "  모드 표시를 찾지 못했습니다. 로그를 확인하세요:"
                 echo  "  sudo journalctl -u $SERVICE --since \"$SINCE\" --no-pager | head -30" ;;
    *)           echo  "  $MODE" ;;
esac
ERRS=$(sudo journalctl -u "$SERVICE" --since "$SINCE" --no-pager | grep -cE "Traceback|\[ERROR\]" || true)
[ "$ERRS" -gt 0 ] && red "  기동 로그에 오류 $ERRS 건 — sudo journalctl -u $SERVICE --since \"$SINCE\""

# ----------------------------------------------------------------
step "6. 커밋"
# ----------------------------------------------------------------
if [ -n "$(git status --porcelain)" ]; then
    git add -A
    git commit -q -m "$MSG"
    echo "  $(git log --oneline -1)"
    if git push -q 2>/tmp/kbot_push.txt; then
        green "  GitHub 에 올렸습니다."
    else
        red "  push 실패 — 코드는 반영됐고 봇도 돌고 있습니다. 나중에 git push 하세요."
        cat /tmp/kbot_push.txt
    fi
else
    echo "  바뀐 파일 없음 (이미 커밋된 상태)"
fi

green "
배포 완료 — $(git log --oneline -1)"
