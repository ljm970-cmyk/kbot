"""
================================================================
사이클 종료 후 새 원금 · 휴장 안내 · 원화 입금 안내

실행:  python tests/test_principal.py
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
    setattr(tgc, "ParseMode", type("ParseMode", (), {"HTML": "HTML", "MARKDOWN": "MD"}))

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

from core.reconciler import CircuitBreaker  # noqa: E402
from core.state_manager import StateManager  # noqa: E402
from tg_bot.bot import KbotTelegramBot  # noqa: E402

from datetime import date  # noqa: E402

from core import principal as P  # noqa: E402
from core.fx import krw_for_usd, krw_text, latest_rate  # noqa: E402
from core.funding import FundingCheck, account_summary  # noqa: E402
from core.market_notice import holiday_name, notice_lines  # noqa: E402
from core.order_registry import OrderRegistry  # noqa: E402
from kiwoom.constants import TradeType  # noqa: E402

CFG = {"ticker": "SOXL", "division": 40, "principal": 20000.0, "fee_rate": 0.0007, "is_active": True}


def _sm(cash=21234.56, holdings=0, awaiting=True, close=80.0):
    sm = StateManager(Path(tempfile.mkdtemp()) / "d")
    sm.save_config("SOXL", dict(CFG))
    st = sm.get_state("SOXL")
    st.cash, st.holdings, st.T = cash, holdings, 0.0
    st.avg_price = 0.0 if holdings == 0 else 100.0
    st.awaiting_principal, st.last_close_price = awaiting, close
    sm.save_state(st)
    return sm


# ================================================================
# 원금 — 문구·적용
# ================================================================

def test_prompt_buttons():
    text, rows = P.prompt("SOXL", _sm().get_state("SOXL"), 861.73)
    assert "지난 원금 $20,000" in text and "$21,234.56" in text and "수익 $+1,234.56" in text
    assert "주문가능 $861.73" in text and "대기" in text and "/principal SOXL" in text
    labels = [l for row in rows for l, _ in row]
    datas = [d for row in rows for _, d in row]
    assert labels == ["💰 수익 포함 $21,234.56", "🔒 그대로 $20,000", "✏️ 직접 입력"]
    assert datas == ["PRIN:PROFIT:SOXL", "PRIN:KEEP:SOXL", "PRIN:INPUT:SOXL"]
    assert all(len(d.encode()) <= 64 for d in datas)


def test_apply_keep_resets_cash_to_principal():
    sm = _sm()
    msg = P.apply(sm, "SOXL", 20000.0)
    st = sm.get_state("SOXL")
    assert st.principal == 20000.0 and st.cash == 20000.0 and not st.awaiting_principal
    assert "남는 $1,234.56" in msg and "1회 매수금 $500.00" in msg
    assert st.next_buy_need > 0                        # 다음 매수 필요액 재계산


def test_apply_profit_compounds():
    sm = _sm()
    st = sm.get_state("SOXL")
    P.apply(sm, "SOXL", P.amount_for(st, P.PROFIT))
    st = sm.get_state("SOXL")
    assert st.principal == 21234.56 and st.cash == 21234.56
    assert sm.get_config("SOXL")["principal"] == 21234.56


def test_apply_custom_updates_config_without_double_change():
    sm = _sm()
    P.apply(sm, "SOXL", 25000.0)
    assert sm.get_config("SOXL")["principal"] == 25000.0
    res = sm.apply_config("SOXL")                       # 이후 설정 반영이 또 바꾸지 않는다
    assert res["changed"] == []
    assert sm.get_state("SOXL").cash == 25000.0


def test_apply_refused_mid_cycle():
    sm = _sm(holdings=10, awaiting=False)
    assert "진행 중인 사이클" in P.apply(sm, "SOXL", 30000.0)
    assert sm.get_state("SOXL").principal == 20000.0


def test_too_small_refused():
    st = _sm(close=80.0).get_state("SOXL")
    assert "1주" in P.check_amount(st, 3000.0)        # 3000/40 = 75 < 80
    assert P.check_amount(st, 3200.0) == ""
    assert "⚠️" in P.apply(_sm(close=80.0), "SOXL", 3000.0)


def test_parse_amount():
    assert P.parse_amount("25,000") == 25000.0
    assert P.parse_amount("$15000") == 15000.0
    assert P.parse_amount("abc") is None and P.parse_amount("-5") is None


def test_confirm_shortfall_warning():
    st = _sm().get_state("SOXL")
    text, rows = P.confirm("SOXL", st, 40000.0, 500.0)
    assert "1회 매수금 $1,000.00" in text and "더 필요" in text
    assert rows[0][0][1] == "PRIN:SET:SOXL:40000.00"


# ================================================================
# 스케줄러 — 정산·접수
# ================================================================

class _N:
    def __init__(self):
        self.sent, self.buttons = [], []

    async def send(self, t):
        self.sent.append(t)

    async def send_buttons(self, t, rows):
        self.buttons.append((t, rows))


def _engine(sm):
    from scheduler.engine import SchedulerEngine
    placed = []

    class K:
        async def available_usd(self, t, ex, price): return 5000.0
        async def get_quote(self, t, ex): return {"cur_price": 80.0, "prev_close": 80.0}
        async def get_recent_closes(self, t, ex, n=5): return [80.0] * 5
        async def get_open_orders(self, t, ex): return []
        async def buy(self, *a, **k):
            placed.append(a)
            return type("O", (), {"ord_no": f"B{len(placed)}"})()
        async def sell(self, *a, **k):
            placed.append(a)
            return type("O", (), {"ord_no": f"S{len(placed)}"})()

    eng = SchedulerEngine.__new__(SchedulerEngine)
    eng.state_mgr, eng.registry = sm, OrderRegistry(Path(tempfile.mkdtemp()) / "o.db")
    eng.kiwoom, eng.notifier = K(), _N()
    eng.config = type("C", (), {"dry_run": False})()
    return eng, placed


def test_awaiting_blocks_first_buy_and_reminds():
    from scheduler.engine import SubmitWindow
    eng, placed = _engine(_sm())
    asyncio.run(eng._submit_ticker("SOXL", "20261026", SubmitWindow.PRE_MARKET, "지정가매도"))
    assert placed == [] and eng.notifier.buttons == []          # 프리장은 조용히
    asyncio.run(eng._submit_ticker("SOXL", "20261026", SubmitWindow.REGULAR, "LOC 접수"))
    assert placed == []
    assert len(eng.notifier.buttons) == 1 and "첫 매수를 하지 않았습니다" in eng.notifier.buttons[0][0]


def test_after_choice_orders_go_out():
    from scheduler.engine import SubmitWindow
    sm = _sm()
    P.apply(sm, "SOXL", 20000.0)
    eng, placed = _engine(sm)
    asyncio.run(eng._submit_ticker("SOXL", "20261026", SubmitWindow.REGULAR, "LOC 접수"))
    assert placed                                                # 처음매수 접수


def test_awaiting_ticker_reserves_nothing():
    eng, _ = _engine(_sm())
    assert eng._core_reserve(["SOXL"]) == 0.0


def test_eod_cycle_close_sets_awaiting_and_asks():
    import scheduler.engine as E
    sm = _sm(cash=19000.0, holdings=100, awaiting=False)
    eng, _ = _engine(sm)
    eng.fill_source = None

    class R:
        cycle_closed, fills_applied, realized_pl = True, 1, 0.0
        anomalies, unmatched_fills = [], []

    class Eod:
        def run(self, state, *a):
            state.holdings, state.cash, state.T, state.avg_price = 0, 21234.56, 0.0, 0.0
            state.next_buy_need, state.next_core_need = 600.0, 500.0
            return R()
    eng.eod = Eod()

    async def no_orders(t, ex): return []
    eng.kiwoom.get_today_orders = no_orders

    async def no_rec(*a, **k): return None
    eng._reconcile_ticker = no_rec
    E.daily_report = lambda *a, **k: "리포트"
    asyncio.run(eng._eod_ticker("SOXL", "20261023", date(2026, 10, 23)))
    st = sm.get_state("SOXL")
    assert st.awaiting_principal and st.next_buy_need == 0.0 and st.next_core_need == 0.0
    assert eng.notifier.buttons and "새 사이클 원금" in eng.notifier.buttons[0][0]


def test_morning_brief_shows_waiting_and_excludes_need():
    from core.ops import morning_brief
    sm = _sm()
    st = sm.get_state("SOXL")
    st.next_buy_need = 600.0
    sm.save_state(st)
    txt = morning_brief(sm, OrderRegistry(Path(tempfile.mkdtemp()) / "o.db"), None, 100.0)
    assert "원금 대기 중" in txt
    assert "입금 불필요" in txt


# ================================================================
# 텔레그램 — 버튼·입력·명령
# ================================================================

class _Msg:
    def __init__(self, text=""):
        self.text, self.sent, self.markups = text, [], []

    async def reply_text(self, t, **k):
        self.sent.append(t)
        self.markups.append(k.get("reply_markup"))


class _Q:
    def __init__(self, data):
        self.data = data

    async def answer(self): pass
    async def edit_message_text(self, t, **k): pass


class _U:
    def __init__(self, data=None, text=""):
        self.callback_query = _Q(data) if data else None
        self.effective_message = _Msg(text)
        self.effective_user = type("U", (), {"id": 1})()


class _C:
    def __init__(self, args=None):
        self.args, self.user_data = args or [], {}


def _bot(sm):
    b = KbotTelegramBot.__new__(KbotTelegramBot)
    b.state, b.scheduler, b.kiwoom = sm, None, None
    b.cmd_handler = None
    return b


def test_button_keep():
    sm = _sm()
    b = _bot(sm)
    u = _U("PRIN:KEEP:SOXL")
    asyncio.run(b._handle_callback(u, _C()))
    assert "새 사이클 원금 $20,000" in u.effective_message.sent[0]
    assert not sm.get_state("SOXL").awaiting_principal


def test_button_input_then_text_then_confirm():
    sm = _sm()
    b, ctx = _bot(sm), _C()
    asyncio.run(b._handle_callback(_U("PRIN:INPUT:SOXL"), ctx))
    assert ctx.user_data["principal_edit"] == "SOXL"
    u = _U(text="25,000")
    assert asyncio.run(b._on_principal_text(u, ctx)) is True
    assert "1회 매수금 $625.00" in u.effective_message.sent[0]
    assert u.effective_message.markups[0] is not None
    assert sm.get_state("SOXL").awaiting_principal          # 아직 적용 전
    u2 = _U("PRIN:SET:SOXL:25000.00")
    asyncio.run(b._handle_callback(u2, ctx))
    assert sm.get_state("SOXL").principal == 25000.0


def test_input_cancel_and_bad_number():
    """숫자가 아닌 메시지는 입력을 끝내고 원래 명령으로 넘긴다 (붙잡지 않는다)"""
    sm = _sm()
    b, ctx = _bot(sm), _C()
    ctx.user_data["principal_edit"] = "SOXL"
    u = _U(text="상태")
    assert asyncio.run(b._on_principal_text(u, ctx)) is False
    assert "principal_edit" not in ctx.user_data and "끝냈습니다" in u.effective_message.sent[0]
    ctx.user_data["principal_edit"] = "SOXL"
    u = _U(text="취소")
    assert asyncio.run(b._on_principal_text(u, ctx)) is True
    assert "principal_edit" not in ctx.user_data and sm.get_state("SOXL").awaiting_principal


def test_keep_button_clears_pending_input():
    sm = _sm()
    b, ctx = _bot(sm), _C()
    asyncio.run(b._handle_callback(_U("PRIN:INPUT:SOXL"), ctx))
    asyncio.run(b._handle_callback(_U("PRIN:KEEP:SOXL"), ctx))
    assert "principal_edit" not in ctx.user_data


def test_inf_and_huge_rejected():
    st = _sm().get_state("SOXL")
    assert P.parse_amount("inf") is None and P.parse_amount("nan") is None
    assert "10배" in P.check_amount(st, 1e9)


def test_apply_refused_when_not_awaiting():
    """지난 버튼을 다시 눌러도 첫 매수 주문이 걸린 뒤 잔금이 바뀌지 않는다"""
    sm = _sm()
    P.apply(sm, "SOXL", 20000.0)
    msg = P.apply(sm, "SOXL", 30000.0)
    assert "기다리는 중이 아닙니다" in msg and sm.get_state("SOXL").principal == 20000.0


def test_cycle_closed_with_holdings_does_not_await():
    import scheduler.engine as E
    sm = _sm(cash=19000.0, holdings=100, awaiting=False)
    eng, _ = _engine(sm)
    eng.fill_source = None

    class R:
        cycle_closed, fills_applied, realized_pl = True, 1, 0.0
        anomalies, unmatched_fills = [], []

    class Eod:
        def run(self, state, *a):
            state.holdings = 3           # 같은 날 매수도 체결된 이상 경우
            return R()
    eng.eod = Eod()

    async def no_orders(t, ex): return []
    eng.kiwoom.get_today_orders = no_orders

    async def no_rec(*a, **k): return None
    eng._reconcile_ticker = no_rec
    E.daily_report = lambda *a, **k: "리포트"
    asyncio.run(eng._eod_ticker("SOXL", "20261023", date(2026, 10, 23)))
    assert not sm.get_state("SOXL").awaiting_principal
    assert eng.notifier.buttons == []


def test_rate_failure_does_not_block_orders():
    from core import fx
    from scheduler.engine import SubmitWindow
    sm = _sm()
    P.apply(sm, "SOXL", 20000.0)
    eng, placed = _engine(sm)

    async def tiny(t, ex, price): return 10.0
    eng.kiwoom.available_usd = tiny
    orig = fx.latest_rate

    def boom(*a, **k):
        raise OSError("disk")
    fx.latest_rate = boom
    try:
        asyncio.run(eng._submit_ticker("SOXL", "20261026", SubmitWindow.REGULAR, "LOC 접수"))
    finally:
        fx.latest_rate = orig
    assert any("부족" in s for s in eng.notifier.sent)


def test_text_not_consumed_when_not_waiting():
    assert asyncio.run(_bot(_sm())._on_principal_text(_U(text="상태"), _C())) is False


def test_principal_text_checked_before_routing():
    src = (ROOT / "tg_bot" / "bot.py").read_text(encoding="utf-8")
    assert src.index("_on_principal_text(update, context)") < src.index('"상태" in text')


def test_command_forms():
    sm = _sm()
    b = _bot(sm)
    u = _U()
    asyncio.run(b._cmd_principal(u, _C()))
    assert "새 사이클 원금을 정해 주세요" in u.effective_message.sent[0]
    u = _U()
    asyncio.run(b._cmd_principal(u, _C(["SOXL", "profit"])))
    assert sm.get_state("SOXL").principal == 21234.56


def test_command_mid_cycle_refused():
    sm = _sm(holdings=5, awaiting=False)
    u = _U()
    asyncio.run(_bot(sm)._cmd_principal(u, _C(["SOXL", "30000"])))
    assert "진행 중인 사이클" in u.effective_message.sent[0]


# ================================================================
# 원화 입금 안내
# ================================================================

def test_krw_conversion_with_haircut():
    assert krw_for_usd(171.54, 1314.10) == 237000      # 171.54 × 1314.10 × 1.05 → 천원 올림
    assert krw_text(100, 0) == ""
    assert krw_for_usd(0, 1300) == 0


def test_latest_rate_from_archive():
    sm = _sm()
    sm.archive_eod("SOXL", "20261022", {"usd_krw": 1300.0})
    sm.archive_eod("SOXL", "20261023", {"usd_krw": 1314.1})
    assert latest_rate(sm, date(2026, 10, 24)) == 1314.1
    assert latest_rate(_sm(), date(2026, 10, 24)) == 0.0


def test_topup_text_shows_krw():
    t = FundingCheck(need=900, available=108.41, core_need=280, krw_rate=1314.1).topup_text()
    assert "₩" in t and "4% 낮게" in t
    plain = FundingCheck(need=900, available=108.41, core_need=280).topup_text()
    assert "₩" not in plain
    assert "₩" in account_summary({"SOXL": 900}, 100.0, {"SOXL": 280}, krw_rate=1300.0)


def test_funding_notice_wording():
    from core.funding import allocate_buys
    from scheduler.engine import SchedulerEngine
    from modes.base_mode import PlannedOrder
    o = PlannedOrder(tag="star_buy", side="buy", trade_type=TradeType.LOC, qty=5, price=100.0)
    alloc = allocate_buys([o], 100.0, 0.0007)
    txt = SchedulerEngine._funding_notice("TQQQ", alloc, [o], krw_rate=1314.1)
    assert "주문가능금액이 부족해" in txt and "달러가 부족" not in txt and "₩" in txt


# ================================================================
# 휴장·조기폐장 안내
# ================================================================

def test_holiday_notices():
    assert notice_lines(date(2026, 11, 25)) == ["📅 내일 밤 미국 휴장 (추수감사절) — 주문·정산 없음"]
    th = notice_lines(date(2026, 11, 26))
    assert "오늘 밤 미국 휴장" in th[0] and "내일 밤 조기폐장" in th[1] and "03:00" in th[1]
    assert "다음 평일(9/7 월) 밤 미국 휴장 (노동절)" in notice_lines(date(2026, 9, 4))[0]
    assert notice_lines(date(2026, 10, 21)) == []
    assert notice_lines(date(2026, 10, 24)) == []                # 토요일, 월요일 정상
    assert holiday_name(date(2026, 4, 3)) == "성금요일"


def test_early_close_summer():
    lines = notice_lines(date(2026, 12, 24))
    assert "오늘 밤 조기폐장 (성탄 전야)" in lines[0] and "내일 밤 미국 휴장 (성탄절)" in lines[1]


def test_morning_brief_has_notice():
    from core.ops import morning_brief
    import core.ops as ops
    from datetime import datetime as _dt

    class FakeDT(_dt):
        @classmethod
        def now(cls, tz=None):
            return _dt(2026, 11, 25, 8, 0, tzinfo=tz)
    orig = ops.datetime
    ops.datetime = FakeDT
    try:
        txt = morning_brief(_sm(awaiting=False), OrderRegistry(Path(tempfile.mkdtemp()) / "o.db"))
    finally:
        ops.datetime = orig
    assert "내일 밤 미국 휴장 (추수감사절)" in txt


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
