"""
================================================================
원화 환산 — 입금 안내용

원화주문(통합증거금) 계좌는 원화를 넣어 두면 그 몫까지 매수에 쓴다.
다만 증권사는 원화를 기준환율보다 낮게 평가한다.

  실측 2026-10-24  원화 주문설정금 1,030,000원 → $753.33 평가
                   기준환율 1,314.10  → 실효 1,367.3 (약 4% 할인)

그래서 "$X 부족" 을 원화로 안내할 때는 기준환율 × 1.05 로 넉넉히
잡고 천 원 단위로 올린다.
================================================================
"""

from __future__ import annotations

import math
from datetime import date, timedelta

KRW_EVAL_FACTOR = 1.05


def latest_rate(state_mgr, today: date | None = None) -> float:
    """정산 때 기록한 가장 최근 기준환율. 없으면 0."""
    today = today or date.today()
    months = sorted({today.strftime("%Y%m"),
                     (today.replace(day=1) - timedelta(days=1)).strftime("%Y%m")})
    best_day, best = "", 0.0
    for ym in months:
        try:
            rows = state_mgr.read_archive(ym)
        except TypeError:
            rows = []
            for t in state_mgr.list_tickers(active_only=False):
                rows += state_mgr.read_archive(ym, t)
        for r in rows:
            v = float(r.get("usd_krw") or 0.0)
            d = str(r.get("trade_date", ""))
            if v > 0 and d >= best_day:
                best_day, best = d, v
    return best


def krw_for_usd(usd: float, rate: float) -> int:
    if usd <= 0 or rate <= 0:
        return 0
    return int(math.ceil(usd * rate * KRW_EVAL_FACTOR / 1000.0) * 1000)


def krw_text(usd: float, rate: float) -> str:
    """' ≈ ₩235,000' — 환율을 모르면 빈 문자열"""
    w = krw_for_usd(usd, rate)
    return f" ≈ ₩{w:,}" if w else ""


def safe_rate(state_mgr) -> float:
    """주문·요약 흐름에서 쓰는 환율. 어떤 오류도 0 으로 (원화 표시만 빠진다)."""
    try:
        return latest_rate(state_mgr)
    except Exception:
        return 0.0
