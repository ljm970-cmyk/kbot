"""
================================================================
일반모드

매수 (매일 LOC 로 걸어둔다)
  처음매수  T=0, 보유 0
            전일종가 × 1.15 에 LOC 매수 + 폭락대비 5건

  전반전    0 < T < 분할수/2
            1회매수액의 절반을 별지점 LOC, 나머지 절반을 평단 LOC 로 분할.
            (1회매수액 ÷ 평단)의 정수부가 홀수면 평단 쪽 수량을 1개 더.
            + 폭락대비 5건

  후반전    분할수/2 ≤ T ≤ 분할수-1
            1회매수액 전체를 별지점 LOC 한 건으로.
            (후반전부터 별지점이 평단 아래로 내려간다)
            + 폭락대비 5건

  폭락대비  1회매수액 ÷ (첫주문수량+1), ÷(+2) ... 5건, 각 1주.
            체결돼도 T는 변하지 않는다.

소진 (라오어 2026-08-02 보강)
  1회 매수액으로 1주도 살 수 없는 시점이 소진이다. T > 분할수-1 도
  함께 본다. 소진되면 OrderPlan.exhausted 가 True 가 되고
  리버스모드로 전환한다.

매도 (전·후반 공통)
  쿼터매도  보유수량 ÷ 4 (반올림) 를 별지점에 LOC 매도
  목표매도  나머지 전량을 평단 +15%(TQQQ) / +20%(SOXL) 에 지정가매도.
            프리장 시작에 걸어 애프터까지 살려둔다.
================================================================
"""

from __future__ import annotations

from core.star_point import (
    crash_buy_prices,
    entry_price,
    guarded_buy_price,
    is_second_half,
    guarded_buy_price,
    needs_price_guard,
    normal_star_point,
    round_half_up,
    target_sell_price,
)
from core.t_calculator import FillKind
from kiwoom.constants import TradeType
from modes.base_mode import (
    BaseTradingMode,
    MarketSnapshot,
    OrderPlan,
    PlannedOrder,
    PositionState,
    SubmitWindow,
)

#: 폭락대비 LOC 매수 건수 (방법론: 최대 5개)
CRASH_ORDER_COUNT = 5


