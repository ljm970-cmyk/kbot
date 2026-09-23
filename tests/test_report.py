"""
================================================================
일별 리포트 테스트

실제 운영 중인 봇의 SOXL 이력(24거래일)을 그대로 넣어,
원장에서 뽑은 집계가 손계산과 맞는지 고정한다.

실행:  python tests/test_report.py
================================================================
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.order_registry import OrderRegistry
from core.t_calculator import FillKind
from eod.report import daily_report, position_block, trade_history_block
from modes.base_mode import PlannedOrder, PositionState

#: 실제 화면에서 가져온 SOXL 24거래일 이력
TRADES = [
    ("20250807", "buy", 140.25, 6), ("20250810", "buy", 134.73, 7),
    ("20250811", "buy", 134.18, 6), ("20250812", "buy", 135.57, 4),
    ("20250813", "buy", 136.70, 3), ("20250814", "buy", 144.95, 3),
    ("20250817", "sell", 137.55, 8), ("20250818", "buy", 135.44, 7),
    ("20250819", "buy", 132.50, 7), ("20250820", "buy", 130.59, 8),
    ("20250821", "buy", 120.60, 8), ("20250824", "buy", 111.16, 8),
    ("20250826", "buy", 116.60, 8), ("20250827", "buy", 125.15, 8),
    ("20250828", "buy", 111.34, 8), ("20250831", "buy", 122.85, 8),
    ("20250901", "buy", 121.33, 9), ("20250903", "buy", 120.12, 9),
    ("20250904", "sell", 117.28, 28), ("20250908", "sell", 120.12, 21),
    ("20250909", "sell", 120.12, 15), ("20250910", "buy", 119.46, 8),
    ("20250911", "buy", 121.82, 4), ("20250914", "buy", 117.11, 9),
]


def _build():
    reg = OrderRegistry(Path(tempfile.mkdtemp()) / "orders.db")
    qty, avg = 0.0, 0.0
    for d, side, price, q in TRADES:
        tag = FillKind.STAR_BUY if side == "buy" else FillKind.QUARTER_SELL
        reg.record_submission(d, "SOXL", PlannedOrder(
            tag=tag, side=side, trade_type="30", qty=q, price=price))
        reg.record_fill(d, "SOXL", side, q, price, order_price=price)
        if side == "buy":
            avg = (avg * qty + price * q) / (qty + q)
            qty += q
        else:
            qty -= q
    state = PositionState(ticker="SOXL", division=20, principal=20000.0,
                          fee_rate=0.0007, T=7.7290, avg_price=avg,
                          holdings=int(qty), cash=3500.0)
    return reg, state


# ================================================================
# 집계
# ================================================================

def test_history_has_one_row_per_trading_day():
    reg, _ = _build()
    rows = reg.trade_history("SOXL")
    assert len(rows) == 24


def test_totals_match_hand_calculation():
    reg, _ = _build()
    t = reg.totals("SOXL")
    assert abs(t["buy_amount"] - 17228.82) < 0.01
    assert abs(t["sell_amount"] - 8708.56) < 0.01
    assert t["buy_qty"] == 138
    assert t["sell_qty"] == 72
    assert t["net_qty"] == 66


def test_weighted_average_not_last_fill_price():
    """평단은 가중평균이어야 한다.

    실제 운영 중인 다른 봇이 마지막 체결가($117.11)를 평단으로 표시하는
    것을 발견했다. 올바른 값은 $123.12 로 6달러 차이가 난다.
    SOXL 목표매도는 평단+20% 이므로, 평단이 낮으면 실제로는 +14% 에
    파는 셈이 된다.
    """
    _, state = _build()
    assert abs(state.avg_price - 123.1211) < 0.001
    last_fill_price = TRADES[-1][2]
    assert abs(state.avg_price - last_fill_price) > 5.0


def test_same_day_fills_merged_by_weighted_average():
    """하루에 별지점·평단·폭락대비가 함께 체결되면 한 줄로 합친다"""
    reg = OrderRegistry(Path(tempfile.mkdtemp()) / "o.db")
    for tag, price, qty in [(FillKind.STAR_BUY, 100.0, 3),
                            (FillKind.AVG_BUY, 90.0, 4),
                            (FillKind.CRASH_BUY, 80.0, 1)]:
        reg.record_submission("20260915", "TQQQ", PlannedOrder(
            tag=tag, side="buy", trade_type="30", qty=qty, price=price))
        reg.record_fill("20260915", "TQQQ", "buy", qty, price, order_price=price)

    rows = reg.trade_history("TQQQ")
    assert len(rows) == 1
    assert rows[0]["qty"] == 8
    assert abs(rows[0]["avg_price"] - (100 * 3 + 90 * 4 + 80 * 1) / 8) < 1e-9


def test_buy_and_sell_same_day_stay_separate():
    reg = OrderRegistry(Path(tempfile.mkdtemp()) / "o.db")
    for tag, side, price, qty in [(FillKind.TARGET_SELL, "sell", 120.0, 10),
                                  (FillKind.STAR_BUY, "buy", 90.0, 5)]:
        reg.record_submission("20260915", "TQQQ", PlannedOrder(
            tag=tag, side=side, trade_type="30", qty=qty, price=price))
        reg.record_fill("20260915", "TQQQ", side, qty, price, order_price=price)
    rows = reg.trade_history("TQQQ")
    assert len(rows) == 2
    assert {r["side"] for r in rows} == {"buy", "sell"}


# ================================================================
# 출력
# ================================================================

def test_history_block_format():
    reg, _ = _build()
    text = trade_history_block(reg, "SOXL")
    assert "08.07" in text and "09.14" in text
    assert "매수" in text and "매도" in text
    assert "$140.25" in text


def test_position_block_shows_key_numbers():
    reg, state = _build()
    text = position_block(state, reg)
    assert "7.7290" in text
    assert "66주" in text
    assert "$123.12" in text
    assert "$17,228.82" in text


def test_ledger_mismatch_is_flagged():
    """이력 합계와 장부 보유수량이 어긋나면 표시한다"""
    reg, state = _build()
    state.holdings = 60          # 원장 이력은 66주
    assert "이력 합계 66주" in position_block(state, reg)


def test_crosscheck_included():
    reg, state = _build()
    text = daily_report(state, reg)
    assert "역산 T" in text


def test_empty_history():
    reg = OrderRegistry(Path(tempfile.mkdtemp()) / "o.db")
    assert "이력 없음" in trade_history_block(reg, "TQQQ")


def test_halted_state_shown_in_report():
    from core.reconciler import CircuitBreaker
    reg, state = _build()
    CircuitBreaker.halt(state, "보유수량 불일치")
    assert "정지" in daily_report(state, reg)


def test_report_command_registered():
    src = (Path(__file__).resolve().parent.parent / "tg_bot" / "bot.py").read_text()
    assert 'CommandHandler("report"' in src


def test_scheduler_uses_daily_report():
    src = (Path(__file__).resolve().parent.parent / "scheduler" / "engine.py").read_text()
    assert "daily_report(state, self.registry, result, stats=stats)" in src


# ================================================================
# 별% 표시
#
# 별%는 T에 따라 매일 움직인다. 가격만 보면 지금 어느 국면인지
# 알 수 없어서 평단 대비 %와 산식을 함께 보여준다.
# ================================================================

def _plan_with_star(star=180.82):
    class P:
        star_point = star
    return P()


def test_star_line_shows_percent_and_formula():
    from eod.report import star_line
    st = PositionState(ticker="SOXL", division=40, principal=20000.0,
                       fee_rate=0.0007, T=1.0, avg_price=151.95)
    line = star_line(st, _plan_with_star())
    assert "180.82" in line
    assert "+19.00%" in line
    assert "20 - T" in line


def test_star_formula_per_combination():
    from eod.report import _star_formula
    def _st(tk, div):
        return PositionState(ticker=tk, division=div, principal=20000.0,
                             fee_rate=0.0007)
    assert _star_formula(_st("SOXL", 20)) == "20 - 2T"
    assert _star_formula(_st("SOXL", 40)) == "20 - T"
    assert _star_formula(_st("TQQQ", 20)) == "15 - 1.5T"
    assert _star_formula(_st("TQQQ", 40)) == "15 - 0.75T"


def test_star_percent_turns_negative_late_in_cycle():
    """T가 커지면 별지점이 평단 아래로 내려간다"""
    from eod.report import star_line
    st = PositionState(ticker="SOXL", division=40, principal=20000.0,
                       fee_rate=0.0007, T=30.0, avg_price=100.0)
    assert "-10.00%" in star_line(st, _plan_with_star(90.0))


def test_reverse_star_has_no_percent():
    """리버스 별지점은 직전 5거래일 종가 평균이라 평단과 무관하다"""
    from eod.report import star_line
    st = PositionState(ticker="SOXL", division=40, principal=20000.0,
                       fee_rate=0.0007, T=39.0, avg_price=100.0, mode="reverse")
    line = star_line(st, _plan_with_star(88.0))
    assert "5거래일" in line
    assert "%" not in line


def test_status_screen_shows_star_percent():
    root = Path(__file__).resolve().parent.parent
    src = (root / "tg_bot" / "commands_handler.py").read_text(encoding="utf-8")
    block = src[src.index("# 별지점"):]
    block = block[:block.index("다음주문")]
    assert "star_pct" in block
    assert "매도" in block          # 별지점 매수가·매도가 둘 다


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
