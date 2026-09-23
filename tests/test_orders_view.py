"""
================================================================
주문 내역 · 직접 취소 후 재주문 테스트

  - /orders 가 실시간 미체결을 보여주고 봇/직접 주문을 구분한다
  - 앱에서 LOC 를 직접 취소한 뒤 /run loc 하면 다시 접수된다

실행:  python tests/test_orders_view.py
================================================================
"""

import asyncio
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules.setdefault(name, m)
    return sys.modules[name]


class _Any:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return _Any()
    def __call__(self, *a, **k): return _Any()


try:
    import telegram  # noqa: F401
except ImportError:
    tg = _stub("telegram")
    tge = _stub("telegram.ext")
    tgc = _stub("telegram.constants")
    for n in ["Update", "InlineKeyboardButton", "InlineKeyboardMarkup",
              "ReplyKeyboardMarkup", "Bot", "BotCommand"]:
        setattr(tg, n, type(n, (_Any,), {}))
    setattr(tge, "ContextTypes", type("ContextTypes", (), {"DEFAULT_TYPE": _Any}))
    setattr(tge, "ConversationHandler", type("ConversationHandler", (_Any,), {"END": -1}))
    setattr(tge, "ApplicationHandlerStop", type("ApplicationHandlerStop", (Exception,), {}))
    for n in ["Application", "ApplicationBuilder", "CommandHandler",
              "CallbackQueryHandler", "MessageHandler", "TypeHandler"]:
        setattr(tge, n, _Any)
    setattr(tge, "filters", _Any())
    setattr(tgc, "ParseMode", type("ParseMode", (), {"HTML": "HTML"}))

for mod, attrs in [("aiohttp", {"ClientSession": object,
                                "ClientTimeout": lambda **k: None,
                                "ClientError": Exception}),
                   ("websockets", {"connect": None})]:
    try:
        __import__(mod)
    except ImportError:
        _stub(mod, **attrs)
try:
    import apscheduler  # noqa: F401
except ImportError:
    for p in ["apscheduler", "apscheduler.schedulers", "apscheduler.triggers"]:
        _stub(p)
    _stub("apscheduler.schedulers.asyncio", AsyncIOScheduler=object)
    _stub("apscheduler.triggers.cron", CronTrigger=object)
    _stub("apscheduler.triggers.date", DateTrigger=object)

from core.order_registry import OrderRegistry  # noqa: E402
from core.state_manager import StateManager  # noqa: E402
from core.t_calculator import FillKind  # noqa: E402
from kiwoom.constants import TradeType  # noqa: E402
from modes.base_mode import PlannedOrder  # noqa: E402

CFG = {"division": 40, "principal": 20000.0, "fee_rate": 0.0007, "is_active": True}
DAY = "20260922"


def _setup():
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    sm.save_config("SOXL", CFG)
    return sm, OrderRegistry(d / "o.db")


def _record(reg, tag, side, tt, qty, price, ord_no):
    rid = reg.record_submission(DAY, "SOXL", PlannedOrder(
        tag=tag, side=side, trade_type=tt, qty=qty, price=price))
    reg.attach_ord_no(rid, ord_no)
    return rid


# ================================================================
# /orders
# ================================================================

def _handler(sm, reg, live, rsv=()):
    from tg_bot.commands_handler import CommandsHandler
    h = CommandsHandler.__new__(CommandsHandler)
    h.state, h.registry = sm, reg

    class K:
        async def get_open_orders(self, t, ex): return list(live)
        async def get_reserved_orders(self, ticker="", exchange="", **k): return list(rsv)
    h.kiwoom = K()

    sent = []

    async def _reply(msg, text, **k):
        sent.append(text)
    h._safe_reply = _reply

    async def _uid(u): return "1"
    h._get_user_id = _uid
    return h, sent


class _U:
    effective_message = object()


class _C:
    args = []


def test_orders_shows_realtime_open_orders():
    """원본은 예약주문만 조회해서 실시간 주문이 있어도 '없음' 이었다"""
    sm, reg = _setup()
    _record(reg, FillKind.ENTRY_BUY, "buy", TradeType.LOC, 3, 163.11, "000010928")
    live = [{"ord_no": "000010928", "side": "buy", "trade_type_nm": "Limit On Close",
             "remain_qty": 3, "ord_uv": 163.11}]
    h, sent = _handler(sm, reg, live)
    asyncio.run(h.cmd_orders(_U(), _C()))
    text = sent[0]
    assert "미체결 1건" in text
    assert "163.11" in text
    assert "처음매수" in text
    assert "LOC" in text          # "Limit On Close" 는 휴대폰에서 줄이 꺾인다


