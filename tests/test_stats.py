"""
================================================================
누적 통계 · 버튼식 보정 테스트

실행:  python tests/test_stats.py
================================================================
"""

import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _Btn:
    def __init__(self, text, callback_data=None):
        self.text = text
        self.callback_data = callback_data


class _Markup:
    def __init__(self, rows):
        self.inline_keyboard = rows
        self.rows = rows            # 스텁 호환


def _rows(markup):
    """실제 InlineKeyboardMarkup 은 inline_keyboard 를 쓴다.

    스텁만 보고 테스트하면 라이브러리가 실제로 설치된 환경에서 깨진다.
    """
    return getattr(markup, "inline_keyboard", None) or markup.rows


try:
    import telegram  # noqa: F401
except ImportError:
    _m = types.ModuleType("telegram")
    _m.InlineKeyboardButton = _Btn
    _m.InlineKeyboardMarkup = _Markup
    sys.modules["telegram"] = _m

from core.state_manager import StateManager  # noqa: E402
from eod.stats import Cycle, collect, cycle_pnl_line, format_stats  # noqa: E402
from modes.base_mode import PositionState  # noqa: E402
from tg_bot.fix_ui import (  # noqa: E402
    FixDraft,
    FixSession,
    cb,
    gap_hint,
    keyboard_side,
    keyboard_t,
    parse_cb,
    preview_text,
    suggest_quantities,
    t_preview_text,
)

CFG = {"division": 40, "principal": 20000.0, "fee_rate": 0.0007, "is_active": True}


def _mgr_with_archive(rows) -> StateManager:
    m = StateManager(Path(tempfile.mkdtemp()) / "data")
    m.save_config("TQQQ", CFG)
    for r in rows:
        m.archive_eod("TQQQ", r["trade_date"], r)
    return m


def _row(date, cash, holdings=0, avg=0.0, T=0.0, closed=False, mode="normal", pl=0.0):
    return {"trade_date": date, "cash": cash, "holdings": holdings, "avg_price": avg,
            "T": T, "mode": mode, "cycle_closed": closed, "realized_pl": pl}


# ================================================================
# 사이클 집계
# ================================================================

def test_collect_splits_cycles():
    m = _mgr_with_archive([
        _row("20260601", 19000, 10, 70.0, 1.0),
        _row("20260602", 18500, 17, 69.0, 2.0),
        _row("20260603", 20500, 0, 0.0, 0.0, closed=True),
        _row("20260604", 19500, 8, 71.0, 1.0),
        _row("20260605", 21000, 0, 0.0, 0.0, closed=True),
    ])
    s = collect(m, "TQQQ", ["202606"])
    assert s.closed == 2
    assert s.total_days == 5


def test_cycle_pnl_is_cash_delta():
    """사이클 손익 = 종료 시 잔금 - 시작 잔금 (이동평균 실현손익과 다르다)"""
    m = _mgr_with_archive([
        _row("20260601", 19300, 10, 70.0, 1.0),      # 시작잔금 = 19300 + 700 = 20000
        _row("20260602", 20500, 0, 0.0, 0.0, closed=True),
    ])
    s = collect(m, "TQQQ", ["202606"])
    c = s.cycles[0]
    assert abs(c.start_cash - 20000.0) < 1e-6
    assert abs(c.pnl - 500.0) < 1e-6
    assert c.is_win is True


def test_win_rate_and_totals():
    m = _mgr_with_archive([
        _row("20260601", 20000, 0, 0.0), _row("20260602", 20500, 0, 0.0, closed=True),
        _row("20260603", 20500, 0, 0.0), _row("20260604", 20200, 0, 0.0, closed=True),
        _row("20260605", 20200, 0, 0.0), _row("20260606", 21000, 0, 0.0, closed=True),
    ])
    s = collect(m, "TQQQ", ["202606"])
    assert s.closed == 3
    assert s.wins == 2
    assert abs(s.win_rate - 66.67) < 0.1


def test_open_cycle_tracked():
    m = _mgr_with_archive([
        _row("20260601", 19000, 10, 70.0, 1.0),
        _row("20260602", 18000, 20, 69.0, 2.5),
    ])
    s = collect(m, "TQQQ", ["202606"])
    assert s.closed == 0
    assert s.open_cycle is not None
    assert s.open_cycle.days == 2
    assert s.open_cycle.max_T == 2.5
    assert s.open_cycle.max_holdings == 20


