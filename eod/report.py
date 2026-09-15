"""
================================================================
텔레그램 리포트 포맷

장 마감 정산 후 관리자에게 보내는 요약. 사람이 휴대폰에서 읽는 것이
목적이므로 한 화면에 들어오게 만든다.

거래 이력은 주문 원장(core/order_registry)에서 뽑는다. 같은 날 여러 건이
체결돼도(별지점 + 평단 + 폭락대비 5단계) 하루 한 줄로 합쳐 보여준다.

여기서 계산하는 값은 모두 원장 기준이다. 증권사 잔고와의 대조는
core/reconciler 가 따로 한다.
================================================================
"""

from __future__ import annotations

from typing import Optional

from core.order_registry import OrderRegistry
from core.reconciler import CircuitBreaker, derive_t

#: 이력에 보여줄 최근 일수
HISTORY_DAYS = 30


def _fmt_money(v: float) -> str:
    return f"${v:,.2f}"


def trade_history_block(registry: OrderRegistry, ticker: str,
                        days: int = HISTORY_DAYS) -> str:
    """일자별 매매 이력 표"""
    rows = registry.trade_history(ticker, days)
    if not rows:
        return f"[{ticker}] 체결 이력 없음"

    lines = [f"[{ticker}] 일자별 매매 (최근 {len(rows)}일)", ""]
    lines.append("No. 일자     구분  평균단가   수량")
    lines.append("-" * 38)
    for i, t in enumerate(rows, start=1):
        d = t["date"]
        label = f"{d[4:6]}.{d[6:8]}" if len(d) == 8 else d
        side = "매수" if t["side"] == "buy" else "매도"
        lines.append(f"{i:<3} {label}  {side}  "
                     f"{_fmt_money(t['avg_price']):>9}  {t['qty']}주")
    lines.append("-" * 38)
    return "\n".join(lines)


def position_block(state, registry: OrderRegistry) -> str:
    """현재 진행 상황"""
    t = registry.totals(state.ticker)
    lines = [
        f"[{state.ticker}] 진행 상황",
        f"  T값     {state.T:.4f} ({state.division}분할 · {state.mode})",
        f"  보유     {state.holdings}주 (평단 {_fmt_money(state.avg_price)})",
        f"  보유원가 {_fmt_money(state.holdings * state.avg_price)}",
        f"  잔금     {_fmt_money(state.cash)}",
        f"  누적매수 {_fmt_money(t['buy_amount'])} ({t['buy_qty']}주)",
        f"  누적매도 {_fmt_money(t['sell_amount'])} ({t['sell_qty']}주)",
    ]

    realized = t["sell_amount"] - (t["buy_amount"] - state.holdings * state.avg_price)
    lines.append(f"  실현손익 {_fmt_money(realized)}  (이동평균 기준)")

    # 원장 이력과 장부가 어긋나면 체결 누락 신호다.
    #
    # 실제 운영 중인 다른 봇에서 이 검사가 없어 이력이 20주 이상 어긋난
    # 채로 돌아가는 것을 봤다. 잔고는 증권사 기준으로 교정되니 겉으로는
    # 정상처럼 보이지만, 이력에서 역산한 값이 전부 틀어진다.
    if t["net_qty"] != state.holdings:
        lines.append(f"  ⚠ 이력 합계 {t['net_qty']}주 ≠ 장부 {state.holdings}주 "
                     f"({t['net_qty'] - state.holdings:+d}주) — 체결 누락 의심")

    return "\n".join(lines)


def crosscheck_block(state) -> str:
    """잔고 역산 T 와 장부 T 비교"""
    if state.holdings <= 0 or state.avg_price <= 0:
        return ""
    d = derive_t(state.holdings, state.avg_price, state.principal, state.division)
    drift = abs(d - state.T)
    line = f"  역산 T   {d:.4f} (장부 {state.T:.4f}, 차이 {drift:.2f})"
    if drift >= 2.0:
        line += "  ← 확인 필요"
    return "교차검증\n" + line


def next_plan_block(plan) -> str:
    """다음 거래일 주문 계획"""
    if plan is None or not plan.orders:
        return "다음 거래일 주문 없음"
    lines = [f"다음 거래일 계획 (1회매수액 {_fmt_money(plan.unit_amount)})"]
    if plan.star_point is not None:
        lines.append(f"  별지점 {plan.star_point:.2f}")
    for o in plan.orders:
        price = f"{o.price:.2f}" if o.price is not None else "MKT"
        side = "매수" if o.side == "buy" else "매도"
        lines.append(f"  {side} {o.qty:>3}주 @{price:>8}  {o.tag}")
    for w in plan.warnings:
        lines.append(f"  ⚠ {w}")
    return "\n".join(lines)


def daily_report(
    state,
    registry: OrderRegistry,
    eod_result=None,
    include_history: bool = True,
    stats=None,
) -> str:
    """장 마감 정산 리포트 전문.

    순서: 이력 → 현황 → 교차검증 → 오늘 정산 → 다음 계획 → 경고
    """
    blocks: list[str] = []

    if include_history:
        blocks.append(trade_history_block(registry, state.ticker))

    blocks.append(position_block(state, registry))

    # 사이클 손익 병기.
    #
    # 이동평균 실현손익만 보면 착시가 생긴다. 151달러 물량이 있는 상태에서
    # 120달러에 추매하면 평단이 143.5달러가 되고, 130달러에 팔면 계좌에는
    # 실현손실로 찍히지만 그 물량 자체는 이익이다. 현금이 실제로 얼마나
    # 늘었는지를 함께 보여준다.
    if stats is not None:
        from eod.stats import cycle_pnl_line
        line = cycle_pnl_line(state, stats)
        if line:
            blocks.append("사이클\n" + line)

    cross = crosscheck_block(state)
    if cross:
        blocks.append(cross)

    if eod_result is not None:
        today = []
        if eod_result.t_result and eod_result.t_result.steps:
            today.append(f"오늘 정산 — 체결 {eod_result.fills_applied}건")
            today.append(f"  T {eod_result.t_result.t_before:.4f} "
                         f"→ {eod_result.t_result.t_after:.4f}")
            for s in eod_result.t_result.steps:
                today.append(f"    {s.detail}")
            if eod_result.realized_pl:
                today.append(f"  실현손익 {_fmt_money(eod_result.realized_pl)}")
        else:
            today.append("오늘 체결 없음")
        if eod_result.mode_transition:
            today.append(f"  모드 전환: {eod_result.mode_transition}")
        if eod_result.cycle_closed:
            today.append("  사이클 종료")
        blocks.append("\n".join(today))

        blocks.append(next_plan_block(eod_result.next_plan))

        alerts = list(eod_result.anomalies)
        alerts += [f"매칭 실패: {f.side} {f.qty}주 @{f.price:.2f}"
                   for f in eod_result.unmatched_fills]
        if alerts:
            blocks.append("확인 필요\n" + "\n".join(f"  ⚠ {a}" for a in alerts))

    halt = CircuitBreaker.status_line(state)
    if halt:
        blocks.append(halt)

    return "\n\n".join(b for b in blocks if b)
