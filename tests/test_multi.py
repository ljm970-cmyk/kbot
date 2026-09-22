"""
================================================================
두 종목 동시 운용 테스트 (TQQQ + SOXL)

  - 이미 한 종목이 있어도 다른 종목을 추가할 수 있다
  - 추가해도 기존 종목 설정은 바뀌지 않는다
  - 두 종목이 같은 달러를 쓰므로 입금 안내는 합산한다
  - 달러가 한 종목분만 있으면 먼저 접수하는 종목만 산다

실행:  python tests/test_multi.py
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
    def __init__(self, *a, **k):
        self.args, self.kwargs = a, k
    def __getattr__(self, n): return _Any()
    def __call__(self, *a, **k): return _Any()


class _Btn:
    def __init__(self, text, callback_data=None):
        self.text, self.callback_data = text, callback_data


class _Markup:
    def __init__(self, rows):
        self.inline_keyboard = rows


try:
    import telegram  # noqa: F401
except ImportError:
    tg = _stub("telegram")
    tge = _stub("telegram.ext")
    tgc = _stub("telegram.constants")
    tg.InlineKeyboardButton, tg.InlineKeyboardMarkup = _Btn, _Markup
    for n in ["Update", "ReplyKeyboardMarkup", "Bot", "BotCommand"]:
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

from core.funding import account_summary  # noqa: E402
from core.order_registry import OrderRegistry  # noqa: E402
from core.state_manager import StateManager  # noqa: E402

SOXL = {"ticker": "SOXL", "division": 40, "principal": 20000.0,
        "fee_rate": 0.0007, "is_active": True}
TQQQ = {"ticker": "TQQQ", "division": 20, "principal": 10000.0,
        "fee_rate": 0.0007, "is_active": True}


def _rows(markup):
    return getattr(markup, "inline_keyboard", None) or getattr(markup, "rows", [])


# ================================================================
# 종목 추가
# ================================================================

class _Msg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **k):
        self.sent.append((text, k.get("reply_markup")))


class _Query:
    def __init__(self, data):
        self.data = data
        self.edited = []

    async def answer(self): pass

    async def edit_message_text(self, text, **k):
        self.edited.append(text)


class _Update:
    def __init__(self, data=None):
        self.effective_message = _Msg()
        self.message = self.effective_message
        self.callback_query = _Query(data) if data else None
        self.effective_user = type("U", (), {"id": 1})()


class _Ctx:
    def __init__(self):
        self.user_data = {}


def _wizard(configs):
    from tg_bot.setup_wizard import SetupWizard
    sm = StateManager(Path(tempfile.mkdtemp()) / "d")
    for c in configs:
        sm.save_config(c["ticker"], c)
    return SetupWizard(sm), sm


def test_start_offers_missing_ticker_when_one_exists():
    """원본은 종목이 하나라도 있으면 거절해서 두 번째 종목을 넣을 길이 없었다"""
    from tg_bot.setup_wizard import SELECT_TICKER_MODE
    wiz, _ = _wizard([SOXL])
    u, ctx = _Update(), _Ctx()
    state = asyncio.run(wiz.cmd_start(u, ctx))
    assert state == SELECT_TICKER_MODE
    text, markup = u.effective_message.sent[0]
    assert "운용 중" in text and "SOXL" in text
    labels = [b.text for row in _rows(markup) for b in row]
    assert any("TQQQ 추가" in t for t in labels)
    assert not any("SOXL 추가" in t for t in labels)
    assert ctx.user_data["adding"] is True


def test_start_ends_when_both_configured():
    from tg_bot.setup_wizard import ConversationHandler
    wiz, _ = _wizard([SOXL, TQQQ])
    u = _Update()
    assert asyncio.run(wiz.cmd_start(u, _Ctx())) == ConversationHandler.END
    assert "모두 운용 중" in u.effective_message.sent[0][0]


def test_add_button_entry_works_from_callback():
    """관제탑 '종목 추가' 버튼으로 들어와도 된다 (콜백엔 update.message 가 없다)"""
    from tg_bot.setup_wizard import SELECT_TICKER_MODE
    wiz, _ = _wizard([SOXL])
    u = _Update(data="TICKER:ADD")
    u.message = None
    assert asyncio.run(wiz.cmd_start(u, _Ctx())) == SELECT_TICKER_MODE


def test_add_cancel():
    from tg_bot.setup_wizard import ConversationHandler
    wiz, _ = _wizard([SOXL])
    u = _Update(data="ADD_CANCEL")
    assert asyncio.run(wiz.on_ticker_mode(u, _Ctx())) == ConversationHandler.END


def test_adding_keeps_existing_ticker_untouched():
    """추가는 이번에 입력한 종목만 저장한다. 기존 SOXL 은 그대로다."""
    wiz, sm = _wizard([SOXL])
    before = sm.get_config("SOXL")

    u = _Update(data="CONFIRM")
    ctx = _Ctx()
    ctx.user_data.update({
        "user_id": "1", "adding": True,
        "tickers_config": [{"ticker": "TQQQ", "division": 20, "principal": 10000.0,
                            "fee_rate": 0.0007, "fee_display": 0.07}],
    })
    asyncio.run(wiz.on_confirm(u, ctx))

    assert set(sm.list_tickers()) == {"SOXL", "TQQQ"}
    assert sm.get_config("SOXL")["principal"] == before["principal"]
    assert sm.get_config("SOXL")["division"] == before["division"]
    assert sm.get_config("TQQQ")["run_mode"] == "both"
    st = sm.get_state("TQQQ")
    assert st.division == 20 and st.principal == 10000.0


def test_readding_previously_deactivated_ticker():
    """비활성화했던 종목을 다시 추가하면 활성화된다"""
    wiz, sm = _wizard([SOXL, {**TQQQ, "is_active": False}])
    assert sm.list_tickers() == ["SOXL"]
    ctx = _Ctx()
    ctx.user_data.update({
        "user_id": "1", "adding": True,
        "tickers_config": [{"ticker": "TQQQ", "division": 20, "principal": 10000.0,
                            "fee_rate": 0.0007, "fee_display": 0.07}],
    })
    asyncio.run(wiz.on_confirm(_Update(data="CONFIRM"), ctx))
    assert set(sm.list_tickers()) == {"SOXL", "TQQQ"}


def test_wizard_accepts_add_cancel_pattern():
    import re
    src = (ROOT / "tg_bot" / "setup_wizard.py").read_text(encoding="utf-8")
    pat = re.search(r'on_ticker_mode, pattern=r"([^"]+)"', src).group(1)
    assert re.match(pat, "ADD_CANCEL")
    assert re.match(pat, "SINGLE:TQQQ")


def test_add_button_is_wizard_entry():
    src = (ROOT / "tg_bot" / "setup_wizard.py").read_text(encoding="utf-8")
    entry = src[src.index("entry_points="):src.index("states={")]
    assert 'pattern=r"^TICKER:ADD$"' in entry


# ================================================================
# 자금 — 합산
# ================================================================

def test_combined_shortfall_even_if_each_alone_fits():
    """종목마다 따로 보면 각자 충분해 보여도 합치면 모자랄 수 있다"""
    text = account_summary({"SOXL": 936.0, "TQQQ": 560.0}, 1200.0)
    assert "합계  $1,496.00" in text
    assert "296.00 입금" in text


def test_combined_enough():
    text = account_summary({"SOXL": 936.0, "TQQQ": 560.0}, 4854.93)
    assert "충분" in text


def test_single_ticker_summary_has_no_total_line():
    text = account_summary({"SOXL": 936.0}, 4854.93)
    assert "합계" not in text


def test_morning_brief_combined_for_two_tickers():
    from core.ops import morning_brief
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    for c, need in [(SOXL, 936.0), (TQQQ, 560.0)]:
        sm.save_config(c["ticker"], c)
        st = sm.get_state(c["ticker"])
        st.next_buy_need = need
        sm.save_state(st)
    text = morning_brief(sm, OrderRegistry(d / "o.db"), None, 1200.0)
    assert "[SOXL]" in text and "[TQQQ]" in text
    assert "합계  $1,496.00" in text
    assert "296.00 입금" in text


def test_halted_ticker_excluded_from_need():
    from core.ops import morning_brief
    from core.reconciler import CircuitBreaker
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    for c, need in [(SOXL, 936.0), (TQQQ, 560.0)]:
        sm.save_config(c["ticker"], c)
        st = sm.get_state(c["ticker"])
        st.next_buy_need = need
        if c is TQQQ:
            CircuitBreaker.halt(st, "테스트")
        sm.save_state(st)
    text = morning_brief(sm, OrderRegistry(d / "o.db"), None, 1000.0)
    assert "합계" not in text          # 정지 종목은 빠져 SOXL 하나만
    assert "충분" in text


# ================================================================
# 접수 — 한 종목분 달러만 있을 때
# ================================================================

def test_first_ticker_funded_second_skipped_when_cash_runs_out():
    """실시간 주문은 접수 즉시 주문가능금액을 묶는다. 앞 종목이 쓰고 남은
    금액으로 다음 종목을 판단해야 한다."""
    from scheduler.engine import SchedulerEngine, SubmitWindow

    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    sm.save_config("SOXL", SOXL)
    sm.save_config("TQQQ", TQQQ)
    reg = OrderRegistry(d / "o.db")
    sent, placed = [], []
    wallet = {"cash": 1200.0}

    class K:
        async def available_usd(self, t, ex, price):
            return wallet["cash"]

        async def get_quote(self, t, ex):
            return {"cur_price": 100.0, "prev_close": 100.0}

        async def get_recent_closes(self, t, ex, n=5):
            return [100.0] * 5

    class N:
        async def send(self, text): sent.append(text)

    class Cfg:
        dry_run = False

    class R:
        def __init__(self, n): self.ord_no = n

    async def _place(t, ex, order):
        placed.append((t, order))
        if order.side == "buy":
            wallet["cash"] -= order.amount
        return R(f"{t}{len(placed)}")

    eng = SchedulerEngine.__new__(SchedulerEngine)
    eng.state_mgr, eng.registry, eng.kiwoom = sm, reg, K()
    eng.notifier, eng.config = N(), Cfg()
    eng._place = _place

    for t in ["SOXL", "TQQQ"]:
        asyncio.run(eng._submit_ticker(t, "20260923", SubmitWindow.REGULAR, "LOC 접수"))

    bought = {t for t, o in placed if o.side == "buy"}
    assert "SOXL" in bought
    assert "TQQQ" not in bought
    assert any("TQQQ" in s and "건너뜁니다" in s for s in sent)


def test_scheduler_iterates_all_active_tickers():
    src = (ROOT / "scheduler" / "engine.py").read_text(encoding="utf-8")
    assert src.count("for ticker in self.state_mgr.list_tickers()") >= 3


def test_account_level_available_used():
    src = (ROOT / "scheduler" / "engine.py").read_text(encoding="utf-8")
    assert "async def _account_available(self)" in src
    assert "_available_by_ticker" not in src


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