def test_reverse_days_counted():
    m = _mgr_with_archive([
        _row("20260601", 100, 50, 70.0, 39.5, mode="reverse"),
        _row("20260602", 500, 45, 70.0, 37.5, mode="reverse"),
        _row("20260603", 900, 40, 70.0, 20.0),
    ])
    s = collect(m, "TQQQ", ["202606"])
    assert s.reverse_days == 2


def test_empty_archive():
    m = StateManager(Path(tempfile.mkdtemp()) / "data")
    m.save_config("TQQQ", CFG)
    s = collect(m, "TQQQ", ["202606"])
    assert s.closed == 0
    assert "집계할 이력이 없습니다" in format_stats(s)


def test_format_contains_key_numbers():
    m = _mgr_with_archive([
        _row("20260601", 19300, 10, 70.0, 1.0),
        _row("20260602", 20500, 0, 0.0, 0.0, closed=True),
    ])
    out = format_stats(collect(m, "TQQQ", ["202606"]))
    assert "완료 사이클" in out
    assert "승률" in out
    assert "$500.00" in out or "500.00" in out


def test_cycle_pnl_line_for_report():
    m = _mgr_with_archive([_row("20260601", 19000, 10, 70.0, 1.0)])
    s = collect(m, "TQQQ", ["202606"])
    st = PositionState(ticker="TQQQ", division=40, principal=20000.0,
                       fee_rate=0.0007, holdings=10, avg_price=70.0)
    line = cycle_pnl_line(st, s)
    assert "사이클 #1" in line
    assert "투입" in line


def test_report_marks_moving_average():
    """이동평균 실현손익은 착시가 있으므로 기준을 명시한다"""
    src = (ROOT / "eod" / "report.py").read_text(encoding="utf-8")
    assert "이동평균 기준" in src


# ================================================================
# 버튼식 보정
# ================================================================

def test_suggest_quantities_puts_gap_first():
    """정지 사유가 '장부 57주 / 증권사 60주' 면 3주를 먼저 제시한다"""
    st = PositionState(ticker="TQQQ", division=40, principal=20000.0,
                       fee_rate=0.0007, holdings=57, avg_price=70.0)
    opts = suggest_quantities(st, {"poss_qty": 60, "avg_price": 69.9})
    assert opts[0][0] == 3
    assert "부족" in opts[0][1]


def test_suggest_quantities_marks_excess():
    st = PositionState(ticker="TQQQ", division=40, principal=20000.0,
                       fee_rate=0.0007, holdings=60, avg_price=70.0)
    opts = suggest_quantities(st, {"poss_qty": 57, "avg_price": 69.9})
    assert opts[0][0] == 3
    assert "초과" in opts[0][1]


def test_suggest_quantities_without_broker_data():
    st = PositionState(ticker="TQQQ", division=40, principal=20000.0,
                       fee_rate=0.0007, holdings=57, avg_price=70.0)
    opts = suggest_quantities(st, None)
    assert [q for q, _ in opts][:3] == [1, 2, 3]


def test_preview_matches_actual_apply():
    """미리보기 숫자와 실제 적용 결과가 같아야 한다"""
    m = StateManager(Path(tempfile.mkdtemp()) / "data")
    m.save_config("TQQQ", CFG)
    st = m.get_state("TQQQ")
    st.holdings, st.avg_price, st.cash = 57, 70.0, 5000.0
    m.save_state(st)

    draft = FixDraft(ticker="TQQQ", side="buy", qty=3, price=68.10)
    text = preview_text(st, draft, st.fee_rate)

    res = m.add_manual_correction("u1", "TQQQ",
                                  {"qty": 3, "price": 68.10, "side": "buy"})
    after = res["after"]
    assert f"{after['holdings']}주" in text
    assert f"{after['avg_price']:.4f}" in text
    assert f"{after['cash']:,.2f}" in text


def test_preview_warns_on_oversell():
    st = PositionState(ticker="TQQQ", division=40, principal=20000.0,
                       fee_rate=0.0007, holdings=5, avg_price=70.0, cash=100.0)
    text = preview_text(st, FixDraft(ticker="TQQQ", side="sell", qty=50, price=70.0),
                        0.0007)
    assert "적용되지 않습니다" in text


