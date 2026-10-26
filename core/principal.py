"""
================================================================
사이클 종료 후 새 원금 정하기

1회 매수금 = 잔금 ÷ (분할 − T) 라서, 예전에는 사이클이 끝나면 번 돈까지
잔금에 남아 다음 사이클이 자동으로 커졌다(자동 복리). 이제 사이클이 끝나면
새 원금을 고를 때까지 그 종목의 첫 매수를 **대기**한다.

  [💰 수익 포함 $21,234]  원금 = 지금 잔금 (복리)
  [🔒 그대로 $20,000]     원금·잔금 = 지난 원금 (남는 돈은 계좌에 그대로)
  [✏️ 직접 입력]          원하는 금액 → 확인 화면 → 적용

버튼을 못 쓰면  /principal SOXL keep | profit | 25000
================================================================
"""

from __future__ import annotations

import math

KEEP, PROFIT, INPUT, SET, CANCEL = "KEEP", "PROFIT", "INPUT", "SET", "CANCEL"


def _fmt(v: float) -> str:
    return f"${v:,.0f}" if abs(v - round(v)) < 0.005 else f"${v:,.2f}"


def prompt(ticker: str, state, available: float | None = None) -> tuple[str, list]:
    keep, profit = float(state.principal), round(float(state.cash), 2)
    diff = profit - keep
    L = [f"💰 [{ticker}] 새 사이클 원금을 정해 주세요",
         f"  지난 원금 {_fmt(keep)} · 지금 잔금 ${profit:,.2f} ({'수익' if diff >= 0 else '손실'} ${diff:+,.2f})"]
    if available is not None:
        L.append(f"  주문가능 ${available:,.2f} (원화 포함)")
    L += ["",
          f"  고를 때까지 {ticker} 새 사이클 첫 매수는 대기합니다.",
          f"  버튼이 안 보이면: /principal {ticker} keep · profit · 25000"]
    rows = [[(f"💰 수익 포함 {_fmt(profit)}", f"PRIN:{PROFIT}:{ticker}"),
             (f"🔒 그대로 {_fmt(keep)}", f"PRIN:{KEEP}:{ticker}")],
            [("✏️ 직접 입력", f"PRIN:{INPUT}:{ticker}")]]
    return "\n".join(L), rows


def parse_amount(text: str) -> float | None:
    t = (text or "").strip().replace(",", "").replace("$", "").replace("달러", "")
    try:
        v = float(t)
    except ValueError:
        return None
    return v if (math.isfinite(v) and v > 0) else None


def check_amount(state, amount: float) -> str:
    """문제가 있으면 사유, 없으면 빈 문자열"""
    if not math.isfinite(amount) or amount <= 0:
        return "0보다 큰 금액을 넣어 주세요."
    cap = max(float(state.principal), float(state.cash), 1.0) * 10
    if amount > cap:
        return (f"지난 원금의 10배(${cap:,.0f})를 넘습니다. 오타가 아닌지 확인하고, "
                f"정말이면 /config 로 바꿔 주세요.")
    unit = amount / max(int(state.division), 1)
    price = float(getattr(state, "last_close_price", 0.0) or 0.0)
    if price > 0 and unit < price:
        return (f"1회 매수금 ${unit:,.2f} 로는 1주(${price:,.2f})도 살 수 없습니다. "
                f"최소 ${price * state.division:,.0f} 이상으로 정해 주세요.")
    return ""


def confirm(ticker: str, state, amount: float, available: float | None = None) -> tuple[str, list]:
    unit = amount / max(int(state.division), 1)
    L = [f"📝 [{ticker}] 새 원금 {_fmt(amount)} · {state.division}분할 → 1회 매수금 ${unit:,.2f}"]
    if available is not None:
        first = unit                                 # 첫 매수는 1회 매수금 안에서 산다
        if available < first:
            L.append(f"  ⚠️ 주문가능 ${available:,.2f} — 첫 매수 전에 약 ${first - available:,.2f} 더 필요합니다")
        else:
            L.append(f"  주문가능 ${available:,.2f} (원화 포함) — 첫 매수 충분")
    rows = [[("✅ 적용", f"PRIN:{SET}:{ticker}:{amount:.2f}"),
             ("취소", f"PRIN:{CANCEL}:{ticker}")]]
    return "\n".join(L), rows


def amount_for(state, kind: str) -> float:
    if kind == PROFIT:
        return round(float(state.cash), 2)
    return float(state.principal)


def apply(state_mgr, ticker: str, amount: float) -> str:
    """새 원금 적용. 결과 문구를 돌려준다."""
    ticker = ticker.upper()
    with state_mgr.edit_state(ticker) as st:
        if st.holdings > 0:
            return (f"⚠️ [{ticker}] 진행 중인 사이클(보유 {st.holdings}주)이라 지금은 바꾸지 않습니다. "
                    f"사이클이 끝나면 다시 고를 수 있습니다.")
        if not getattr(st, "awaiting_principal", False):
            # 이미 골랐거나 대기 중이 아니다. 지난 버튼을 다시 눌러 첫 매수 주문이
            # 걸린 뒤 잔금이 바뀌는 일을 막는다.
            return (f"ℹ️ [{ticker}] 새 원금을 기다리는 중이 아닙니다 (이미 정했거나 사이클 진행 중). "
                    f"원금을 바꾸려면 /config 를 쓰세요.")
        why = check_amount(st, amount)
        if why:
            return f"⚠️ [{ticker}] {why}"
        before_cash = float(st.cash)
        st.principal = float(amount)
        st.cash = float(amount)
        st.T = 0.0
        st.awaiting_principal = False
        # 아침 요약·자금 점검이 쓰는 다음 매수 필요 금액을 다시 계산한다
        try:
            if st.last_close_price > 0:
                from core.funding import buy_need, core_buy_need
                from modes.base_mode import MarketSnapshot
                from modes.normal_mode import NormalMode
                px = st.last_close_price
                plan = NormalMode(st).plan(MarketSnapshot(prev_close=px, current_price=px))
                st.next_buy_need = buy_need(plan, st.fee_rate)
                st.next_core_need = core_buy_need(plan, st.fee_rate)
        except Exception:
            pass
        division = st.division

    cfg = state_mgr.get_config(ticker) or {}
    cfg["principal"] = float(amount)
    state_mgr.save_config(ticker, cfg)

    unit = amount / max(division, 1)
    L = [f"✅ [{ticker}] 새 사이클 원금 {_fmt(amount)} — 1회 매수금 ${unit:,.2f}",
         "  다음 주문 시간(17:02)부터 첫 매수가 나갑니다.",
         "  오늘 주문 시간이 이미 지났다면 /run loc 로 지금 넣을 수 있습니다 (장 마감 전까지)."]
    left = before_cash - amount
    if left > 0.005:
        L.append(f"  남는 ${left:,.2f} 는 계좌에 그대로 두고 봇은 쓰지 않습니다.")
    elif left < -0.005:
        L.append(f"  지난 잔금보다 ${-left:,.2f} 많습니다 — 계좌 주문가능금액을 확인하세요.")
    return "\n".join(L)


def awaiting_tickers(state_mgr) -> list[str]:
    out = []
    for t in state_mgr.list_tickers():
        st = state_mgr.get_state(t)
        if st is not None and getattr(st, "awaiting_principal", False):
            out.append(t)
    return out
