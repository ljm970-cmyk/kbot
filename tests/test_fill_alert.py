"""
================================================================
실시간 체결 알림

  - WebSocket 으로 체결이 들어오면 잠깐 모았다가 종목별로 한 번 보낸다
  - 봇 주문은 태그 이름(별지점 매수, 쿼터매도...)으로, 직접 주문은 👤 로
  - 같은 체결이 다시 오면(재연결) 두 번 알리지 않는다
  - 목표매도 체결은 사이클 종료 안내를 붙인다

실행:  python tests/test_fill_alert.py
================================================================
"""

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import websockets  # noqa: F401
except ImportError:
    import types
    _stub = types.ModuleType("websockets")
    _stub.connect = None
    sys.modules["websockets"] = _stub

from core.fill_alert import FillAlerter, build_message  # noqa: E402
from core.order_registry import OrderRegistry  # noqa: E402
from core.t_calculator import FillKind  # noqa: E402
from kiwoom.constants import TradeType  # noqa: E402
from kiwoom.websocket_handler import WebSocketFillReceiver  # noqa: E402
from modes.base_mode import PlannedOrder  # noqa: E402

BASE = {
    "302": "x", "900": "3", "901": "0000076.0200", "902": "0",
    "904": "000000000", "905": "10", "907": "02", "908": "050000",
    "909": "000000101", "910": "0000068.1000", "911": "3", "913": "체결완료",
    "1091": "미국", "8046": "000030", "9001": "TQQQ", "9203": "000000027",
    "50073": "LOC",
}


def _msg(**over):
    return {"trnm": "REAL", "data": [{"type": "F5", "item": "TQQQ",
                                      "values": {**BASE, **over}}]}


class _N:
    def __init__(self):
        self.sent = []

    async def send(self, t):
        self.sent.append(t)


def _setup():
    d = Path(tempfile.mkdtemp())
    reg = OrderRegistry(d / "o.db")
    rx = WebSocketFillReceiver(access_token="t", db_path=d / "fills.db")
    n = _N()
    al = FillAlerter(reg, lambda: n, delay=0.01)
    rx.add_callback(al.on_fill)
    return reg, rx, al, n


def _order(reg, tag, side, qty, price, ord_no, tt=TradeType.LOC):
    rid = reg.record_submission("20261021", "TQQQ",
                                PlannedOrder(tag=tag, side=side, trade_type=tt, qty=qty, price=price))
    reg.attach_ord_no(rid, ord_no)


def _feed(rx, *msgs):
    async def go():
        for m in msgs:
            await rx._handle(m)
        await asyncio.sleep(0.05)
    asyncio.run(go())


def test_bot_fills_batched_with_labels():
    reg, rx, _, n = _setup()
    _order(reg, FillKind.STAR_BUY, "buy", 3, 76.02, "27")
    _order(reg, FillKind.QUARTER_SELL, "sell", 2, 80.71, "28")
    _feed(rx, _msg(),
          _msg(**{"9203": "000000028", "907": "01", "901": "0000080.7100",
                  "910": "0000080.9000", "911": "2", "909": "000000102"}))
    assert len(n.sent) == 1
    t = n.sent[0]
    assert "⚡ [TQQQ] 체결 2건" in t
    assert "🔴 별지점 매수 3주 @68.10 (주문 @76.02)" in t
    assert "🔵 쿼터매도 2주 @80.90 (주문 @80.71)" in t
    assert "정산 때 반영" in t


def test_duplicate_fill_not_alerted_twice():
    reg, rx, _, n = _setup()
    _order(reg, FillKind.STAR_BUY, "buy", 3, 76.02, "27")
    _feed(rx, _msg(), _msg())
    assert len(n.sent) == 1 and "체결 1건" in n.sent[0]


def test_accept_event_not_alerted():
    _, rx, _, n = _setup()
    _feed(rx, _msg(**{"911": "0", "913": "접수", "910": "000000000000"}))
    assert n.sent == []


def test_manual_order_marked():
    _, rx, _, n = _setup()
    _feed(rx, _msg(**{"9203": "000000999"}))
    assert "👤 직접 주문 매수 3주" in n.sent[0]


def test_target_sell_adds_cycle_hint():
    reg, rx, _, n = _setup()
    _order(reg, FillKind.TARGET_SELL, "sell", 15, 90.0, "40", TradeType.LIMIT)
    _feed(rx, _msg(**{"9203": "000000040", "907": "01", "901": "0000090.0000",
                      "910": "0000090.0000", "911": "15"}))
    assert "목표매도 15주 @90.00" in n.sent[0]
    assert "(주문 @" not in n.sent[0]           # 지정가는 주문가 = 체결가
    assert "사이클 종료" in n.sent[0]


def test_partial_fill_marked():
    reg, rx, _, n = _setup()
    _order(reg, FillKind.TARGET_SELL, "sell", 15, 90.0, "40", TradeType.LIMIT)
    _feed(rx, _msg(**{"9203": "000000040", "907": "01", "901": "0000090.0000",
                      "910": "0000090.0000", "911": "5", "913": "부분체결"}))
    assert "부분체결" in n.sent[0]


def test_two_tickers_two_messages():
    reg, rx, _, n = _setup()
    _feed(rx, _msg(), _msg(**{"9001": "SOXL", "909": "000000555", "9203": "000000777"}))
    assert len(n.sent) == 2
    assert any("[SOXL]" in s for s in n.sent) and any("[TQQQ]" in s for s in n.sent)


def test_no_notifier_is_safe():
    d = Path(tempfile.mkdtemp())
    al = FillAlerter(OrderRegistry(d / "o.db"), lambda: None, delay=0.01)
    rx = WebSocketFillReceiver(access_token="t", db_path=d / "f.db")
    rx.add_callback(al.on_fill)
    _feed(rx, _msg())


def test_message_reverse_moc():
    class F:
        ord_no, side, fill_qty, fill_price, order_price, status = "1", "sell", 30, 50.0, 0.0, "체결완료"
    rec = type("R", (), {"tag": FillKind.REVERSE_MOC_SELL})()
    assert "리버스 첫날 매도 체결" in build_message("SOXL", [(F(), rec)])


def test_main_wires_alerter():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "FillAlerter(registry" in src and "add_callback" in src


def test_fill_during_send_not_stuck():
    """보내는 도중 들어온 체결도 다음 체결을 기다리지 않고 알린다"""
    d = Path(tempfile.mkdtemp())
    reg = OrderRegistry(d / "o.db")
    rx = WebSocketFillReceiver(access_token="t", db_path=d / "f.db")
    sent = []

    class SlowN:
        async def send(self, t):
            if not sent:
                await rx._handle(_msg(**{"909": "000000999", "9203": "000000888"}))
            sent.append(t)
    al = FillAlerter(reg, lambda: SlowN(), delay=0.01)
    rx.add_callback(al.on_fill)
    _feed(rx, _msg())
    assert len(sent) == 2


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
