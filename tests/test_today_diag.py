"""
/today 진단 명령 — 앱에서 사유가 안 보일 때 증권사 당일 주문 원본을 본다

실행:  python tests/test_today_diag.py
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

from core.order_registry import OrderRegistry  # noqa: E402
from kiwoom.constants import TradeType  # noqa: E402
from modes.base_mode import PlannedOrder  # noqa: E402


class _Msg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, t, **k):
        self.sent.append(t)


class _U:
    def __init__(self):
        self.effective_message = _Msg()
        self.effective_user = type("U", (), {"id": 1})()
        self.callback_query = None


class _C:
    def __init__(self, args):
        self.args, self.user_data = args, {}


def _bot(rows, reg=None):
    b = KbotTelegramBot.__new__(KbotTelegramBot)
    b.state = StateManager(Path(tempfile.mkdtemp()) / "d")

    class K:
        async def get_today_orders(self, t, ex):
            return rows
    b.kiwoom = K()
    b.scheduler = type("S", (), {"registry": reg})()
    return b


ROWS = [
    {"ord_no": "27", "ord_uv": 72.55, "raw": {"ord_no": "0000027", "ord_uv": "72.5500",
                                               "ord_stat": "거부", "rjct_rsn": "주문가능금액 부족",
                                               "acnt_no": "12345678"}},
    {"ord_no": "28", "ord_uv": 67.03, "raw": {"ord_no": "0000028", "ord_uv": "67.0300", "ord_stat": "접수"}},
]


def test_filters_by_price_and_hides_account():
    u = _U()
    asyncio.run(_bot(ROWS)._cmd_today(u, _C(["tqqq", "72.55"])))
    t = "\n".join(u.effective_message.sent)
    assert "2건 중 1건" in t and "rjct_rsn = 주문가능금액 부족" in t
    assert "67.0300" not in t and "acnt_no" not in t


def test_all_rows_without_price():
    u = _U()
    asyncio.run(_bot(ROWS)._cmd_today(u, _C(["TQQQ"])))
    assert "2건 중 2건" in u.effective_message.sent[0]


def test_shows_bot_ledger():
    from core.market_calendar import session_date_for
    reg = OrderRegistry(Path(tempfile.mkdtemp()) / "o.db")
    day = session_date_for().strftime("%Y%m%d")
    rid = reg.record_submission(day, "TQQQ", PlannedOrder(
        tag="star_buy", side="buy", trade_type=TradeType.LOC, qty=3, price=72.55))
    reg.attach_ord_no(rid, "27")
    u = _U()
    asyncio.run(_bot(ROWS, reg)._cmd_today(u, _C(["TQQQ", "72.55"])))
    t = "\n".join(u.effective_message.sent)
    assert "봇 원장" in t and "[star_buy]" in t and "ord_no=27" in t


def test_usage_and_bad_price():
    u = _U()
    asyncio.run(_bot(ROWS)._cmd_today(u, _C([])))
    assert "형식" in u.effective_message.sent[0]
    u = _U()
    asyncio.run(_bot(ROWS)._cmd_today(u, _C(["TQQQ", "abc"])))
    assert "숫자" in u.effective_message.sent[0]


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