class NormalMode(BaseTradingMode):
    """일반모드 주문 생성기"""

    def plan(self, market: MarketSnapshot) -> OrderPlan:
        st = self.state
        unit = self.unit_amount()

        plan = OrderPlan(ticker=st.ticker, mode="normal", T=st.T, unit_amount=unit)

        # --- 별지점 (보유가 있을 때만 계산 가능) ---
        star = None
        if st.holdings > 0 and st.avg_price > 0:
            star = normal_star_point(st.ticker, st.division, st.avg_price, st.T)
            plan.star_point = star.star

        # --- 매수 ---
        if st.T > st.division - 1:
            plan.exhausted = True
            plan.warnings.append(
                f"T={st.T:.4f} 가 소진 기준({st.division - 1})을 넘었습니다. "
                f"리버스모드로 전환해야 합니다."
            )
        elif unit <= 0:
            plan.warnings.append("잔금이 없어 매수 주문을 생성하지 않습니다.")
        elif st.holdings == 0:
            self._plan_entry_buy(plan, market, unit)
        elif star is None:
            plan.warnings.append("평단가가 0이라 별지점을 계산할 수 없습니다.")
        elif is_second_half(st.division, st.T):
            self._plan_second_half_buy(plan, market, unit, star)
        else:
            self._plan_first_half_buy(plan, market, unit, star)

        # --- 매도 ---
        if st.holdings > 0 and star is not None:
            self._plan_sells(plan, star)

        # --- 단가 충돌 점검 ---
        # 예약주문은 ord_no 를 알 수 없어 단가로 태그를 복원한다.
        # 같은 매매구분·매매유형에서 단가가 겹치면 복원이 모호해진다.
        self._check_price_collisions(plan)

        # --- 잔금 점검 ---
        if plan.max_buy_amount > st.cash:
            plan.warnings.append(
                f"전량 체결 시 소요액 ${plan.max_buy_amount:,.2f} 가 "
                f"잔금 ${st.cash:,.2f} 를 초과합니다 (폭락대비 포함)."
            )

        return plan

    # ------------------------------------------------------------
    # 매수
    # ------------------------------------------------------------

    def _plan_entry_buy(self, plan: OrderPlan, market: MarketSnapshot, unit: float) -> None:
        """처음매수: 전일종가 +15% LOC"""
        if market.prev_close <= 0:
            plan.warnings.append("전일종가를 가져오지 못해 처음매수를 생성할 수 없습니다.")
            return

        price = entry_price(market.prev_close)
        qty = int(unit / price)
        if qty < 1:
            self._mark_exhausted(plan, unit, price, "처음매수")
            return

        plan.orders.append(PlannedOrder(
            tag=FillKind.ENTRY_BUY, side="buy", trade_type=TradeType.LOC,
            qty=qty, price=price,
            note=f"처음매수 전일종가 {market.prev_close:.2f}×1.15",
        ))
        self._append_crash_buys(plan, unit, qty)

    def _plan_first_half_buy(
        self, plan: OrderPlan, market: MarketSnapshot, unit: float, star
    ) -> None:
        """전반전: 별지점 LOC + 평단 LOC 2분할"""
        st = self.state
        total_qty = int(unit / st.avg_price)

        if total_qty < 1:
            self._mark_exhausted(plan, unit, st.avg_price, "전반전")
            return

        # 정수부가 홀수면 평단 쪽을 1개 더 준다
        if total_qty % 2 == 1:
            avg_qty = total_qty // 2 + 1
            star_qty = total_qty // 2
        else:
            avg_qty = star_qty = total_qty // 2

        avg_price = round_half_up(st.avg_price, 2)

        # 방법론 8: 별지점·평단 LOC 가 현재가에서 크게 벌어져 거부될 상황이면
        # 2분할하지 않고 현재가 +15% 에 단건으로 건다.
        if self._guard_needed(market, star.buy_price, avg_price):
            self._plan_guard_buy(plan, market, unit, "전반전")
            return

        # T 가 분할수의 절반에 가까워지면 별% 가 0 에 수렴한다.
        # 그러면 별지점이 평단에 붙어 별지점 매수가(별지점-0.01)와 평단
        # 매수가가 센트 단위에서 같아진다. 같은 가격에 주문 두 건을 내면
        # 둘 다 체결되는데 태그 복원이 모호해져서, 1회 매수(+1)를
        # 절반 매수(+0.5)로 잘못 계산할 수 있다. 한 건으로 합친다.
        if star.buy_price == avg_price:
            plan.orders.append(PlannedOrder(
                tag=FillKind.MERGED_BUY, side="buy", trade_type=TradeType.LOC,
                qty=total_qty, price=avg_price,
                note=f"별지점·평단 동일가 병합 ({total_qty}주)",
            ))
        else:
            if star_qty > 0:
                plan.orders.append(PlannedOrder(
                    tag=FillKind.STAR_BUY, side="buy", trade_type=TradeType.LOC,
                    qty=star_qty, price=star.buy_price, note="별지점",
                ))
            if avg_qty > 0:
                plan.orders.append(PlannedOrder(
                    tag=FillKind.AVG_BUY, side="buy", trade_type=TradeType.LOC,
                    qty=avg_qty, price=avg_price, note="평단",
                ))

        self._append_crash_buys(plan, unit, total_qty)

    def _plan_second_half_buy(
        self, plan: OrderPlan, market: MarketSnapshot, unit: float, star
    ) -> None:
        """후반전: 1회매수액 전체를 별지점 LOC 한 건으로"""
        qty = int(unit / star.buy_price) if star.buy_price > 0 else 0
        if qty < 1:
            self._mark_exhausted(plan, unit, star.buy_price, "후반전")
            return

        if self._guard_needed(market, star.buy_price):
            self._plan_guard_buy(plan, market, unit, "후반전")
            return

        plan.orders.append(PlannedOrder(
            tag=FillKind.HALF_BUY, side="buy", trade_type=TradeType.LOC,
            qty=qty, price=star.buy_price, note="별지점(후반전)",
        ))
        self._append_crash_buys(plan, unit, qty)

    def _append_crash_buys(self, plan: OrderPlan, unit: float, first_qty: int) -> None:
        """폭락대비 LOC 매수: 1회매수액 ÷ (first_qty+1) 부터 5건, 각 1주.

        이미 계획에 있는 단가와 겹치면 건너뛴다.

        주문수량이 크면 1회매수액을 (n), (n+1), (n+2) ... 로 나눈 값이
        센트 단위 반올림 후 같아진다. 예를 들어 주가가 낮아 1,000주를
        사는 상황이면 폭락대비 5건이 전부 같은 단가가 된다.
        같은 단가에 주문이 여럿이면 체결 태그를 복원할 수 없고,
        어차피 같은 가격이라 주문을 나눠 걸 이유도 없다.
        """
        used = {o.price for o in plan.orders if o.side == "buy" and o.price is not None}
        skipped = 0
        for i, price in enumerate(crash_buy_prices(unit, first_qty, CRASH_ORDER_COUNT), start=1):
            if price <= 0:
                continue
            if price in used:
                skipped += 1
                continue
            used.add(price)
            plan.orders.append(PlannedOrder(
                tag=FillKind.CRASH_BUY, side="buy", trade_type=TradeType.LOC,
                qty=1, price=price,
                note=f"폭락대비 {i} ({unit:.2f}/{first_qty + i}) · T불변",
            ))
        if skipped:
            plan.warnings.append(
                f"폭락대비 {skipped}건이 기존 주문과 단가가 같아 생략했습니다 "
                f"(주문수량이 커서 센트 단위에서 겹칩니다).")

    @staticmethod
    def _check_price_collisions(plan: OrderPlan) -> None:
        """같은 (매매구분, 매매유형) 안에서 단가가 겹치는지 확인.

        T=0 에서 TQQQ 별%가 15%, SOXL 이 20% 가 되어 쿼터매도(별지점 LOC)와
        목표매도(평단+15%/20% 지정가)의 단가가 같아진다. 매매유형이 달라
        실제 매칭은 가능하지만, 같은 유형끼리 겹치면 태그 복원이 무너진다.
        """
        seen: dict[tuple, str] = {}
        for o in plan.orders:
            key = (o.side, o.trade_type, o.price)
            if o.price is None:
                continue
            if key in seen:
                plan.warnings.append(
                    f"단가 충돌: {seen[key]} 와 {o.tag} 가 모두 "
                    f"{o.side} {o.trade_type} @{o.price:.2f} 입니다. "
                    f"체결 시 태그 복원이 모호해집니다."
                )
            seen[key] = o.tag

    def _mark_exhausted(self, plan: OrderPlan, unit: float, price: float, where: str) -> None:
        """소진 판정.

        라오어 2026-08-02 보강: 일반모드의 소진 기준은
        '1회 매수가 불가능해지는 시점'이다. 이 시점부터 리버스모드로 넘어간다.
        """
        plan.exhausted = True
        plan.warnings.append(
            f"[소진] 1회매수액 ${unit:,.2f} 로는 {price:.2f} 에서 1주도 살 수 없습니다 "
            f"({where}). 리버스모드로 전환해야 합니다."
        )

    @staticmethod
    def _guard_needed(market: MarketSnapshot, *prices: float) -> bool:
        """방법론 8번 발동 여부.

        증권사는 현재가에서 약 20% 이상 벌어진 주문을 거부한다.
        후반전에 평단이 현재가보다 한참 위일 때 반드시 걸린다.
        """
        if market.current_price <= 0:
            return False
        return any(needs_price_guard(p, market.current_price) for p in prices if p and p > 0)

    def _plan_guard_buy(
        self, plan: OrderPlan, market: MarketSnapshot, unit: float, where: str
    ) -> None:
        """가격제한폭 대체주문.

        무조건 매수를 의도하므로 현재가 +15% 에 LOC 를 **한 건만** 건다.
        별지점·평단으로 2분할하면 같은 가격의 주문이 두 건 나가면서
        1회매수액의 두 배가 체결되고, 태그 복원도 불가능해진다.
        체결 시 1회 매수로 보아 T +1.
        """
        price = guarded_buy_price(float("inf"), market.current_price)
        qty = int(unit / price) if price > 0 else 0
        if qty < 1:
            self._mark_exhausted(plan, unit, price, f"{where} 대체주문")
            return

        plan.orders.append(PlannedOrder(
            tag=FillKind.GUARD_BUY, side="buy", trade_type=TradeType.LOC,
            qty=qty, price=price,
            note=f"{where} 가격제한폭 회피 · 현재가 {market.current_price:.2f}×1.15 단건",
        ))
        plan.warnings.append(
            f"[가격제한폭] 별지점·평단 LOC 가 현재가 {market.current_price:.2f} 에서 "
            f"크게 벌어져 거부될 수 있습니다. 현재가×1.15 = {price:.2f} 에 단건으로 대체했습니다."
        )
        self._append_crash_buys(plan, unit, qty)

    # ------------------------------------------------------------
    # 매도
    # ------------------------------------------------------------

    def _plan_sells(self, plan: OrderPlan, star) -> None:
        st = self.state

        quarter_qty = int(round_half_up(st.holdings / 4, 0))
        quarter_qty = max(0, min(quarter_qty, st.holdings))

        if quarter_qty > 0:
            plan.orders.append(PlannedOrder(
                tag=FillKind.QUARTER_SELL, side="sell", trade_type=TradeType.LOC,
                qty=quarter_qty, price=star.sell_price,
                note=f"쿼터매도 보유 {st.holdings}÷4",
            ))

        remaining = st.holdings - quarter_qty
        if remaining > 0:
            price = target_sell_price(st.ticker, st.avg_price)
            plan.orders.append(PlannedOrder(
                tag=FillKind.TARGET_SELL, side="sell", trade_type=TradeType.LIMIT,
                qty=remaining, price=price,
                window=SubmitWindow.PRE_MARKET,
                note=f"목표매도 평단 {st.avg_price:.2f}×{1 + (0.15 if st.ticker.upper() == 'TQQQ' else 0.20):.2f}",
            ))


# ================================================================
# 편의 함수
# ================================================================

def plan_normal_day(state_dict: dict, market: MarketSnapshot) -> OrderPlan:
    """dict 기반 state 로 바로 계획 생성 (eod/scheduler 연동용)"""
    return NormalMode(PositionState.from_dict(state_dict)).plan(market)