def test_callback_roundtrip():
    assert parse_cb(cb("t", "TQQQ")) == ["t", "TQQQ"]
    assert parse_cb(cb("q", 3)) == ["q", "3"]
    assert parse_cb("other:data") == []


def test_callback_data_fits_telegram_limit():
    """telegram callback_data 는 64바이트 제한"""
    for data in [cb("t", "TQQQ"), cb("s", "sell"), cb("q", "manual"), cb("ok"), cb("x")]:
        assert len(data.encode()) <= 64


def test_session_isolates_users():
    s = FixSession()
    s.get("111").ticker = "TQQQ"
    s.get("222").ticker = "SOXL"
    assert s.get("111").ticker == "TQQQ"
    s.reset("111")
    assert s.get("111").ticker == ""
    assert s.get("222").ticker == "SOXL"


# ================================================================
# T값 조정
#
# 회로차단기는 정지하면서 이미 수량·평단을 증권사 기준으로 교정한다.
# 그래서 정지 직후에는 수량 차이가 0이고, 남는 문제는 T값이다.
# 여기서 수량을 또 보정하면 오히려 어긋난다.
# ================================================================

def test_gap_is_zero_after_auto_correct():
    from core.reconciler import apply_reconcile, reconcile

    m = StateManager(Path(tempfile.mkdtemp()) / "data")
    m.save_config("TQQQ", CFG)
    st = m.get_state("TQQQ")
    st.T, st.holdings, st.avg_price, st.cash = 8.0, 57, 70.0, 5000.0
    m.save_state(st)

    broker = {"poss_qty": 60, "avg_price": 69.905}
    st = m.get_state("TQQQ")
    apply_reconcile(st, reconcile(st, broker, deposit_usd=5000.0), broker)

    gap, hint = gap_hint(st, broker)
    assert gap == 0
    assert "이미 맞습니다" in hint
    assert "T값" in hint


def test_t_button_comes_first_when_no_gap():
    """차이가 없으면 T 조정을 먼저 보여준다"""
    first = _rows(keyboard_side(gap=0))[0][0].text
    assert "T값" in first
    first_with_gap = _rows(keyboard_side(gap=3))[0][0].text
    assert "매수" in first_with_gap


def test_t_keyboard_offers_derived_value():
    kb = keyboard_t(current=8.0, derived=8.3886)
    assert "8.3886" in _rows(kb)[0][0].text


def test_t_keyboard_skips_identical_derived():
    kb = keyboard_t(current=8.0, derived=8.0)
    assert "직접 입력" in _rows(kb)[0][0].text


def test_t_preview_shows_impact():
    """T 는 1회매수액과 별지점을 동시에 움직인다"""
    st = PositionState(ticker="TQQQ", division=40, principal=20000.0,
                       fee_rate=0.0007, T=8.0, holdings=60, avg_price=70.0, cash=5000.0)
    text = t_preview_text(st, 8.3886, 8.3886)
    assert "8.0000 → 8.3886" in text
    assert "1회매수액" in text
    assert "별지점" in text


def test_t_preview_warns_on_exhaustion():
    st = PositionState(ticker="TQQQ", division=40, principal=20000.0,
                       fee_rate=0.0007, T=38.0, holdings=60, avg_price=70.0, cash=500.0)
    assert "리버스모드로 전환" in t_preview_text(st, 39.5, None)


def test_set_t_draft_summary():
    d = FixDraft(ticker="TQQQ", side="setT", new_T=8.3886)
    assert "T값" in d.summary()
    assert "8.3886" in d.summary()


def test_set_t_handler_wired():
    src = (ROOT / "tg_bot" / "bot.py").read_text(encoding="utf-8")
    assert "_apply_set_t" in src
    assert 'draft.side == "setT"' in src
    assert "T_input" in src


def test_handlers_registered():
    src = (ROOT / "tg_bot" / "bot.py").read_text(encoding="utf-8")
    assert 'CommandHandler("stats"' in src
    assert 'CommandHandler("fix", self._cmd_fix_ui)' in src
    assert 'CallbackQueryHandler(' in src
    assert 'pattern=r"^fix:"' in src


def test_fix_text_input_consumed_before_routing():
    """수량·가격 입력이 한글 라우팅으로 새면 안 된다"""
    src = (ROOT / "tg_bot" / "bot.py").read_text(encoding="utf-8")
    assert src.index("_on_fix_text(update, context)") < src.index('"상태" in text')


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
