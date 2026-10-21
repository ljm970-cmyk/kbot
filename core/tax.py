"""
================================================================
연간 실현손익 정리 (해외주식 양도소득 참고용)

봇 원장의 체결 기록으로 한 해 동안 판 주식의 손익을 계산한다.

  취득가   선입선출 (먼저 산 물량부터 판 것으로 본다)
  수수료   계좌 수수료율로 추정 (매수 수수료는 취득가에, 매도 수수료는 따로)
  원화     매도한 날 정산 때 기록한 기준환율. 없으면 가장 가까운 기록.
  세액     (원화 손익 − 250만원 기본공제) × 22%  — 참고용 추정

이 계산은 봇 장부만 본다. 같은 계좌에서 직접 사고판 것, 다른 계좌,
배당·환차손익은 빠져 있다. 신고는 증권사가 주는 양도소득 자료로 한다.
================================================================
"""

from __future__ import annotations

import csv
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

BASIC_DEDUCTION_KRW = 2_500_000
TAX_RATE = 0.22


@dataclass
class Sale:
    date: str
    ticker: str
    qty: int
    price: float
    proceeds: float
    cost: float
    fee: float
    usd_krw: float = 0.0

    @property
    def gain(self) -> float:
        return self.proceeds - self.cost - self.fee

    @property
    def gain_krw(self) -> float:
        return self.gain * self.usd_krw if self.usd_krw else 0.0


@dataclass
class TickerTax:
    ticker: str
    sales: list[Sale] = field(default_factory=list)
    unmatched_qty: int = 0          # 원장에 매수 기록 없이 판 수량 (취득가 0 으로 계산하지 않음)

    @property
    def proceeds(self) -> float:
        return sum(s.proceeds for s in self.sales)

    @property
    def cost(self) -> float:
        return sum(s.cost for s in self.sales)

    @property
    def fee(self) -> float:
        return sum(s.fee for s in self.sales)

    @property
    def gain(self) -> float:
        return sum(s.gain for s in self.sales)

    @property
    def gain_krw(self) -> float:
        return sum(s.gain_krw for s in self.sales)


def fifo_sales(rows: list[dict], ticker: str, year: str, fee_rate: float) -> TickerTax:
    """rows: OrderRegistry.filled_rows() — 오래된 것부터"""
    lots: deque = deque()          # [qty, unit_cost_with_fee]
    out = TickerTax(ticker=ticker)
    for r in rows:
        qty, px = int(r["filled_qty"]), float(r["filled_price"])
        if r["side"] == "buy":
            lots.append([qty, px * (1 + fee_rate)])
            continue
        need, cost = qty, 0.0
        while need > 0 and lots:
            take = min(need, lots[0][0])
            cost += take * lots[0][1]
            lots[0][0] -= take
            need -= take
            if lots[0][0] == 0:
                lots.popleft()
        if not str(r["trade_date"]).startswith(year):
            continue
        sold = qty - need
        if need:
            out.unmatched_qty += need
        if sold <= 0:
            continue
        proceeds = sold * px
        out.sales.append(Sale(date=r["trade_date"], ticker=ticker, qty=sold, price=px,
                              proceeds=proceeds, cost=cost, fee=proceeds * fee_rate))
    return out


def fx_by_date(state_mgr, ticker: str, year: str) -> dict[str, float]:
    rates = {}
    for m in range(1, 13):
        for r in state_mgr.read_archive(f"{year}{m:02d}", ticker):
            v = float(r.get("usd_krw") or 0.0)
            if v > 0 and r.get("trade_date"):
                rates[str(r["trade_date"])] = v
    return rates


def _nearest(rates: dict[str, float], day: str) -> float:
    if not rates:
        return 0.0
    if day in rates:
        return rates[day]
    before = [d for d in rates if d <= day]
    return rates[max(before)] if before else rates[min(rates)]


