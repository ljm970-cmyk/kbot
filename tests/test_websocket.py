"""
================================================================
F5 실시간 체결 파싱 테스트

키움 문서의 Response Example 을 그대로 넣어 검증한다.
필드 코드가 틀리면 모든 체결이 0으로 들어오고 T값이 통째로 어긋난다.

실행:  python tests/test_websocket.py
================================================================
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# websockets 는 연결에만 쓰인다. 파싱 검증은 패키지 없이도 돌아야 한다.
try:
    import websockets  # noqa: F401
except ImportError:
    import types
    _stub = types.ModuleType("websockets")
    _stub.connect = None
    sys.modules["websockets"] = _stub
    print("  (websockets 미설치 — 파싱 테스트만 실행)")

from kiwoom.websocket_handler import F5, WebSocketFillReceiver, _int, _num


def _rx() -> WebSocketFillReceiver:
    return WebSocketFillReceiver(
        access_token="t", db_path=Path(tempfile.mkdtemp()) / "fills.db")


#: 키움 문서 F5 Response Example (주문 접수 시점 — 체결량 0)
DOC_ACCEPT = {
    "302": "엔비디아", "900": "1", "901": "0000198.4200", "902": "1",
    "904": "000000000", "905": "25", "907": "02", "908": "105247",
    "909": "000000000", "910": "000000000000", "911": "0", "913": "접수",
    "930": "54", "931": "0000185.0305", "1091": "미국", "8043": "USD",
    "8046": "000030", "9001": "NVDA", "9201": "8985024710", "9203": "000000027",
    "50072": "매수", "50073": "지정가", "50810": "0000000.0000", "50841": "0",
}

#: 같은 주문이 LOC 로 체결된 상황 (주문가 76.02, 종가 68.10)
LOC_FILL = {
    **DOC_ACCEPT,
    "9001": "TQQQ", "901": "0000076.0200",
    "910": "0000068.1000", "911": "3", "902": "0", "913": "체결완료",
    "909": "000000101", "50073": "LOC", "907": "02",
}

SELL_FILL = {
    **LOC_FILL, "907": "01", "911": "35",
    "901": "0000059.5500", "910": "0000059.5500", "909": "000000102",
}


# ================================================================
# 숫자 파싱
# ================================================================

def test_zero_padded_numbers():
    """F5 값은 0 패딩 문자열로 온다"""
    assert _num("0000198.4200") == 198.42
    assert _num("000000000000") == 0.0
    assert _int("000000000005") == 5
    assert _num("") == 0.0


# ================================================================
# 체결 판정
# ================================================================

def test_accept_event_is_not_a_fill():
    """F5 는 주문 접수 시점에도 온다 (913='접수', 911='0').
    체결량 0을 체결로 세면 T값이 어긋난다."""
    assert _rx()._parse(DOC_ACCEPT) is None


def test_loc_fill_parsed():
    f = _rx()._parse(LOC_FILL)
    assert f is not None
    assert f.ticker == "TQQQ"
    assert f.side == "buy"                 # 907 = "02"
    assert f.fill_qty == 3
    assert f.fill_price == 68.10           # 910 체결가
    assert f.order_price == 76.02          # 901 주문가 — 태그 매칭 키
    assert f.trade_type == "30"            # LOC
    assert f.status == "체결완료"


def test_order_price_differs_from_fill_price():
    """LOC 은 주문가와 체결가가 다르다. 둘을 구분하지 못하면
    어떤 주문이 체결됐는지 복원할 수 없다."""
    f = _rx()._parse(LOC_FILL)
    assert f.order_price != f.fill_price


def test_sell_side_parsed():
    """907: 01=매도, 02=매수"""
    f = _rx()._parse(SELL_FILL)
    assert f.side == "sell"
    assert f.fill_qty == 35


def test_order_no_zero_stripped():
    """주문번호는 0 패딩으로 온다"""
    f = _rx()._parse(LOC_FILL)
    assert f.ord_no == "27"                # "000000027"
    assert f.fill_no == "101"


def test_broker_position_captured():
    """증권사 기준 보유수량·매입단가를 받아 전략 장부와 대조한다"""
    f = _rx()._parse(LOC_FILL)
    assert f.broker_holdings == 54
    assert abs(f.broker_avg_price - 185.0305) < 1e-9


def test_trade_kind_mapping():
    """매매구분은 텍스트로만 오므로 코드로 역매핑해야 한다"""
    rx = _rx()
    assert rx._parse({**LOC_FILL, "50073": "지정가"}).trade_type == "00"
    assert rx._parse({**LOC_FILL, "50073": "MOC"}).trade_type == "33"
    assert rx._parse({**LOC_FILL, "50073": "시장가"}).trade_type == "03"
    assert rx._parse({**LOC_FILL, "50073": "알수없음"}).trade_type == ""


# ================================================================
# 저장 · 조회
# ================================================================

def test_save_and_fills_for():
    import asyncio
    rx = _rx()
    f = rx._parse(LOC_FILL)
    rx._save(f)

    events = asyncio.run(rx.fills_for(f.trade_date, "TQQQ"))
    assert len(events) == 1
    e = events[0]
    assert e.order_price == 76.02
    assert e.price == 68.10
    assert e.trade_type == "30"
    assert e.source == "websocket"


def test_duplicate_fill_ignored():
    """같은 (주문번호, 체결번호)는 한 번만 저장된다"""
    import asyncio
    rx = _rx()
    f = rx._parse(LOC_FILL)
    rx._save(f)
    rx._save(f)
    assert len(asyncio.run(rx.fills_for(f.trade_date, "TQQQ"))) == 1


def test_broker_position_lookup():
    rx = _rx()
    f = rx._parse(LOC_FILL)
    rx._save(f)
    pos = rx.broker_position(f.trade_date, "TQQQ")
    assert pos == (54, 185.0305)


# ================================================================
# 토큰
# ================================================================

def test_token_callback_reflects_refresh():
    """토큰을 복사해 두면 갱신 후 옛 토큰으로 재연결한다.
    콜백으로 받아야 항상 최신 토큰을 쓴다."""
    holder = {"t": "old"}
    rx = WebSocketFillReceiver(access_token=lambda: holder["t"],
                               db_path=Path(tempfile.mkdtemp()) / "f.db")
    assert rx.token == "old"
    holder["t"] = "new"
    assert rx.token == "new"


def test_static_token_still_works():
    rx = WebSocketFillReceiver(access_token="abc",
                               db_path=Path(tempfile.mkdtemp()) / "f.db")
    assert rx.token == "abc"


# ================================================================
# 필드 코드
# ================================================================

def test_field_codes_match_spec():
    """문서에 적힌 코드와 일치하는지 고정"""
    assert (F5.TICKER, F5.ORD_NO, F5.SIDE) == ("9001", "9203", "907")
    assert (F5.ORD_PRICE, F5.FILL_PRICE, F5.FILL_QTY) == ("901", "910", "911")
    assert (F5.STATUS, F5.HOLDINGS, F5.AVG_PRICE) == ("913", "930", "931")


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