def test_orders_distinguishes_bot_and_personal():
    sm, reg = _setup()
    _record(reg, FillKind.CRASH_BUY, "buy", TradeType.LOC, 1, 125.0, "000010929")
    live = [{"ord_no": "000010929", "side": "buy", "trade_type_nm": "Limit On Close",
             "remain_qty": 1, "ord_uv": 125.0},
            {"ord_no": "000099999", "side": "sell", "trade_type_nm": "지정가",
             "remain_qty": 5, "ord_uv": 200.0}]
    h, sent = _handler(sm, reg, live)
    asyncio.run(h.cmd_orders(_U(), _C()))
    text = sent[0]
    assert "🤖 🔴매수" in text and "폭락대비" in text
    assert "👤 🔵매도" in text and "직접 주문" in text


def test_orders_empty():
    sm, reg = _setup()
    h, sent = _handler(sm, reg, [])
    asyncio.run(h.cmd_orders(_U(), _C()))
    assert "미체결 0건" in sent[0]


def test_orders_shows_reserved_too():
    sm, reg = _setup()
    rsv = [{"rsrv_ord_no": "R1", "slby_tp": "매도", "trde_nm": "Market On Close",
            "ord_qty": "000000000010", "ord_uv": "0.0000", "proc_tp": "미처리"}]
    h, sent = _handler(sm, reg, [], rsv)
    asyncio.run(h.cmd_orders(_U(), _C()))
    assert "예약주문 1건" in sent[0]


def test_registry_wired_to_orders_view():
    src = (ROOT / "tg_bot" / "bot.py").read_text(encoding="utf-8")
    assert 'self.cmd_handler.registry = getattr(scheduler, "registry", None)' in src


# ================================================================
# 앱에서 직접 취소 → /run loc 재접수
# ================================================================

def _engine(sm, reg, open_nos, fail_query=False):
    from scheduler.engine import SchedulerEngine
    sent = []

    class K:
        async def get_open_orders(self, t, ex):
            if fail_query:
                raise RuntimeError("조회 실패")
            return [{"ord_no": n} for n in open_nos]

    class N:
        async def send(self, text): sent.append(text)

    eng = SchedulerEngine.__new__(SchedulerEngine)
    eng.state_mgr, eng.registry, eng.kiwoom, eng.notifier = sm, reg, K(), N()
    return eng, sent


def test_cancelled_loc_marked_in_registry():
    """LOC 는 종가 전엔 체결될 수 없다. 사라졌으면 직접 취소한 것이다."""
    sm, reg = _setup()
    rid = _record(reg, FillKind.ENTRY_BUY, "buy", TradeType.LOC, 3, 163.11, "A1")
    eng, sent = _engine(sm, reg, open_nos=[])
    assert asyncio.run(eng._sync_cancelled_loc("SOXL", DAY)) == 1
    assert [r.status for r in reg.day_orders(DAY, "SOXL") if r.id == rid] == ["cancelled"]
    assert sent and "다시 접수" in sent[0]


def test_live_loc_left_alone():
    sm, reg = _setup()
    _record(reg, FillKind.ENTRY_BUY, "buy", TradeType.LOC, 3, 163.11, "A1")
    eng, _ = _engine(sm, reg, open_nos=["A1"])
    assert asyncio.run(eng._sync_cancelled_loc("SOXL", DAY)) == 0


def test_limit_sell_not_touched():
    """지정가는 장중 체결일 수 있어 사라져도 취소로 보지 않는다"""
    sm, reg = _setup()
    _record(reg, FillKind.TARGET_SELL, "sell", TradeType.LIMIT, 3, 195.0, "A2")
    eng, _ = _engine(sm, reg, open_nos=[])
    assert asyncio.run(eng._sync_cancelled_loc("SOXL", DAY)) == 0


def test_query_failure_changes_nothing():
    """확인 없이 취소로 표시하면 살아 있는 주문을 한 번 더 내게 된다"""
    sm, reg = _setup()
    _record(reg, FillKind.ENTRY_BUY, "buy", TradeType.LOC, 3, 163.11, "A1")
    eng, _ = _engine(sm, reg, open_nos=[], fail_query=True)
    assert asyncio.run(eng._sync_cancelled_loc("SOXL", DAY)) == 0
    assert all(r.status == "submitted" for r in reg.day_orders(DAY, "SOXL"))