def yearly(state_mgr, registry, year: str, fallback_rate: float = 0.0) -> list[TickerTax]:
    out = []
    tickers = sorted(set(state_mgr.list_tickers(active_only=False)))
    for t in tickers:
        cfg = state_mgr.get_config(t) or {}
        fee = float(cfg.get("fee_rate", 0.0) or 0.0)
        tt = fifo_sales(registry.filled_rows(t), t, year, fee)
        if not tt.sales and not tt.unmatched_qty:
            continue
        rates = fx_by_date(state_mgr, t, year)
        for s in tt.sales:
            s.usd_krw = _nearest(rates, s.date) or fallback_rate
        out.append(tt)
    return out


def format_yearly(year: str, items: list[TickerTax]) -> str:
    L = [f"🧾 {year}년 실현손익 — 봇 장부 기준 (선입선출)", ""]
    if not items:
        L.append("  이 해에 봇이 판 기록이 없습니다.")
        return "\n".join(L)
    total_usd = total_krw = 0.0
    krw_ok = True
    for t in items:
        L.append(f"💎 [{t.ticker}] 매도 {len(t.sales)}건 · {sum(s.qty for s in t.sales)}주")
        L.append(f"  매도금액 ${t.proceeds:,.2f} · 취득가 ${t.cost:,.2f} · 수수료 약 ${t.fee:,.2f}")
        L.append(f"  실현손익 ${t.gain:+,.2f}"
                 + (f" ≈ ₩{t.gain_krw:+,.0f}" if all(s.usd_krw for s in t.sales) else ""))
        if t.unmatched_qty:
            L.append(f"  ⚠️ 매수 기록 없이 판 {t.unmatched_qty}주는 계산에서 뺐습니다 (봇 시작 전 물량 등)")
        L.append("")
        total_usd += t.gain
        total_krw += t.gain_krw
        krw_ok = krw_ok and all(s.usd_krw for s in t.sales)

    L.append("━━ 합계 ━━")
    if krw_ok:
        taxable = max(0.0, total_krw - BASIC_DEDUCTION_KRW)
        L.append(f"  실현손익 ${total_usd:+,.2f} ≈ ₩{total_krw:+,.0f}")
        L.append(f"  기본공제 ₩{BASIC_DEDUCTION_KRW:,.0f} 뒤 과세표준 ≈ ₩{taxable:,.0f}")
        L.append(f"  예상세액(22%) ≈ ₩{taxable * TAX_RATE:,.0f}")
    else:
        L.append(f"  실현손익 ${total_usd:+,.2f} (환율 기록이 없어 원화 환산 생략)")
    L += ["",
          "참고용 추정입니다.",
          "  • 봇 장부만 계산 — 같은 계좌의 직접 매매, 다른 계좌, 배당은 빠짐",
          "  • 기본공제 250만원은 그 해 해외주식 전체에 한 번 적용",
          "  • 환율은 매도일 정산 때 기록한 기준환율 (실제 신고는 결제일 기준)",
          "  • 신고(이듬해 5월)는 증권사 양도소득 자료로 하세요",
          "",
          "건별 내역은 함께 보내는 CSV 파일에 있습니다."]
    return "\n".join(L)


def write_csv(items: list[TickerTax], path: Path) -> Path:
    path = Path(path)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:      # 엑셀에서 한글 깨짐 방지
        w = csv.writer(f)
        w.writerow(["매도일", "종목", "수량", "매도가(USD)", "매도금액(USD)", "취득가(USD)",
                    "수수료추정(USD)", "실현손익(USD)", "환율", "실현손익(KRW)"])
        for t in items:
            for s in t.sales:
                w.writerow([s.date, s.ticker, s.qty, f"{s.price:.4f}", f"{s.proceeds:.2f}",
                            f"{s.cost:.2f}", f"{s.fee:.2f}", f"{s.gain:.2f}",
                            f"{s.usd_krw:.2f}" if s.usd_krw else "",
                            f"{s.gain_krw:.0f}" if s.usd_krw else ""])
    return path
