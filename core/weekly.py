"""
================================================================
주간 성과 요약 — 매주 토요일 아침 (백업 직전)

한 주 동안의 종목별 흐름을 한 장으로 본다.

  T 변화      주초 → 주말, 막대 그래프
  체결        매수·매도 수량과 금액 (봇 원장 기준)
  실현손익    이동평균 평단 기준 (정산 아카이브)
  평가손익    금요일 종가 기준 보유분
  사이클      이번 주에 끝난 사이클이 있으면 표시

데이터
  정산 아카이브 data/archive/YYYYMM.jsonl  (T·보유·평단·잔금·종가)
  주문 원장 orders.db                       (체결 수량·가격)
================================================================
"""

from __future__ import annotations

from datetime import date, timedelta

from core.tchart import sparkline


def _md(d: str) -> str:
    return f"{int(d[4:6])}/{int(d[6:8])}"


def _archive_rows(state_mgr, ticker: str, start: str, end: str) -> list[dict]:
    """start~end(YYYYMMDD, 포함) 정산 기록. 같은 날은 나중 것."""
    months = sorted({start[:6], end[:6]})
    by_day: dict[str, dict] = {}
    for ym in months:
        for r in state_mgr.read_archive(ym, ticker):
            d = str(r.get("trade_date", ""))
            if start <= d <= end:
                by_day[d] = r
    return [by_day[d] for d in sorted(by_day)]


def _before(state_mgr, ticker: str, start: str) -> dict | None:
    """주 시작 직전 마지막 정산 기록 (최근 두 달 안에서)"""
    s = date(int(start[:4]), int(start[4:6]), int(start[6:8]))
    first = (s.replace(day=1) - timedelta(days=1)).replace(day=1)
    rows = _archive_rows(state_mgr, ticker, first.strftime("%Y%m%d"),
                         (s - timedelta(days=1)).strftime("%Y%m%d"))
    return rows[-1] if rows else None


def week_range(today: date) -> tuple[str, str]:
    """오늘 기준 직전 7일 (오늘 포함하지 않음) — 토요일 아침이면 지난 토~금"""
    end = today - timedelta(days=1)
    start = today - timedelta(days=7)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def ticker_week(state_mgr, registry, ticker: str, start: str, end: str) -> dict | None:
    rows = _archive_rows(state_mgr, ticker, start, end)
    if not rows:
        return None
    prev = _before(state_mgr, ticker, start)
    last = rows[-1]

    trades = [t for t in (registry.trade_history(ticker, days=10_000) if registry else [])
              if start <= t["date"] <= end]
    buys = [t for t in trades if t["side"] == "buy"]
    sells = [t for t in trades if t["side"] == "sell"]

    holdings = int(last.get("holdings", 0) or 0)
    avg = float(last.get("avg_price", 0.0) or 0.0)
    close = float(last.get("close", 0.0) or 0.0)
    return {
        "ticker": ticker,
        "days": len(rows),
        "t_start": float(prev.get("T", 0.0)) if prev else float(rows[0].get("T", 0.0)),
        "t_end": float(last.get("T", 0.0)),
        "t_values": ([float(prev.get("T", 0.0))] if prev else [])
                    + [float(r.get("T", 0.0)) for r in rows],
        "buy_qty": sum(t["qty"] for t in buys),
        "buy_amt": sum(t["amount"] for t in buys),
        "sell_qty": sum(t["qty"] for t in sells),
        "sell_amt": sum(t["amount"] for t in sells),
        "realized": sum(float(r.get("realized_pl", 0.0) or 0.0) for r in rows),
        "holdings": holdings, "avg": avg, "close": close,
        "unrealized": holdings * (close - avg) if holdings and close and avg else 0.0,
        "cycles_closed": sum(1 for r in rows if r.get("cycle_closed")),
        "reverse_days": sum(1 for r in rows if r.get("mode") == "reverse"),
        "mode": last.get("mode", "normal"),
    }


def weekly_report(state_mgr, registry, today: date | None = None) -> str:
    today = today or date.today()
    start, end = week_range(today)
    tickers = state_mgr.list_tickers()
    L = [f"📅 주간 요약 — {_md(start)} ~ {_md(end)}", ""]

    weeks = []
    for t in tickers:
        try:
            w = ticker_week(state_mgr, registry, t, start, end)
        except Exception as e:                       # 한 종목 실패가 전체를 막지 않게
            L.append(f"⚠️ [{t}] 집계 실패: {e}")
            continue
        if w is None:
            L += [f"💎 [{t}]", "  이번 주 정산 기록 없음", ""]
            continue
        weeks.append(w)
        dt = w["t_end"] - w["t_start"]
        L.append(f"💎 [{t}] {w['days']}거래일"
                 + (" · 🔄 리버스" if w["mode"] == "reverse" else ""))
        flow = sparkline(w["t_values"]) if len(w["t_values"]) >= 2 else ""
        L.append(f"  📈 T {w['t_start']:.1f} → {w['t_end']:.1f} ({dt:+.1f})"
                 + (f"  {flow}" if flow else ""))
        if w["buy_qty"] or w["sell_qty"]:
            parts = []
            if w["buy_qty"]:
                parts.append(f"🔴 매수 {w['buy_qty']}주 ${w['buy_amt']:,.2f}")
            if w["sell_qty"]:
                parts.append(f"🔵 매도 {w['sell_qty']}주 ${w['sell_amt']:,.2f}")
            L.append("  " + " · ".join(parts))
        else:
            L.append("  체결 없음")
        if w["realized"]:
            L.append(f"  💵 실현손익 ${w['realized']:+,.2f} (이동평균 기준)")
        if w["holdings"]:
            pct = (w["close"] / w["avg"] - 1) * 100 if w["avg"] else 0.0
            L.append(f"  📦 {w['holdings']}주 @${w['avg']:.2f} · 종가 ${w['close']:.2f}"
                     f" · 평가 ${w['unrealized']:+,.2f} ({pct:+.1f}%)")
        else:
            L.append("  📦 보유 없음")
        if w["cycles_closed"]:
            L.append(f"  🏁 이번 주 사이클 {w['cycles_closed']}회 완료 — /stats")
        L.append("")

    if len(weeks) >= 2:
        L.append("━━ 합계 ━━")
        L.append(f"  🔴 매수 ${sum(w['buy_amt'] for w in weeks):,.2f}"
                 f" · 🔵 매도 ${sum(w['sell_amt'] for w in weeks):,.2f}")
        L.append(f"  💵 실현 ${sum(w['realized'] for w in weeks):+,.2f}"
                 f" · 평가 ${sum(w['unrealized'] for w in weeks):+,.2f}")
        L.append("")

    if not tickers:
        L.append("운용 중인 종목이 없습니다.")
    return "\n".join(L).rstrip()
