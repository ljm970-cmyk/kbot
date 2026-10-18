#!/usr/bin/env bash
# kbot 생존 감시 타이머 설치/갱신. 여러 번 실행해도 된다.
#   ./ops/install_watchdog.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(stat -c %U "$APP_DIR")"
changed=0
for unit in kbot-watchdog.service kbot-watchdog.timer; do
    tmp=$(mktemp)
    sed -e "s#/home/ljm970/kbot#$APP_DIR#g" -e "s#^User=ljm970#User=$USER_NAME#" \
        -e "s#^Group=ljm970#Group=$USER_NAME#" "$APP_DIR/ops/$unit" > "$tmp"
    if ! sudo cmp -s "$tmp" "/etc/systemd/system/$unit"; then
        sudo cp "$tmp" "/etc/systemd/system/$unit"
        changed=1
    fi
    rm -f "$tmp"
done
if [ "$changed" = 1 ]; then
    sudo systemctl daemon-reload
fi
sudo systemctl enable --now kbot-watchdog.timer >/dev/null 2>&1
if systemctl is-active --quiet kbot-watchdog.timer; then
    [ "$changed" = 1 ] && echo "  생존 감시 설치/갱신 — 2분마다 확인합니다" || echo "  생존 감시 동작 중"
else
    echo "  생존 감시 타이머를 켜지 못했습니다: sudo systemctl status kbot-watchdog.timer"
fi
