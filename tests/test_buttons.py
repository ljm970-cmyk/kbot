"""
================================================================
텔레그램 버튼 테스트

원본 코드의 버튼 대부분이 같은 화면을 다시 띄우거나 "처리: ..." 만
출력했다. 게다가 설정 마법사가 버튼 처리기에 패턴 제한 없이 등록돼,
마법사 대화가 끝나지 않은 채 남으면 이후 모든 버튼을 가로챘다.

실행:  python tests/test_buttons.py
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

CFG = {"division": 20, "principal": 20000.0, "fee_rate": 0.0007, "is_active": True}


class _CH:
    def __init__(self):
        self.calls = []

    async def cmd_status(self, u, c): self.calls.append("status")
    async def cmd_orders(self, u, c): self.calls.append(("orders", list(c.args)))
    async def cmd_history(self, u, c): self.calls.append("history")
    async def cmd_config(self, u, c): self.calls.append(("config", list(c.args)))


class _Msg:
    def __init__(self, text=""):
        self.text = text
        self.sent = []

    async def reply_text(self, t, **k):
        self.sent.append(t)


class _Query:
    def __init__(self, data):
        self.data = data

    async def answer(self): pass
    async def edit_message_text(self, t, **k): pass


class _Update:
    def __init__(self, data=None, text=""):
        self.callback_query = _Query(data) if data else None
        self.effective_message = _Msg(text)
        self.effective_user = type("U", (), {"id": 1})()


class _Ctx:
    def __init__(self):
        self.args = []
        self.user_data = {}


def _bot(halted=False):
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    sm.save_config("SOXL", CFG)
    if halted:
        st = sm.get_state("SOXL")
        CircuitBreaker.halt(st, "예수금 부족")
        sm.save_state(st)
    b = KbotTelegramBot.__new__(KbotTelegramBot)
    b.state = sm
    b.cmd_handler = _CH()
    b.scheduler = None
    b.kiwoom = type("K", (), {"can_cancel_now": lambda self: True})()
    return b


def _press(bot, data, ctx=None):
    ctx = ctx or _Ctx()
    u = _Update(data)
    asyncio.run(bot._handle_callback(u, ctx))
    return u.effective_message.sent, ctx


# ================================================================
# 마법사가 다른 버튼을 가로채지 않는지
# ================================================================

def test_wizard_callbacks_have_patterns():
    """패턴 없이 두면 마법사 대화가 남았을 때 모든 버튼을 삼킨다"""
    src = (ROOT / "tg_bot" / "setup_wizard.py").read_text(encoding="utf-8")
    block = src[src.index("states={"):src.index("fallbacks=")]
    n_handlers = block.count("CallbackQueryHandler(")
    n_patterns = block.count("pattern=")
    assert n_handlers == 4
    assert n_patterns == n_handlers, "패턴 없는 마법사 버튼 처리기가 있다"


def test_wizard_patterns_do_not_match_other_buttons():
    import re
    src = (ROOT / "tg_bot" / "setup_wizard.py").read_text(encoding="utf-8")
    pats = re.findall(r'pattern=r"([^"]+)"', src[src.index("states={"):])
    others = ["CONFIG:SEED:SOXL", "SYNC:NOW", "ORDERS:SOXL", "CALC:FORCE",
              "HIST:LIST", "panic:go", "fix:t:SOXL"]
    for o in others:
        for p in pats:
            assert not re.match(p, o), f"마법사 패턴 {p} 가 {o} 를 가로챈다"


def test_wizard_patterns_match_own_buttons():
    import re
    src = (ROOT / "tg_bot" / "setup_wizard.py").read_text(encoding="utf-8")
    pats = re.findall(r'pattern=r"([^"]+)"', src[src.index("states={"):])
    own = ["SINGLE:SOXL", "SINGLE:TQQQ", "BOTH", "20", "40",
           "KEEP_SAME", "NEW_INPUT", "CONFIRM", "RESTART"]
    for o in own:
        assert any(re.match(p, o) for p in pats), f"{o} 를 받는 패턴이 없다"


# ================================================================
# 버튼 라우팅
# ================================================================

def test_sync_button():
    b = _bot()
    _press(b, "SYNC:NOW")
    assert b.cmd_handler.calls == ["status"]


def test_orders_button_with_ticker():
    b = _bot()
    _press(b, "ORDERS:SOXL")
    assert b.cmd_handler.calls == [("orders", ["SOXL"])]


def test_orders_refresh_is_not_treated_as_ticker():
    """원본은 REFRESH 를 종목명으로 넘겨 조회가 깨졌다"""
    b = _bot()
    _press(b, "ORDERS:REFRESH")
    _press(b, "ORDERS:VIEW")
    assert b.cmd_handler.calls == [("orders", []), ("orders", [])]


def test_history_button():
    b = _bot()
    _press(b, "HIST:LIST")
    assert b.cmd_handler.calls == ["history"]


def test_force_calc_button_routes_to_eod():
    sent, _ = _press(_bot(), "CALC:FORCE")
    assert any("EOD" in s for s in sent)


def test_ticker_add_button_guides_to_start():
    sent, _ = _press(_bot(), "TICKER:ADD")
    assert any("/start" in s for s in sent)


def test_unknown_button_is_reported_not_silent():
    sent, _ = _press(_bot(), "SOMETHING:WEIRD")
    assert any("지원하지 않습니다" in s for s in sent)


# ================================================================
# 설정 버튼
# ================================================================

def test_seed_button_prompts_then_applies():
    """원금 버튼 → 입력 요청 → 숫자를 보내면 실제로 적용"""
    b = _bot()
    sent, ctx = _press(b, "CONFIG:SEED:SOXL")
    assert any("원금" in s for s in sent)
    assert ctx.user_data.get("config_edit") == ("SOXL", "principal", "원금")

    handled = asyncio.run(b._on_config_text(_Update(text="3,500"), ctx))
    assert handled is True
    assert b.cmd_handler.calls[-1] == ("config", ["SOXL", "principal", "3500"])
    assert "config_edit" not in ctx.user_data


def test_fee_and_division_buttons():
    b = _bot()
    _, ctx = _press(b, "CONFIG:FEE:SOXL")
    asyncio.run(b._on_config_text(_Update(text="0.07"), ctx))
    _, ctx = _press(b, "CONFIG:DIV:SOXL")
    asyncio.run(b._on_config_text(_Update(text="20"), ctx))
    assert ("config", ["SOXL", "fee", "0.07"]) in b.cmd_handler.calls
    assert ("config", ["SOXL", "division", "20"]) in b.cmd_handler.calls


def test_config_edit_can_be_cancelled():
    b = _bot()
    _, ctx = _press(b, "CONFIG:SEED:SOXL")
    asyncio.run(b._on_config_text(_Update(text="취소"), ctx))
    assert not any(c[0] == "config" for c in b.cmd_handler.calls if isinstance(c, tuple))


def test_config_text_ignored_when_not_waiting():
    b = _bot()
    handled = asyncio.run(b._on_config_text(_Update(text="3500"), _Ctx()))
    assert handled is False


def test_unhalt_button_releases():
    b = _bot(halted=True)
    _press(b, "CONFIG:UNHALT")
    assert CircuitBreaker.is_halted(b.state.get_state("SOXL")) is False


def test_halt_button_halts():
    b = _bot()
    _press(b, "CONFIG:HALT")
    assert CircuitBreaker.is_halted(b.state.get_state("SOXL")) is True


# ================================================================
# 설정 화면 내용
# ================================================================

def test_fee_display_uses_fee_rate():
    """fee_display 가 없으면 기본값 0.25% 가 보였다. 실제 값을 보여야 한다."""
    src = (ROOT / "tg_bot" / "commands_handler.py").read_text(encoding="utf-8")
    block = src[src.index("async def _config_menu"):]
    block = block[:block.index("async def", 20)]
    assert "cfg.get('fee_display', 0.25)" not in block
    assert "fee_rate" in block


def test_fee_display_formatting():
    for rate, want in [(0.0007, "0.07"), (0.0005, "0.05"), (0.0025, "0.25")]:
        got = f"{rate * 100:.3f}".rstrip("0").rstrip(".")
        assert got == want


def test_fake_toggles_removed():
    """샌드박스는 실행 중에 바꾸면 위험하고, 알림 토글은 구현이 없었다"""
    src = (ROOT / "tg_bot" / "commands_handler.py").read_text(encoding="utf-8")
    assert "CONFIG:SANDBOX" not in src
    assert "CONFIG:NOTIFY" not in src
    assert "CONFIG:HALT" in src
    assert "CONFIG:UNHALT" in src


def test_config_text_consumed_before_routing():
    """설정 값 입력이 한글 라우팅으로 새면 안 된다"""
    src = (ROOT / "tg_bot" / "bot.py").read_text(encoding="utf-8")
    assert src.index("_on_config_text(update, context)") < src.index('"상태" in text')


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
