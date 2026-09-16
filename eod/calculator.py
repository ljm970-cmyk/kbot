"""
================================================================
End of Day 계산 엔진

장 마감 후 하루를 정산한다.

  1. 체결 수집 — WebSocket(F5) 수신분 + ust21510 조회분을 대조
  2. 원장 매칭 — 각 체결에 FillKind 태그를 복원 (core/order_registry)
  3. 순서 정렬 — 3/4 지정가매도 → 쿼터 LOC매도 → LOC매수 (방법론 9)
  4. 평단·보유·잔금 갱신 (수수료 차감)
  5. T값 갱신 — 하루치 체결 조합으로 한 번에 (core/t_calculator)
  6. 모드 전환 판정
  7. 다음 거래일 주문 계획 생성

이 모듈은 네트워크를 직접 건드리지 않는다. 체결 목록과 시세를 인자로
받으므로 방법론 예제로 그대로 테스트할 수 있다.
================================================================
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Optional, Sequence

from config.fees import FeePolicy, apply_buy, apply_sell, calculate_fee, policy_from_rate
from core.order_registry import OrderRegistry
from core.star_point import should_exit_reverse
from core.t_calculator import DayFills, FillKind, TCalculator, TResult
from modes.base_mode import MarketSnapshot, OrderPlan, PositionState
from modes.normal_mode import NormalMode
from modes.reverse_mode import ReverseMode

logger = logging.getLogger("kbot.eod")


# ================================================================
# 체결 순서 (방법론 9)
# ================================================================

#: 같은 거래일에 여러 건이 체결됐을 때 T·잔금 계산 순서.
#: 항상 매도를 먼저 계산하고 매수를 나중에 계산한다.
TAG_ORDER = {
    FillKind.TARGET_SELL: 0,        # 3/4 지정가매도
    FillKind.QUARTER_SELL: 1,       # 쿼터 LOC 매도
    FillKind.REVERSE_MOC_SELL: 1,
    FillKind.REVERSE_SELL: 1,
    FillKind.ENTRY_BUY: 2,          # 이하 매수
    FillKind.GUARD_BUY: 2,
    FillKind.MERGED_BUY: 2,
    FillKind.STAR_BUY: 2,
    FillKind.AVG_BUY: 2,
    FillKind.HALF_BUY: 2,
    FillKind.REVERSE_QUARTER_BUY: 2,
    FillKind.CRASH_BUY: 3,          # 폭락대비는 맨 뒤 (T 불변)
}


#: 체결가가 주문가에서 벗어날 수 있는 배수 범위.
#: LOC 는 종가에 체결되므로 주문가와 크게 다를 수 있다. 하루 -50% 폭락도
#: 0.5배이므로 아래 범위면 정상 거래를 막지 않는다. 이를 벗어나면
#: 파싱 오류(필드 어긋남, 소수점 위치)를 의심해야 한다.
FILL_PRICE_MIN_RATIO = 0.2
FILL_PRICE_MAX_RATIO = 5.0


@dataclass
class FillEvent:
    """체결 1건.

    order_price 는 매칭 키다. LOC 는 주문단가(예: 76.02)와
    체결단가(종가, 예: 68.10)가 다르므로 둘을 반드시 구분해야 한다.
    """
    ticker: str
    side: str                      # buy / sell
    qty: int
    price: float                   # 체결단가
    order_price: Optional[float] = None   # 주문단가 (없으면 매칭 정확도가 떨어진다)
    ord_no: str = ""
    fill_no: str = ""              # 체결번호 — 부분체결을 구분하는 유일 키
    trade_type: str = ""
    time: str = ""
    source: str = ""               # websocket / ust21510

    @property
    def amount(self) -> float:
        return self.qty * self.price

    def validate(self) -> Optional[str]:
        """명백히 잘못된 체결을 걸러낸다.

        원본 F5 파서가 필드명을 잘못 읽어 모든 체결을 0으로 만들었던 적이
        있다. 그런 파싱 오류는 예외를 내지 않고 조용히 장부를 망가뜨린다.
        음수 수량은 매수를 매도로 둔갑시키고, 비정상 가격은 평단과 잔금을
        복구 불가능하게 만든다.

        Returns:
            문제가 있으면 사유, 없으면 None
        """
        if self.side not in ("buy", "sell"):
            return f"매매구분이 잘못됨: {self.side!r}"
        if self.qty <= 0:
            return f"체결수량이 0 이하: {self.qty}"
        if self.price <= 0:
            return f"체결가가 0 이하: {self.price}"
        if self.order_price is not None and self.order_price > 0:
            ratio = self.price / self.order_price
            if not (FILL_PRICE_MIN_RATIO <= ratio <= FILL_PRICE_MAX_RATIO):
                return (f"체결가 {self.price:.2f} 가 주문가 {self.order_price:.2f} 의 "
                        f"{ratio:.2f}배 — 파싱 오류 의심")
        return None

    def dedupe_key(self) -> tuple:
        """중복 제거 키.

        같은 주문이 같은 수량·가격으로 두 번 부분체결될 수 있다.
        (예: 10주 주문이 5주씩 두 번) 주문번호·수량·가격만으로 키를
        만들면 두 건이 같은 것으로 보여 하나가 버려지고,
        보유수량이 그만큼 모자라게 기록된다.

        체결번호가 있으면 그것으로, 없으면 체결시각으로 구분한다.
        """
        marker = self.fill_no or self.time or ""
        # 주문단가를 반드시 포함한다.
        #
        # 폭락대비 매수 5건은 주문단가가 모두 다르지만(예: 62.67 / 55.70 /
        # 50.13 / 45.57 / 41.78) 종가가 크게 빠진 날에는 **전부 같은 종가에
        # 체결**된다. 체결가만 쓰면 주문번호가 비어 있을 때 5건의 키가
        # 완전히 같아져 4건이 중복으로 버려진다.
        # 게다가 중복 제거는 매칭 이전 단계라 미매칭 경고에도 잡히지 않는다.
        order_px = round(self.order_price, 4) if self.order_price is not None else None
        return (self.ord_no or "", marker, self.side, self.qty,
                round(self.price, 4), order_px)


@dataclass
class EodResult:
    """하루 정산 결과"""
    ticker: str
    trade_date: str
    mode_before: str
    mode_after: str
    t_result: Optional[TResult] = None
    fills_applied: int = 0
    deduped: int = 0                # 중복으로 제거된 체결 건수
    unmatched_fills: list[FillEvent] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    mode_transition: Optional[str] = None
    cycle_closed: bool = False
    next_plan: Optional[OrderPlan] = None
    realized_pl: float = 0.0

    def report(self, state: PositionState) -> str:
        lines = [
            f"[{self.ticker}] {self.trade_date} 정산",
            f"  체결 {self.fills_applied}건",
        ]
        if self.t_result:
            lines.append(f"  T {self.t_result.t_before:.4f} → {self.t_result.t_after:.4f}"
                         f"  ({self.t_result.detail})")
        lines.append(f"  보유 {state.holdings}주 · 평단 {state.avg_price:.4f}"
                     f" · 잔금 ${state.cash:,.2f}")
        if self.mode_transition:
            lines.append(f"  모드 전환: {self.mode_before} → {self.mode_after}")
        if self.cycle_closed:
            lines.append("  사이클 종료")
        for f in self.unmatched_fills:
            lines.append(f"  ⚠ 매칭 실패: {f.side} {f.qty}주 @{f.price:.2f}")
        for a in self.anomalies:
            lines.append(f"  ⚠ {a}")
        return "\n".join(lines)


# ================================================================
# 계산기
# ================================================================

class EndOfDayCalculator:
    """하루 정산기"""

    def __init__(self, registry: OrderRegistry):
        self.registry = registry

    # ------------------------------------------------------------
    # 진입점
    # ------------------------------------------------------------

    def run(
        self,
        state: PositionState,
        trade_date: str,
        fills: Sequence[FillEvent],
        close_price: float,
        next_market: MarketSnapshot,
        fee_policy: Optional[FeePolicy] = None,
        force: bool = False,
    ) -> EodResult:
        """하루를 정산하고 다음 거래일 계획까지 만든다.

        Args:
            state: 정산 전 상태. 이 객체를 직접 갱신한다.
            trade_date: YYYYMMDD
            fills: 그 날 체결 목록 (WebSocket + API 조회 합본)
            close_price: 그 날 종가. 리버스 종료 판정에 쓴다.
            next_market: 다음 거래일 주문 계획에 필요한 시세
            force: 이미 정산한 거래일을 다시 정산할지 (수동 복구용)
        """
        policy = fee_policy or policy_from_rate(state.fee_rate)
        result = EodResult(ticker=state.ticker, trade_date=trade_date,
                           mode_before=state.mode, mode_after=state.mode)

        # 같은 거래일을 두 번 정산하지 않는다.
        #
        # 스케줄 실행과 /run eod 수동 실행이 겹치면 같은 체결이 다시 들어와
        # 보유수량·잔금이 이중으로 반영된다. 원장 쪽에도 멱등 방어가 있지만,
        # 여기서 먼저 막는 편이 확실하다.
        if not force and getattr(state, "last_eod_date", "") == trade_date:
            result.anomalies.append(
                f"{trade_date} 는 이미 정산했습니다. 다시 정산하지 않았습니다.")
            result.next_plan = self._transition_and_plan(
                state, close_price, next_market, result)
            result.mode_after = state.mode
            return result

        # 1) 중복 제거 후 태그 복원
        tagged, unmatched = self._match_fills(trade_date, state.ticker, fills)
        result.unmatched_fills = unmatched
        result.fills_applied = len(tagged)
        result.deduped = len(getattr(self, "_last_dropped", []))
        for msg in getattr(self, "_last_invalid", []):
            result.anomalies.append(f"체결 데이터 이상: {msg}")
        if result.deduped:
            result.anomalies.append(
                f"중복으로 제거된 체결 {result.deduped}건. merge_fill_sources 가 "
                f"이미 경로별 중복을 처리하므로, 여기서 제거가 나오면 "
                f"주문번호·체결번호가 비어 있을 가능성이 있습니다.")

        # 2) 방법론 9 순서로 정렬
        tagged.sort(key=lambda p: (TAG_ORDER.get(p[0], 9), -(p[1].order_price or p[1].price)))

        # 3) 평단·보유·잔금 갱신
        result.realized_pl = self._apply_cash_and_position(state, tagged, policy)

        # 4) T값 — 하루치 조합으로 한 번에
        calc = TCalculator(state.division, state.T)
        day_fills = DayFills(kinds={tag for tag, _ in tagged},
                             holdings_after=state.holdings)

        if state.mode == "normal":
            t_result = calc.apply_normal_day(day_fills)
        else:
            t_result = calc.apply_reverse_day(day_fills)

        state.T = t_result.t_after
        state.last_close_price = close_price
        state.last_eod_date = trade_date
        result.t_result = t_result
        result.anomalies.extend(t_result.anomalies)
        result.cycle_closed = t_result.cycle_closed

        # 5) 사이클 종료 처리 — 잔금은 이월한다 (원금으로 되돌리지 않는다)
        # 보유 0 이어도 그날 체결이 없었으면 종료가 아니다.
        # 시작 전(아직 아무것도 안 산 상태)과 완주 후를 구분해야 한다.
        # 구분하지 않으면 매일 "사이클 종료" 가 뜬다.
        if state.holdings <= 0 and tagged:
            state.avg_price = 0.0
            state.T = 0.0
            state.mode = "normal"
            state.reverse_first_day = False
            result.cycle_closed = True
        elif state.holdings <= 0:
            result.cycle_closed = False

        # 6) 당일 미체결 만료
        self.registry.expire_open_orders(trade_date, state.ticker)

        # 7) 모드 전환 + 다음날 계획
        result.next_plan = self._transition_and_plan(state, close_price, next_market, result)
        # 별지점을 상태에 남긴다. 리버스 별지점은 직전 5거래일 종가 평균이라
        # 텔레그램에서 다시 계산하려면 시세를 또 조회해야 한다.
        if result.next_plan is not None and result.next_plan.star_point:
            state.last_star_point = result.next_plan.star_point
        result.mode_after = state.mode
        return result

    # ------------------------------------------------------------
    # 체결 매칭
    # ------------------------------------------------------------

    def _match_fills(
        self, trade_date: str, ticker: str, fills: Sequence[FillEvent]
    ) -> tuple[list[tuple[str, FillEvent]], list[FillEvent]]:
        """중복 제거 후 각 체결에 FillKind 를 붙인다."""
        seen: set[tuple] = set()
        tagged: list[tuple[str, FillEvent]] = []
        unmatched: list[FillEvent] = []
        dropped: list[FillEvent] = []
        invalid: list[str] = []

        for f in fills:
            if f.ticker.upper() != ticker.upper():
                continue

            reason = f.validate()
            if reason:
                invalid.append(f"{f.side} {f.qty}주 @{f.price} — {reason}")
                logger.error("잘못된 체결 데이터를 버립니다: %s", reason)
                continue

            key = f.dedupe_key()
            if key in seen:
                dropped.append(f)
                continue
            seen.add(key)

            rec = self.registry.record_fill(
                trade_date, ticker, f.side, f.qty, f.price,
                ord_no=f.ord_no, order_price=f.order_price, trade_type=f.trade_type,
            )
            if rec is None:
                unmatched.append(f)
                continue
            # 원장이 실제로 반영한 수량을 쓴다. 체결 이벤트의 수량을 그대로
            # 쓰면, 주문수량을 넘는 체결이 왔을 때 원장(상한 적용)과
            # 장부(무제한)가 갈라진다.
            applied = getattr(rec, "applied_qty", 0) or f.qty
            if applied != f.qty:
                invalid.append(
                    f"{f.side} {f.qty}주 요청 중 {applied}주만 반영 "
                    f"(주문수량 {rec.qty}주 초과)")
                f = replace(f, qty=applied)

            tagged.append((rec.tag, f))

        self._last_dropped = dropped
        self._last_invalid = invalid
        return tagged, unmatched

    # ------------------------------------------------------------
    # 평단 · 보유 · 잔금
    # ------------------------------------------------------------

    @staticmethod
    def _apply_cash_and_position(
        state: PositionState,
        tagged: list[tuple[str, FillEvent]],
        policy: FeePolicy,
    ) -> float:
        """정렬된 순서대로 평단·보유·잔금을 갱신하고 실현손익을 돌려준다.

        평단은 내부적으로 반올림하지 않는다. 매 체결마다 소수 둘째자리로
        끊으면 40회 누적 시 별지점이 몇 센트씩 밀린다.
        """
        realized = 0.0

        for tag, f in tagged:
            amount = f.amount
            fee = calculate_fee(amount, f.qty, is_sell=(f.side == "sell"), policy=policy)

            if f.side == "buy":
                prev_value = state.avg_price * state.holdings
                state.holdings += f.qty
                if state.holdings > 0:
                    state.avg_price = (prev_value + amount) / state.holdings
                state.cash = apply_buy(state.cash, amount, fee)
            else:
                # 보유수량을 넘는 매도는 있을 수 없다. 그대로 반영하면
                # 팔지도 않은 물량의 대금이 잔금에 더해진다.
                sell_qty = min(f.qty, state.holdings)
                if sell_qty <= 0:
                    continue
                amount = sell_qty * f.price
                fee = calculate_fee(amount, sell_qty, is_sell=True, policy=policy)
                realized += (f.price - state.avg_price) * sell_qty - fee
                state.holdings -= sell_qty
                state.cash = apply_sell(state.cash, amount, fee)

        return round(realized, 4)

    # ------------------------------------------------------------
    # 모드 전환 + 계획
    # ------------------------------------------------------------

    def _transition_and_plan(
        self,
        state: PositionState,
        close_price: float,
        market: MarketSnapshot,
        result: EodResult,
    ) -> Optional[OrderPlan]:
        """모드를 정한 뒤 다음 거래일 계획을 만든다."""

        if state.holdings <= 0:
            # 사이클 종료 — 다음날은 처음매수부터 다시 시작
            return NormalMode(state).plan(market)

        # 리버스 → 일반 복귀 판정 (T값은 그대로 이어 쓴다)
        if state.mode == "reverse":
            if should_exit_reverse(state.ticker, state.avg_price, close_price):
                state.mode = "normal"
                state.reverse_first_day = False
                result.mode_transition = "reverse→normal"
                logger.info("[%s] 종가 %.2f 회복 → 일반모드 복귀 (T=%.4f 유지)",
                            state.ticker, close_price, state.T)
            else:
                state.reverse_first_day = False
                return ReverseMode(state).plan(market)

        # 일반모드 계획을 만들어 보고, 소진이면 리버스로 넘긴다
        plan = NormalMode(state).plan(market)
        if plan.exhausted:
            state.mode = "reverse"
            state.reverse_first_day = True
            state.reverse_start_T = state.T
            result.mode_transition = (result.mode_transition or "") + "normal→reverse"
            logger.info("[%s] 소진 → 리버스모드 전환 (T=%.4f)", state.ticker, state.T)
            return ReverseMode(state).plan(market)

        return plan


# ================================================================
# 체결 수집 헬퍼
# ================================================================

def merge_fill_sources(
    websocket_fills: Sequence[FillEvent],
    api_fills: Sequence[FillEvent],
) -> tuple[list[FillEvent], list[FillEvent]]:
    """WebSocket 수신분과 ust21510 조회분을 합친다.

    두 경로는 같은 체결을 다른 형태로 준다. WebSocket 은 체결번호가 있고
    API 조회는 없을 수 있어서, 체결 단위로 키를 맞추면 같은 체결이 서로
    다른 것으로 보여 이중 계상된다.

    그래서 **주문번호 단위로 총 체결수량을 비교**한다.
      - API 총량 > WebSocket 총량  → WebSocket 이 놓친 것이므로 API 를 쓴다
      - 그 외                      → WebSocket 을 쓴다 (체결번호가 있어 정확)
    주문번호가 없는 체결은 그대로 통과시킨다 (원장이 단가로 매칭한다).

    Returns:
        (합본, WebSocket 이 놓친 체결)
    """
    def _by_ord(events):
        grouped: dict[str, list[FillEvent]] = {}
        for e in events:
            grouped.setdefault(e.ord_no or "", []).append(e)
        return grouped

    ws_grouped = _by_ord(websocket_fills)
    api_grouped = _by_ord(api_fills)

    merged: list[FillEvent] = []
    missed: list[FillEvent] = []

    for ord_no, api_events in api_grouped.items():
        ws_events = ws_grouped.get(ord_no, [])
        api_qty = sum(e.qty for e in api_events)
        ws_qty = sum(e.qty for e in ws_events)

        if not ord_no:
            # 주문번호가 없으면 대조할 수 없다. 단가 매칭에 맡긴다.
            merged.extend(api_events)
            continue

        if api_qty > ws_qty:
            merged.extend(api_events)
            missed.extend(api_events if not ws_events else api_events)
        else:
            merged.extend(ws_events)

    # API 에는 없고 WebSocket 에만 있는 주문 (조회 반영이 늦은 경우)
    for ord_no, ws_events in ws_grouped.items():
        if ord_no not in api_grouped:
            merged.extend(ws_events)

    if missed:
        logger.warning("WebSocket 이 놓친 체결 %d건을 API 조회로 보완합니다", len(missed))
    return merged, missed


def fills_from_api_rows(rows: Sequence[dict], registry: OrderRegistry = None) -> list[FillEvent]:
    """KiwoomAPIClient.get_today_orders() 결과 → FillEvent 목록.

    체결수량이 0인 행(미체결·취소)은 걸러낸다.
    """
    events = []
    for r in rows:
        cntr_qty = int(r.get("cntr_qty") or 0)
        if cntr_qty <= 0:
            continue
        events.append(FillEvent(
            ticker=r.get("stk_cd", ""),
            side=r.get("side", ""),
            qty=cntr_qty,
            price=float(r.get("cntr_uv") or 0),
            order_price=float(r.get("ord_uv") or 0) or None,
            ord_no=str(r.get("ord_no", "")),
            fill_no=str(r.get("fill_no", "") or ""),
            trade_type=r.get("trade_type", ""),
            time=r.get("cntr_time", ""),
            source="ust21510",
        ))
    return events
