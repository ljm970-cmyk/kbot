"""
================================================================
EOD 계산기 테스트

실행:  python tests/test_eod.py
================================================================
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.order_registry import OrderRegistry
from core.t_calculator import FillKind
from eod.calculator import (
    EndOfDayCalculator,
    FillEvent,
    fills_from_api_rows,
    merge_fill_sources,
)
from modes.base_mode import MarketSnapshot, PositionState
from modes.normal_mode import NormalMode

DAY = "20260915"
TICKER = "TQQQ"
NEXT_MARKET = MarketSnapshot(prev_close=68.10, current_price=68.10,
                             recent_closes=[68.1, 69.0, 67.5, 70.2, 68.2])


def _setup(prev_close=78.0, current_price=76.0, **kw):
    """전반전 상태 + 그 날 주문을 원장에 기록"""
    base = dict(ticker=TICKER, division=40, principal=20000, fee_rate=0.0007,
                T=8.0, avg_price=69.75, holdings=60, cash=539.23 * 32)
    base.update(kw)
    state = PositionState(**base)

    reg = OrderRegistry(Path(tempfile.mkdtemp()) / "orders.db")
    plan = NormalMode(state).plan(
        MarketSnapshot(prev_close=prev_close, current_price=current_price))
    reg.record_plan(DAY, TICKER, plan.orders)
    return state, reg, plan, EndOfDayCalculator(reg)


def _price_of(plan, tag):
    return next(o.price for o in plan.orders if o.tag == tag)


def _qty_of(plan, tag):
    return next(o.qty for o in plan.orders if o.tag == tag)


# ================================================================
# T값 반영
# ================================================================

def test_full_buy_day_raises_t_by_one():
    """별지점 + 평단 둘 다 체결 → T +1"""
    state, reg, plan, eod = _setup()
    close = 68.10
    fills = [
        FillEvent(TICKER, "buy", _qty_of(plan, FillKind.STAR_BUY), close,
                  order_price=_price_of(plan, FillKind.STAR_BUY)),
        FillEvent(TICKER, "buy", _qty_of(plan, FillKind.AVG_BUY), close,
                  order_price=_price_of(plan, FillKind.AVG_BUY)),
    ]
    r = eod.run(state, DAY, fills, close, NEXT_MARKET)
    assert r.t_result.t_after == 9.0
    assert r.fills_applied == 2
    assert state.holdings == 67


def test_half_buy_day():
    """별지점만 체결 → T +0.5"""
    state, reg, plan, eod = _setup()
    fills = [FillEvent(TICKER, "buy", 3, 75.0, order_price=_price_of(plan, FillKind.STAR_BUY))]
    r = eod.run(state, DAY, fills, 75.0, NEXT_MARKET)
    assert r.t_result.t_after == 8.5


def test_crash_buy_only_keeps_t():
    """폭락대비만 체결되면 T는 그대로, 보유·잔금만 변한다"""
    state, reg, plan, eod = _setup()
    crash = [o for o in plan.orders if o.tag == FillKind.CRASH_BUY][0]
    before_cash = state.cash
    r = eod.run(state, DAY, [FillEvent(TICKER, "buy", 1, 60.0, order_price=crash.price)],
                60.0, NEXT_MARKET)
    assert r.t_result.t_after == 8.0
    assert state.holdings == 61
    assert state.cash < before_cash


def test_deep_crash_fills_everything_but_t_rises_once():
    """종가 급락으로 본매수 2건 + 폭락대비 전부 체결 → T는 +1만"""
    state, reg, plan, eod = _setup()
    close = 44.00
    fills = [FillEvent(TICKER, "buy", o.qty, close, order_price=o.price)
             for o in plan.buys if o.price >= close]
    assert len(fills) >= 6
    r = eod.run(state, DAY, fills, close, NEXT_MARKET)
    assert r.t_result.t_after == 9.0


# ================================================================
# 체결 순서 (방법론 9)
# ================================================================

def test_target_sell_then_buy_ordering():
    """지정가매도 후 LOC 매수 → T × 0.25 + 1"""
    state, reg, plan, eod = _setup(T=20.0, holdings=60)
    # 후반전이므로 계획을 다시 만든다
    state2, reg2, plan2, eod2 = _setup(T=20.0)
    target = next(o for o in plan2.orders if o.tag == FillKind.TARGET_SELL)
    half = next(o for o in plan2.orders if o.tag == FillKind.HALF_BUY)

    fills = [
        FillEvent(TICKER, "buy", half.qty, 50.0, order_price=half.price),
        FillEvent(TICKER, "sell", target.qty, target.price, order_price=target.price),
    ]
    r = eod2.run(state2, DAY, fills, 50.0, NEXT_MARKET)
    assert r.t_result.t_after == 20.0 * 0.25 + 1


def test_sell_does_not_change_avg_price():
    """매도는 평단을 바꾸지 않는다"""
    state, reg, plan, eod = _setup()
    avg_before = state.avg_price
    quarter = next(o for o in plan.orders if o.tag == FillKind.QUARTER_SELL)
    eod.run(state, DAY, [FillEvent(TICKER, "sell", quarter.qty, quarter.price,
                                   order_price=quarter.price)],
            quarter.price, NEXT_MARKET)
    assert abs(state.avg_price - avg_before) < 1e-9


def test_avg_price_keeps_full_precision():
    """평단을 매 체결마다 반올림하지 않는다 (누적 오차 방지)"""
    state, reg, plan, eod = _setup(holdings=3, avg_price=10.0, cash=100000,
                                   prev_close=10.5, current_price=10.5)
    px = _price_of(plan, FillKind.AVG_BUY)
    qty = _qty_of(plan, FillKind.AVG_BUY)      # 주문수량만큼만 체결된다
    eod.run(state, DAY, [FillEvent(TICKER, "buy", qty, 20.0, order_price=px)],
            20.0, NEXT_MARKET)

    expected = (10.0 * 3 + 20.0 * qty) / (3 + qty)
    assert abs(state.avg_price - expected) < 1e-9
    assert state.avg_price != round(state.avg_price, 2)


# ================================================================
# 잔금 · 수수료
# ================================================================

def test_sell_proceeds_added_to_cash():
    """방법론 10: 매도 대금은 수수료 차감 후 즉시 잔금에 더해진다"""
    state, reg, plan, eod = _setup()
    cash_before = state.cash
    quarter = next(o for o in plan.orders if o.tag == FillKind.QUARTER_SELL)
    eod.run(state, DAY, [FillEvent(TICKER, "sell", quarter.qty, quarter.price,
                                   order_price=quarter.price)],
            quarter.price, NEXT_MARKET)
    gross = quarter.qty * quarter.price
    assert cash_before < state.cash < cash_before + gross    # 수수료만큼 덜 들어온다


def test_cycle_close_carries_cash_not_principal():
    """사이클 종료 시 잔금을 원금으로 되돌리면 손익이 사라진다"""
    state, reg, plan, eod = _setup(holdings=60, cash=1000.0, principal=20000.0)
    sells = [o for o in plan.orders if o.side == "sell"]
    fills = [FillEvent(TICKER, "sell", o.qty, o.price, order_price=o.price) for o in sells]
    r = eod.run(state, DAY, fills, sells[0].price, NEXT_MARKET)

    assert r.cycle_closed is True
    assert state.holdings == 0
    assert state.T == 0.0
    assert state.avg_price == 0.0
    assert state.cash > 1000.0            # 매도대금이 남아야 한다
    assert state.cash != state.principal  # 원금으로 리셋되면 안 된다


# ================================================================
# 모드 전환
# ================================================================

def test_exhaustion_switches_to_reverse():
    """1회 매수로 1주도 못 사면 소진 → 리버스 전환, 다음날 MOC 계획"""
    state, reg, plan, eod = _setup(T=30.0, cash=5.0, holdings=100, avg_price=69.75)
    r = eod.run(state, DAY, [], 60.0, NEXT_MARKET)

    assert state.mode == "reverse"
    assert state.reverse_first_day is True
    assert "normal→reverse" in (r.mode_transition or "")
    tags = [o.tag for o in r.next_plan.orders]
    assert FillKind.REVERSE_MOC_SELL in tags


def test_reverse_exit_carries_t():
    """리버스 종료 시 T값을 그대로 이어 쓴다"""
    state, reg, plan, eod = _setup(T=38.14375, holdings=100, avg_price=100.0,
                                   cash=5000.0)
    state.mode = "reverse"
    r = eod.run(state, DAY, [], close_price=86.0, next_market=NEXT_MARKET)

    assert state.mode == "normal"                 # 86 > 100×0.85 = 85
    assert abs(state.T - 38.14375) < 1e-9         # 리셋되지 않는다
    assert r.mode_transition == "reverse→normal"


def test_reverse_stays_below_threshold():
    state, reg, plan, eod = _setup(T=38.0, holdings=100, avg_price=100.0, cash=5000.0)
    state.mode = "reverse"
    eod.run(state, DAY, [], close_price=80.0, next_market=NEXT_MARKET)
    assert state.mode == "reverse"


# ================================================================
# 체결 수집 / 대조
# ================================================================

def _F(qty, ord_no, fill_no="", time="", side="buy"):
    return FillEvent(TICKER, side, qty, 68.1, order_price=76.02,
                     ord_no=ord_no, fill_no=fill_no, time=time)


def test_merge_detects_websocket_miss():
    """WebSocket 이 놓친 체결을 API 조회로 보완한다"""
    merged, missed = merge_fill_sources(
        [_F(3, "A1", fill_no="101")],
        [_F(3, "A1", time="153000"), _F(4, "A2", time="153001")])
    assert sum(e.qty for e in merged) == 7
    assert len(missed) == 1
    assert missed[0].ord_no == "A2"


def test_merge_does_not_double_count_same_fill():
    """WebSocket 은 체결번호가 있고 API 조회는 없을 수 있다.
    체결 단위로 키를 맞추면 같은 체결이 두 번 반영된다."""
    merged, missed = merge_fill_sources(
        [_F(5, "A1", fill_no="101")], [_F(5, "A1", time="153000")])
    assert sum(e.qty for e in merged) == 5
    assert missed == []


def test_merge_prefers_api_when_websocket_partial():
    """WebSocket 이 5주만 받고 API 가 10주면 API 가 맞다"""
    merged, _ = merge_fill_sources(
        [_F(5, "A3", fill_no="101")], [_F(10, "A3", time="153000")])
    assert sum(e.qty for e in merged) == 10


def test_merge_keeps_both_partial_fills():
    merged, _ = merge_fill_sources(
        [_F(5, "A4", fill_no="101"), _F(5, "A4", fill_no="102")],
        [_F(10, "A4", time="153000")])
    assert sum(e.qty for e in merged) == 10


def test_merge_keeps_websocket_only_fills():
    """API 조회 반영이 늦으면 WebSocket 쪽만 있을 수 있다"""
    merged, _ = merge_fill_sources([_F(3, "A5", fill_no="101")], [])
    assert sum(e.qty for e in merged) == 3


def test_crash_fills_at_same_close_are_not_deduped():
    """폭락대비 5건은 주문단가가 다르지만 큰 하락일엔 전부 같은 종가에
    체결된다. 체결가만으로 키를 만들면 4건이 조용히 버려진다.

    중복 제거는 매칭 이전 단계라 미매칭 경고에도 잡히지 않는다."""
    keys = {FillEvent(TICKER, "buy", 1, 35.0, order_price=p).dedupe_key()
            for p in [62.67, 55.70, 50.13, 45.57, 41.78]}
    assert len(keys) == 5


def test_true_duplicate_still_removed():
    a = FillEvent(TICKER, "buy", 1, 35.0, order_price=62.67, ord_no="X", fill_no="1")
    b = FillEvent(TICKER, "buy", 1, 35.0, order_price=62.67, ord_no="X", fill_no="1")
    assert a.dedupe_key() == b.dedupe_key()


def test_deduped_count_is_reported():
    """조용히 사라지지 않게 제거 건수를 남긴다"""
    state, reg, plan, eod = _setup()
    p = _price_of(plan, FillKind.STAR_BUY)
    q = _qty_of(plan, FillKind.STAR_BUY)
    dup = FillEvent(TICKER, "buy", q, 68.1, order_price=p, ord_no="X1", fill_no="1")
    r = eod.run(state, DAY, [dup, dup], 68.1, NEXT_MARKET)
    assert r.deduped == 1
    assert any("중복으로 제거" in a for a in r.anomalies)


def test_partial_fills_not_deduped():
    """같은 주문이 같은 수량·가격으로 두 번 부분체결될 수 있다.
    주문번호·수량·가격만으로 키를 만들면 한 건이 버려진다."""
    a = _F(5, "A1", fill_no="101")
    b = _F(5, "A1", fill_no="102")
    assert a.dedupe_key() != b.dedupe_key()

    c = _F(5, "A1", time="093015")
    d = _F(5, "A1", time="160000")
    assert c.dedupe_key() != d.dedupe_key()


def test_partial_fill_weighted_average_price():
    """부분체결이 다른 가격에 나면 체결단가는 가중평균이어야 한다"""
    state, reg, plan, eod = _setup()
    p = _price_of(plan, FillKind.STAR_BUY)
    reg.record_fill(DAY, TICKER, "buy", 1, 80.0, order_price=p)
    rec = reg.record_fill(DAY, TICKER, "buy", 2, 50.0, order_price=p)
    assert abs(rec.filled_price - (80.0 * 1 + 50.0 * 2) / 3) < 1e-9


def test_duplicate_fills_counted_once():
    """같은 체결이 두 경로로 들어와도 한 번만 반영한다"""
    state, reg, plan, eod = _setup()
    p = _price_of(plan, FillKind.STAR_BUY)
    q = _qty_of(plan, FillKind.STAR_BUY)
    dup = FillEvent(TICKER, "buy", q, 68.1, order_price=p, ord_no="X1")
    r = eod.run(state, DAY, [dup, dup], 68.1, NEXT_MARKET)
    assert r.fills_applied == 1
    assert state.holdings == 60 + q


def test_fills_from_api_rows_skips_unfilled():
    rows = [
        {"stk_cd": "TQQQ", "side": "buy", "cntr_qty": 0, "cntr_uv": 0,
         "ord_uv": 76.02, "ord_no": "1"},
        {"stk_cd": "TQQQ", "side": "buy", "cntr_qty": 3, "cntr_uv": 68.1,
         "ord_uv": 76.02, "ord_no": "2"},
    ]
    events = fills_from_api_rows(rows)
    assert len(events) == 1
    assert events[0].qty == 3
    assert events[0].order_price == 76.02


def test_unmatched_fill_is_reported():
    """원장에 없는 체결(수동 주문 등)은 경고로 남는다"""
    state, reg, plan, eod = _setup()
    r = eod.run(state, DAY, [FillEvent(TICKER, "buy", 5, 10.0, order_price=10.0)],
                10.0, NEXT_MARKET)
    assert len(r.unmatched_fills) == 1
    assert r.fills_applied == 0


# ================================================================
# 만료
# ================================================================

def test_all_open_orders_expire_at_eod():
    state, reg, plan, eod = _setup()
    eod.run(state, DAY, [], 68.1, NEXT_MARKET)
    rows = {r.tag: r.status for r in reg.day_orders(DAY, TICKER)}
    assert rows[FillKind.STAR_BUY] == "expired"
    assert rows[FillKind.TARGET_SELL] == "expired"


# ================================================================
# 중복 정산 방지
#
# 스케줄 실행과 /run eod 수동 실행이 겹치면 같은 체결이 다시 들어온다.
# 막지 않으면 보유수량·잔금이 이중으로 반영된다.
# ================================================================

def test_second_eod_same_day_is_ignored():
    state, reg, plan, eod = _setup()
    fills = [FillEvent(TICKER, "buy", _qty_of(plan, FillKind.STAR_BUY), 68.10,
                       order_price=_price_of(plan, FillKind.STAR_BUY), ord_no="A1")]

    r1 = eod.run(state, DAY, fills, 68.10, NEXT_MARKET)
    h1, c1, t1 = state.holdings, state.cash, state.T

    r2 = eod.run(state, DAY, fills, 68.10, NEXT_MARKET)
    assert state.holdings == h1, "보유수량이 이중 반영됐다"
    assert state.cash == c1
    assert state.T == t1
    assert any("이미 정산" in a for a in r2.anomalies)


def test_registry_ignores_duplicate_fill():
    """원장 차원의 2차 방어 — 이미 체결 처리된 주문은 다시 더하지 않는다"""
    state, reg, plan, eod = _setup()
    p = _price_of(plan, FillKind.STAR_BUY)
    q = _qty_of(plan, FillKind.STAR_BUY)

    r1 = reg.record_fill(DAY, TICKER, "buy", q, 68.1, ord_no="A1", order_price=p)
    assert r1.filled_qty == q
    r2 = reg.record_fill(DAY, TICKER, "buy", q, 68.1, ord_no="A1", order_price=p)
    assert r2 is None


def test_fill_never_exceeds_order_qty():
    """주문수량보다 많은 체결은 있을 수 없다"""
    state, reg, plan, eod = _setup()
    p = _price_of(plan, FillKind.STAR_BUY)
    q = _qty_of(plan, FillKind.STAR_BUY)
    reg.record_fill(DAY, TICKER, "buy", 1, 68.1, order_price=p)
    rec = reg.record_fill(DAY, TICKER, "buy", q + 10, 68.1, order_price=p)
    assert rec.filled_qty == q


def test_force_allows_recalculation():
    """수동 복구용 — force 를 주면 다시 정산한다"""
    state, reg, plan, eod = _setup()
    eod.run(state, DAY, [], 68.10, NEXT_MARKET)
    r = eod.run(state, DAY, [], 68.10, NEXT_MARKET, force=True)
    assert not any("이미 정산" in a for a in r.anomalies)


def test_next_day_eod_runs_normally():
    state, reg, plan, eod = _setup()
    eod.run(state, DAY, [], 68.10, NEXT_MARKET)
    r = eod.run(state, "20260916", [], 68.10, NEXT_MARKET)
    assert not any("이미 정산" in a for a in r.anomalies)
    assert state.last_eod_date == "20260916"


# ================================================================
# 체결 데이터 검증
#
# 원본 F5 파서가 필드명을 잘못 읽어 모든 체결을 0으로 만들었던 적이 있다.
# 그런 파싱 오류는 예외를 내지 않고 조용히 장부를 망가뜨린다.
# ================================================================

def _valid_plan():
    state, reg, plan, eod = _setup()
    sp = _price_of(plan, FillKind.STAR_BUY)
    sq = _qty_of(plan, FillKind.STAR_BUY)
    return state, reg, eod, plan, sp, sq


def test_zero_and_negative_qty_rejected():
    for qty in (0, -5):
        state, reg, eod, plan, sp, sq = _valid_plan()
        before = (state.holdings, state.cash)
        r = eod.run(state, DAY, [FillEvent(TICKER, "buy", qty, 68.0, order_price=sp)],
                    68.0, NEXT_MARKET)
        assert (state.holdings, state.cash) == before, f"수량 {qty} 가 반영됐다"
        assert any("체결수량" in a for a in r.anomalies)


def test_zero_and_negative_price_rejected():
    for px in (0.0, -68.0):
        state, reg, eod, plan, sp, sq = _valid_plan()
        before = (state.holdings, state.avg_price, state.cash)
        r = eod.run(state, DAY, [FillEvent(TICKER, "buy", sq, px, order_price=sp)],
                    68.0, NEXT_MARKET)
        assert (state.holdings, state.avg_price, state.cash) == before
        assert any("체결가" in a for a in r.anomalies)


def test_absurd_price_rejected():
    """파싱 오류로 소수점이 밀리면 평단과 잔금이 복구 불가능해진다"""
    state, reg, eod, plan, sp, sq = _valid_plan()
    before = state.cash
    r = eod.run(state, DAY, [FillEvent(TICKER, "buy", sq, 1_000_000.0, order_price=sp)],
                68.0, NEXT_MARKET)
    assert state.cash == before
    assert state.avg_price < 1000
    assert any("파싱 오류" in a for a in r.anomalies)


def test_crash_day_fill_still_accepted():
    """LOC 은 종가에 체결된다. 하루 -50% 폭락도 정상 거래다."""
    state, reg, eod, plan, sp, sq = _valid_plan()
    r = eod.run(state, DAY, [FillEvent(TICKER, "buy", sq, sp * 0.5, order_price=sp)],
                sp * 0.5, NEXT_MARKET)
    assert r.fills_applied == 1
    assert not any("파싱 오류" in a for a in r.anomalies)


def test_fill_exceeding_order_qty_is_capped():
    """원장은 주문수량으로 막는데 장부가 그대로 받으면 둘이 갈라진다"""
    state, reg, eod, plan, sp, sq = _valid_plan()
    before = state.holdings
    r = eod.run(state, DAY, [FillEvent(TICKER, "buy", sq + 50, 68.0, order_price=sp)],
                68.0, NEXT_MARKET)
    assert state.holdings == before + sq
    assert any("초과" in a for a in r.anomalies)


def test_oversell_capped_at_holdings():
    """보유를 넘는 매도를 반영하면 팔지도 않은 물량의 대금이 잔금에 더해진다"""
    state, reg, plan, eod = _setup()
    qs = next(o for o in plan.orders if o.tag == FillKind.QUARTER_SELL)
    before_cash = state.cash
    r = eod.run(state, DAY,
                [FillEvent(TICKER, "sell", 1000, qs.price, order_price=qs.price)],
                qs.price, NEXT_MARKET)
    assert state.holdings >= 0
    assert state.cash < before_cash + 1000 * qs.price
    assert any("초과" in a for a in r.anomalies)


def test_bad_side_rejected():
    state, reg, eod, plan, sp, sq = _valid_plan()
    before = state.holdings
    r = eod.run(state, DAY, [FillEvent(TICKER, "BUY", sq, 68.0, order_price=sp)],
                68.0, NEXT_MARKET)
    assert state.holdings == before


def test_validate_returns_none_for_good_fill():
    assert FillEvent(TICKER, "buy", 3, 68.0, order_price=76.02).validate() is None


def test_duplicate_api_rows_for_same_order():
    """실측 2026-09-23: ust21510 이 같은 체결(3주)을 두 행으로 돌려줬다.
    체결번호·시각이 달라 중복 제거에 걸리지 않고, 원장이 두 번째를 막아
    "매칭 실패" 로 신고됐다. 한 주문의 체결 합계는 주문수량을 넘을 수 없다.
    """
    row = {"stk_cd": TICKER, "side": "buy", "ord_no": "A1", "ord_qty": 3,
           "cntr_qty": 3, "cntr_uv": 151.95, "ord_uv": 163.11,
           "fill_no": "1", "cntr_time": "160000", "trade_type": "30"}
    dup = {**row, "fill_no": "2", "cntr_time": "160001"}
    events = fills_from_api_rows([row, dup])
    assert len(events) == 1
    assert events[0].qty == 3


def test_partial_fills_of_same_order_kept():
    """부분체결은 합계가 주문수량 이내라 모두 살려야 한다"""
    base = {"stk_cd": TICKER, "side": "buy", "ord_no": "A1", "ord_qty": 5,
            "cntr_uv": 151.95, "ord_uv": 163.11, "trade_type": "30"}
    events = fills_from_api_rows([
        {**base, "cntr_qty": 2, "fill_no": "1"},
        {**base, "cntr_qty": 3, "fill_no": "2"},
    ])
    assert [e.qty for e in events] == [2, 3]


def test_overfill_row_is_capped():
    base = {"stk_cd": TICKER, "side": "buy", "ord_no": "A1", "ord_qty": 3,
            "cntr_uv": 151.95, "ord_uv": 163.11, "trade_type": "30"}
    events = fills_from_api_rows([
        {**base, "cntr_qty": 2, "fill_no": "1"},
        {**base, "cntr_qty": 5, "fill_no": "2"},
    ])
    assert [e.qty for e in events] == [2, 1]


def test_same_fill_with_different_ord_no_padding():
    """실측 2026-09-24: 같은 체결이 WebSocket 과 API 에서 자리수가 다른
    주문번호로 와, 둘 다 반영되려다 "매칭 실패" 로 신고됐다."""
    ws = [FillEvent(TICKER, "buy", 1, 146.25, order_price=180.81,
                    ord_no="0000010928", fill_no="1")]
    api = [FillEvent(TICKER, "buy", 1, 146.25, order_price=180.81,
                     ord_no="000010928", cntr_time="160000")
           if False else
           FillEvent(TICKER, "buy", 1, 146.25, order_price=180.81,
                     ord_no="000010928", time="160000")]
    merged, missed = merge_fill_sources(ws, api)
    assert sum(e.qty for e in merged) == 1
    assert missed == []


def test_ws_only_fill_without_ord_no_is_not_duplicated():
    """WebSocket 이 주문번호 없이 준 체결이 API 것과 같은 모양이면
    한 번만 센다."""
    ws = [FillEvent(TICKER, "buy", 2, 146.25, order_price=151.95, ord_no="")]
    api = [FillEvent(TICKER, "buy", 2, 146.25, order_price=151.95,
                     ord_no="000010929", time="160000")]
    merged, _ = merge_fill_sources(ws, api)
    assert sum(e.qty for e in merged) == 2


def test_genuinely_different_fill_still_added():
    """모양이 다르면 별개 체결이므로 살려야 한다"""
    ws = [FillEvent(TICKER, "buy", 1, 140.00, order_price=151.95, ord_no="")]
    api = [FillEvent(TICKER, "buy", 2, 146.25, order_price=151.95,
                     ord_no="000010929", time="160000")]
    merged, _ = merge_fill_sources(ws, api)
    assert sum(e.qty for e in merged) == 3


def test_registry_matches_ord_no_regardless_of_padding():
    from core.order_registry import normalize_ord_no
    state, reg, plan, eod = _setup()
    rid = reg.record_submission(DAY, TICKER, plan.orders[0])
    reg.attach_ord_no(rid, "000010928")

    assert normalize_ord_no("0000010928") == normalize_ord_no("000010928")
    assert reg.by_ord_no("0000010928") is not None
    assert reg.by_ord_no("10928") is not None
    assert reg.by_ord_no("999") is None


# ================================================================

if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:
                failed += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print("-" * 60)
    print("전부 통과" if failed == 0 else f"{failed}건 실패")
    sys.exit(1 if failed else 0)
