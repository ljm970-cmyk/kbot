"""
================================================================
T값 글자 그래프 테스트 — 아침 요약의 진행 막대와 T 흐름

실행:  python tests/test_tchart.py
================================================================
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.order_registry import OrderRegistry  # noqa: E402
from core.state_manager import StateManager  # noqa: E402
from core.tchart import (  # noqa: E402
    EMPTY,
    FILL,
    HALF_MARK,
    cycle_t_history,
    phase_of,
    progress_bar,
    sparkline,
    t_flow_line,
)

CFG = {"division": 40, "principal": 20000.0, "fee_rate": 0.0007, "is_active": True}


def _sm(history):
    sm = StateManager(Path(tempfile.mkdtemp()) / "data")
    sm.save_config("SOXL", CFG)
    for d, T, h in history:
        sm.archive_eod("SOXL", d, {"T": T, "holdings": h})
    return sm


# ================================================================
# 진행 막대
# ================================================================

def test_bar_shape_and_phase():
    bar = progress_bar(7.0, 40)
    cells = bar.split(" ")[0]
    assert cells.count(FILL) == 4 and cells.count(EMPTY) == 16
    assert cells.index(HALF_MARK) == 10, "가운데가 전반·후반 경계여야 한다"
    assert "18% · 전반전 (후반까지 13.0)" in bar


def test_bar_shows_one_cell_once_started():
    assert progress_bar(0.5, 40).split(" ")[0].count(FILL) == 1


def test_bar_second_half():
    bar = progress_bar(25.0, 40)
    assert "후반전" in bar and "소진까지 14.0" in bar


def test_bar_20_division():
    bar = progress_bar(10.0, 20)
    assert "50%" in bar and "후반전" in bar


def test_bar_reverse_and_exhausted():
    assert "리버스" in progress_bar(38.0, 40, "reverse")
    assert phase_of(39.5, 40, "normal") == "소진"


def test_bar_caps_at_full():
    cells = progress_bar(45.0, 40).split(" ")[0]
    assert EMPTY not in cells


# ================================================================
# T 흐름
# ================================================================

def test_sparkline_relative_and_dip():
    """쿼터매도로 T 가 줄어든 날은 움푹 들어가야 한다"""
    line = sparkline([4.0, 6.0, 8.0, 6.0, 7.0])
    assert line[0] == "▁" and line[2] == "█"
    assert line[3] < line[2]


def test_sparkline_flat():
    assert len(set(sparkline([3.0, 3.0, 3.0]))) == 1


def test_history_only_current_cycle():
    """보유 0 인 날(직전 사이클)에서 끊는다"""
    sm = _sm([
        ("20260901", 12.0, 30), ("20260902", 0.0, 0),        # 직전 사이클 종료
        ("20260903", 1.0, 3), ("20260904", 2.0, 6), ("20260905", 3.0, 9),
    ])
    hist = cycle_t_history(sm, "SOXL")
    assert [d for d, _ in hist] == ["20260903", "20260904", "20260905"]


def test_history_same_day_uses_latest():
    """같은 날 정산이 두 번 기록됐으면 나중 값"""
    sm = _sm([("20260903", 1.0, 3), ("20260904", 2.0, 6), ("20260904", 1.5, 5)])
    assert cycle_t_history(sm, "SOXL")[-1] == ("20260904", 1.5)


def test_history_limited_points():
    rows = [(f"202609{d:02d}", float(d), d) for d in range(1, 29)]
    assert len(cycle_t_history(_sm(rows), "SOXL", max_points=20)) == 20


def test_flow_line_text():
    line = t_flow_line([("20260922", 1.0), ("20261002", 7.0)])
    assert "9/22 1.0 → 10/2 7.0" in line


def test_flow_line_needs_two_points():
    assert t_flow_line([("20260922", 1.0)]) == ""


# ================================================================
# 아침 요약
# ================================================================

def _brief(sm, T, holdings):
    from core.ops import morning_brief
    st = sm.get_state("SOXL")
    st.T, st.holdings, st.avg_price, st.cash = T, holdings, 148.32, 17179.86
    sm.save_state(st)
    return morning_brief(sm, OrderRegistry(Path(tempfile.mkdtemp()) / "o.db"), None, 18000.0)


def test_morning_brief_has_bar_and_flow():
    sm = _sm([("20260922", 1.0, 3), ("20260923", 2.0, 6), ("20261002", 7.0, 19)])
    txt = _brief(sm, 7.0, 19)
    assert HALF_MARK in txt and "전반전" in txt
    assert "T 흐름" in txt


def test_morning_brief_no_chart_between_cycles():
    """사이클 사이(보유 0, T 0)에는 그래프를 그리지 않는다"""
    txt = _brief(_sm([]), 0.0, 0)
    assert HALF_MARK not in txt and "T 흐름" not in txt


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