def test_rerun_after_manual_cancel_resubmits():
    """앱에서 전부 취소 → /run loc → 다시 접수돼야 한다"""
    from scheduler.engine import SubmitWindow

    sm, reg = _setup()
    placed, sent = [], []
    broker_open = set()

    class K:
        async def available_usd(self, t, ex, price): return 100000.0
        async def get_quote(self, t, ex): return {"cur_price": 141.83, "prev_close": 141.83}
        async def get_recent_closes(self, t, ex, n=5): return [141.83] * 5
        async def get_open_orders(self, t, ex): return [{"ord_no": n} for n in broker_open]

    class N:
        async def send(self, text): sent.append(text)

    class Cfg:
        dry_run = False

    class R:
        def __init__(self, n): self.ord_no = n

    async def _place(t, ex, order):
        no = f"N{len(placed)}"
        placed.append(order)
        broker_open.add(no)
        return R(no)

    from scheduler.engine import SchedulerEngine
    eng = SchedulerEngine.__new__(SchedulerEngine)
    eng.state_mgr, eng.registry, eng.kiwoom = sm, reg, K()
    eng.notifier, eng.config, eng._place = N(), Cfg(), _place

    asyncio.run(eng._submit_ticker("SOXL", DAY, SubmitWindow.REGULAR, "LOC 접수"))
    first = len(placed)
    assert first > 0

    broker_open.clear()                          # 앱에서 전부 취소
    asyncio.run(eng._submit_ticker("SOXL", DAY, SubmitWindow.REGULAR, "LOC 접수"))
    assert len(placed) == first * 2, "취소 후 재접수가 막혔다"


def test_rerun_without_cancel_is_still_blocked():
    """취소하지 않았으면 여전히 중복으로 막혀야 한다"""
    from scheduler.engine import SubmitWindow

    sm, reg = _setup()
    placed = []
    broker_open = set()

    class K:
        async def available_usd(self, t, ex, price): return 100000.0
        async def get_quote(self, t, ex): return {"cur_price": 141.83, "prev_close": 141.83}
        async def get_recent_closes(self, t, ex, n=5): return [141.83] * 5
        async def get_open_orders(self, t, ex): return [{"ord_no": n} for n in broker_open]

    class N:
        async def send(self, text): pass

    class Cfg:
        dry_run = False

    class R:
        def __init__(self, n): self.ord_no = n

    async def _place(t, ex, order):
        no = f"N{len(placed)}"
        placed.append(order)
        broker_open.add(no)
        return R(no)

    from scheduler.engine import SchedulerEngine
    eng = SchedulerEngine.__new__(SchedulerEngine)
    eng.state_mgr, eng.registry, eng.kiwoom = sm, reg, K()
    eng.notifier, eng.config, eng._place = N(), Cfg(), _place

    asyncio.run(eng._submit_ticker("SOXL", DAY, SubmitWindow.REGULAR, "LOC 접수"))
    first = len(placed)
    asyncio.run(eng._submit_ticker("SOXL", DAY, SubmitWindow.REGULAR, "LOC 접수"))
    assert len(placed) == first, "살아 있는 주문이 중복으로 나갔다"


# ================================================================
# 예약 검증 오보 — 거둔 예약을 거부로 신고하지 않는다
#
# 실측 2026-09-22: 17:20 예약 6건을 17:38 /panic 으로 거두고 실시간으로
# 다시 냈다. 22:40 개장 검증이 거둔 6건을 "거부" 로 신고했다 (사유 빈칸).
# ================================================================

def _rsv_row(no, cncl, proc="미처리", err=""):
    return {"rsrv_ord_no": no, "stk_cd": "SOXL", "ord_qty": "000000000001",
            "ord_uv": "125.0000", "rsrv_cncl_yn": cncl, "proc_tp": proc, "err_cntn": err}


def _api_with(rows):
    from kiwoom.api_client import KiwoomAPIClient
    api = KiwoomAPIClient.__new__(KiwoomAPIClient)

    async def _get(**k):
        return rows
    api.get_reserved_orders = _get
    return api


def test_cancelled_reservation_is_not_rejection():
    api = _api_with([_rsv_row("R1", "취소")])
    assert asyncio.run(api.verify_reserved_orders("SOXL", "NY")) == []


def test_error_is_rejection():
    api = _api_with([_rsv_row("R1", "미취소", proc="처리중에러", err="증거금 부족")])
    bad = asyncio.run(api.verify_reserved_orders("SOXL", "NY"))
    assert len(bad) == 1 and bad[0]["reason"] == "증거금 부족"


def test_error_text_alone_is_rejection():
    api = _api_with([_rsv_row("R1", "미취소", proc="정상처리", err="가격제한폭 초과")])
    assert len(asyncio.run(api.verify_reserved_orders("SOXL", "NY"))) == 1


def test_void_is_rejection():
    api = _api_with([_rsv_row("R1", "무효")])
    assert len(asyncio.run(api.verify_reserved_orders("SOXL", "NY"))) == 1


def test_normal_pending_is_quiet():
    api = _api_with([_rsv_row("R1", "미취소")])
    assert asyncio.run(api.verify_reserved_orders("SOXL", "NY")) == []


def test_scheduler_skips_reservations_already_cancelled_in_registry():
    """원장에서 이미 취소로 기록한 예약은 알리지 않는다"""
    src = (ROOT / "scheduler" / "engine.py").read_text(encoding="utf-8")
    block = src[src.index("async def verify_reserved"):src.index("async def _sync_cancelled_loc")]
    assert "bot_reserved_orders(ticker)" in block
    assert 'b.get("rsrv_ord_no")) in ours' in block


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
