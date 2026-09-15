"""
================================================================
텔레그램 연동 테스트

tg_bot 은 재작성하지 않고 호환 계층으로 붙였다. 그 접점이
깨지지 않았는지 검증한다.

실행:  python tests/test_telegram.py
================================================================
"""

import asyncio
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# --- 외부 패키지 스텁 (텔레그램·네트워크 없이 검증) ---
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
    for n in ["Application", "ApplicationBuilder", "CommandHandler",
              "CallbackQueryHandler", "MessageHandler"]:
        setattr(tge, n, _Any)
    setattr(tge, "filters", _Any())
    setattr(tge, "TypeHandler", _Any)
    setattr(tge, "ApplicationHandlerStop",
            type("ApplicationHandlerStop", (Exception,), {}))
    setattr(tgc, "ParseMode", type("ParseMode", (), {"HTML": "HTML", "MARKDOWN": "MD"}))

try:
    import aiohttp  # noqa: F401
except ImportError:
    a = _stub("aiohttp")
    a.ClientSession = object
    a.ClientTimeout = lambda **k: None
    a.ClientError = Exception

try:
    import apscheduler  # noqa: F401
except ImportError:
    for p in ["apscheduler", "apscheduler.schedulers", "apscheduler.triggers"]:
        _stub(p)
    _stub("apscheduler.schedulers.asyncio", AsyncIOScheduler=object)
    _stub("apscheduler.triggers.cron", CronTrigger=object)
    _stub("apscheduler.triggers.date", DateTrigger=object)

try:
    import websockets  # noqa: F401
except ImportError:
    _stub("websockets", connect=None)

from core.state_manager import StateManager  # noqa: E402
from tg_bot.commands_handler import CommandsHandler  # noqa: E402

CFG = {"division": 40, "principal": 20000.0, "fee_rate": 0.0007, "is_active": True}
USER = "8985024710"


def _mgr() -> StateManager:
    m = StateManager(Path(tempfile.mkdtemp()) / "data")
    m.save_ticker_config(USER, "TQQQ", CFG)
    return m


def _handler(state):
    h = CommandsHandler.__new__(CommandsHandler)
    h.state = state
    return h


# ================================================================
# _retry_api — 동기/비동기 혼용
# ================================================================

def test_retry_api_handles_sync_function():
    """state_manager 메서드는 동기다. 무조건 await 하면
    TypeError 로 3회 실패한 뒤 조용히 None 을 돌려준다."""
    m = _mgr()
    h = _handler(m)
    st = asyncio.run(h._retry_api(m.get_state, USER, "TQQQ", default=None))
    assert st is not None, "/status 가 '상태 없음' 으로 표시된다"
    assert st.division == 40


def test_retry_api_handles_async_function():
    async def fetch(x):
        return x * 2
    h = _handler(_mgr())
    assert asyncio.run(h._retry_api(fetch, 21)) == 42


def test_retry_api_returns_default_on_failure():
    async def boom():
        raise ConnectionError("네트워크")
    h = _handler(_mgr())
    assert asyncio.run(h._retry_api(boom, default="기본값", retries=2)) == "기본값"


def test_retry_api_does_not_retry_programming_errors():
    """TypeError 를 3회 재시도해도 결과는 같다. 즉시 중단해야
    사용자가 오래 기다리지 않는다."""
    calls = []

    def bad():
        calls.append(1)
        raise TypeError("잘못된 호출")

    h = _handler(_mgr())
    assert asyncio.run(h._retry_api(bad, default=None, retries=3)) is None
    assert len(calls) == 1


# ================================================================
# 상태 접근 (commands_handler 가 쓰는 형태)
# ================================================================

def test_status_reads_state_as_dict():
    """cmd_status 는 st['mode'], st['T'], st['division'] 로 읽는다"""
    m = _mgr()
    st = m.get_state(USER, "TQQQ")
    st.T = 8.5
    m.save_state(st)

    st = m.get_state(USER, "TQQQ")
    assert st["mode"] == "normal"
    assert st["T"] == 8.5
    assert st["division"] == 40


def test_legacy_state_methods_exist():
    """tg_bot 이 호출하는 6개 메서드가 모두 살아 있어야 한다"""
    m = _mgr()
    for name in ["get_state", "save_ticker_config", "get_ticker_config",
                 "get_user_tickers", "get_principal", "deactivate_ticker",
                 "add_manual_correction"]:
        assert callable(getattr(m, name, None)), f"{name} 없음"


def test_setup_wizard_auto_creates_state():
    """setup_wizard 가 get_state 로 초기 상태를 자동 생성한다"""
    m = StateManager(Path(tempfile.mkdtemp()) / "data")
    m.save_ticker_config(USER, "SOXL", {**CFG, "division": 20})
    st = m.get_state(USER, "SOXL")
    assert st is not None
    assert st.division == 20
    assert st.cash == 20000.0
    assert st.T == 0.0


def test_user_id_does_not_split_state():
    """계좌가 하나이므로 user_id 가 달라도 같은 상태를 봐야 한다"""
    m = _mgr()
    st = m.get_state("111", "TQQQ")
    st.T = 3.0
    m.save_state(st)
    assert m.get_state("222", "TQQQ").T == 3.0


# ================================================================
# API 호환 별칭
# ================================================================

def test_kiwoom_legacy_aliases_exist():
    """commands_handler 가 쓰는 구버전 메서드명이 살아 있어야 한다"""
    from kiwoom.api_client import KiwoomAPIClient
    for name in ["get_current_price", "get_reserv_orders", "cancel_reserv_order"]:
        assert callable(getattr(KiwoomAPIClient, name, None)), f"{name} 없음"


def test_get_reserv_orders_signature():
    """_retry_api(self.kiwoom.get_reserv_orders, fr_rsrv_dt=..., to_rsrv_dt=...)"""
    import inspect
    from kiwoom.api_client import KiwoomAPIClient
    params = inspect.signature(KiwoomAPIClient.get_reserv_orders).parameters
    assert "fr_rsrv_dt" in params
    assert "to_rsrv_dt" in params


def test_timezone_handler_interface():
    """commands_handler 가 self.tz 로 부르는 메서드"""
    from core.timezone_handler import USMarketTimezone
    tz = USMarketTimezone()
    assert isinstance(tz.is_summer_time(), bool)
    assert len(tz.get_dst_info()) == 2
    assert tz.get_next_order_time() is not None
    assert tz.get_next_eod_time() is not None


# ================================================================
# 관리자 인증
#
# TELEGRAM_ADMIN_ID 는 원래 알림 전송에만 쓰였고 명령어 접근은
# 누구에게나 열려 있었다. /fix 로 가짜 체결을 주입하고 /start 로
# 원금을 바꿀 수 있었으므로 전역 가드를 넣었다.
# ================================================================

def test_admin_guard_is_registered_first():
    """가드는 group=-1 로 등록되어 다른 모든 핸들러보다 먼저 돌아야 한다"""
    src = (ROOT / "tg_bot" / "bot.py").read_text(encoding="utf-8")
    assert "TypeHandler(Update, self._require_admin), group=-1" in src
    # 핵심 명령 등록보다 앞서야 한다
    assert src.index("_require_admin), group=-1") < src.index('CommandHandler("status"')


def test_admin_check_uses_config():
    from config.settings import TelegramConfig
    t = TelegramConfig(bot_token="x", admin_id="111,222")
    assert t.is_admin(111) is True
    assert t.is_admin("222") is True
    assert t.is_admin(333) is False


def test_non_admin_is_stopped():
    """관리자가 아니면 ApplicationHandlerStop 이 올라가 처리가 중단된다"""
    from telegram.ext import ApplicationHandlerStop
    from config.settings import TelegramConfig
    from tg_bot.bot import KbotTelegramBot

    bot = KbotTelegramBot.__new__(KbotTelegramBot)
    bot.config = type("C", (), {"telegram": TelegramConfig(bot_token="x", admin_id="111")})()

    sent = []

    class _Msg:
        async def reply_text(self, text, **k): sent.append(text)

    class _Update:
        effective_user = type("U", (), {"id": 999, "username": "침입자"})()
        effective_message = _Msg()

    try:
        asyncio.run(bot._require_admin(_Update(), None))
    except ApplicationHandlerStop:
        pass
    else:
        raise AssertionError("비관리자는 차단돼야 한다")
    assert sent and "관리자 전용" in sent[0]


def test_admin_passes_through():
    from config.settings import TelegramConfig
    from tg_bot.bot import KbotTelegramBot

    bot = KbotTelegramBot.__new__(KbotTelegramBot)
    bot.config = type("C", (), {"telegram": TelegramConfig(bot_token="x", admin_id="111")})()

    class _Update:
        effective_user = type("U", (), {"id": 111, "username": "주인"})()
        effective_message = None

    asyncio.run(bot._require_admin(_Update(), None))    # 예외 없이 통과


# ================================================================
# 스케줄러 제어 명령
# ================================================================

def test_scheduler_commands_registered():
    src = (ROOT / "tg_bot" / "bot.py").read_text(encoding="utf-8")
    for cmd in ["next", "pause", "resume", "run", "help"]:
        assert f'CommandHandler("{cmd}"' in src, f"/{cmd} 미등록"


def test_help_lists_new_commands():
    src = (ROOT / "tg_bot" / "bot.py").read_text(encoding="utf-8")
    for cmd in ["/next", "/pause", "/resume", "/run"]:
        assert cmd in src, f"도움말에 {cmd} 없음"


def test_run_without_args_shows_usage():
    from tg_bot.bot import KbotTelegramBot
    bot = KbotTelegramBot.__new__(KbotTelegramBot)
    bot.scheduler = object()
    sent = []

    class _Msg:
        async def reply_text(self, text, **k): sent.append(text)

    class _Update:
        effective_message = _Msg()

    ctx = type("C", (), {"args": []})()
    asyncio.run(bot._cmd_run(_Update(), ctx))
    assert sent and "사용법" in sent[0]


def test_commands_without_scheduler_do_not_crash():
    """스케줄러 미연결 상태에서도 안내만 하고 넘어가야 한다"""
    from tg_bot.bot import KbotTelegramBot
    bot = KbotTelegramBot.__new__(KbotTelegramBot)
    bot.scheduler = None
    sent = []

    class _Msg:
        async def reply_text(self, text, **k): sent.append(text)

    class _Update:
        effective_message = _Msg()

    ctx = type("C", (), {"args": []})()
    for fn in [bot._cmd_next, bot._cmd_pause, bot._cmd_resume, bot._cmd_run]:
        asyncio.run(fn(_Update(), ctx))
    assert len(sent) == 4
    assert all("연결되지 않았습니다" in s for s in sent)


# ================================================================
# 수동 보정 (/fix)
#
# 구버전은 기록만 남기고 장부에 반영하지 않았고, 반환값을 확인하지 않아
# 사용자에게 아무 응답도 가지 않았다. 정지가 걸렸을 때 쓸 유일한
# 복구 수단이므로 실제로 동작해야 한다.
# ================================================================

def test_manual_fix_actually_updates_ledger():
    m = _mgr()
    st = m.get_state(USER, "TQQQ")
    st.holdings, st.avg_price, st.cash = 57, 70.0, 5000.0
    m.save_state(st)

    r = m.add_manual_correction(USER, "TQQQ",
                                {"qty": 3, "price": 68.10, "side": "buy", "date": "20260915"})
    assert r["ok"] is True

    after = m.get_state(USER, "TQQQ")
    assert after.holdings == 60
    assert abs(after.avg_price - (70.0 * 57 + 68.10 * 3) / 60) < 1e-9
    assert after.cash < 5000.0            # 매수대금 + 수수료 차감


def test_manual_fix_sell_updates_cash():
    m = _mgr()
    st = m.get_state(USER, "TQQQ")
    st.holdings, st.avg_price, st.cash = 60, 70.0, 1000.0
    m.save_state(st)

    r = m.add_manual_correction(USER, "TQQQ", {"qty": 15, "price": 80.0, "side": "sell"})
    assert r["ok"] is True
    after = m.get_state(USER, "TQQQ")
    assert after.holdings == 45
    assert after.avg_price == 70.0        # 매도는 평단을 바꾸지 않는다
    assert after.cash > 1000.0


def test_manual_fix_rejects_oversell():
    m = _mgr()
    st = m.get_state(USER, "TQQQ")
    st.holdings = 10
    m.save_state(st)
    r = m.add_manual_correction(USER, "TQQQ", {"qty": 50, "price": 70.0, "side": "sell"})
    assert r["ok"] is False
    assert "보유" in r["error"]
    assert m.get_state(USER, "TQQQ").holdings == 10


def test_manual_fix_rejects_bad_input():
    m = _mgr()
    for bad in [{"qty": 0, "price": 70.0, "side": "buy"},
                {"qty": 5, "price": 0, "side": "buy"},
                {"qty": 5, "price": 70.0, "side": "hold"}]:
        assert m.add_manual_correction(USER, "TQQQ", bad)["ok"] is False


def test_manual_fix_records_history():
    """비파괴 — 무엇을 어떻게 고쳤는지 남는다"""
    m = _mgr()
    m.add_manual_correction(USER, "TQQQ", {"qty": 3, "price": 68.10, "side": "buy"})
    rows = m.corrections("TQQQ")
    assert len(rows) == 1
    assert rows[0]["source"] == "manual_fix"
    assert "before" in rows[0] and "after" in rows[0]


def test_sell_all_closes_cycle():
    m = _mgr()
    st = m.get_state(USER, "TQQQ")
    st.holdings, st.avg_price, st.T = 10, 70.0, 8.0
    m.save_state(st)
    m.add_manual_correction(USER, "TQQQ", {"qty": 10, "price": 80.0, "side": "sell"})
    after = m.get_state(USER, "TQQQ")
    assert after.holdings == 0
    assert after.avg_price == 0.0
    assert after.T == 0.0


# ================================================================
# 설정 반영 (/start 마법사)
# ================================================================

def test_config_change_applies_when_flat():
    """보유가 없으면 분할수·원금 변경이 실행 상태에 반영된다"""
    m = _mgr()
    m.get_state(USER, "TQQQ")                       # 40분할 상태 생성
    m.save_ticker_config(USER, "TQQQ", {**CFG, "division": 20, "principal": 30000.0})

    res = m.apply_config("TQQQ")
    assert res["ok"] is True
    st = m.get_state(USER, "TQQQ")
    assert st.division == 20
    assert st.principal == 30000.0
    assert st.cash == 30000.0


def test_division_change_blocked_mid_cycle():
    """20분할 T=8 과 40분할 T=8 은 전혀 다른 상태다"""
    m = _mgr()
    st = m.get_state(USER, "TQQQ")
    st.holdings, st.T = 60, 8.0
    m.save_state(st)

    m.save_ticker_config(USER, "TQQQ", {**CFG, "division": 20})
    res = m.apply_config("TQQQ")
    assert any("분할수" in b for b in res["blocked"])
    assert m.get_state(USER, "TQQQ").division == 40     # 바뀌지 않았다


def test_principal_change_keeps_cash_mid_cycle():
    m = _mgr()
    st = m.get_state(USER, "TQQQ")
    st.holdings, st.cash = 60, 7000.0
    m.save_state(st)

    m.save_ticker_config(USER, "TQQQ", {**CFG, "principal": 30000.0})
    res = m.apply_config("TQQQ")
    st2 = m.get_state(USER, "TQQQ")
    assert st2.principal == 30000.0
    assert st2.cash == 7000.0                          # 진행 중 잔금은 유지
    assert any("잔금은 유지" in b for b in res["blocked"])


def test_config_command_applies_to_state():
    """/config 도 설정 파일만 쓰고 끝내면 안 된다"""
    src = (ROOT / "tg_bot" / "commands_handler.py").read_text(encoding="utf-8")
    assert "apply_config" in src
    assert src.index("save_ticker_config(user_id, ticker, cfg)") < src.index(
        "self.state.apply_config(ticker)")


def test_config_no_longer_reads_t_from_config():
    """T 는 상태 파일에 있다. 설정에서 읽으면 항상 0 이다.

    주석에 남은 설명 문구는 제외하고 실제 코드만 본다.
    """
    import ast
    src = (ROOT / "tg_bot" / "commands_handler.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    bad = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "cfg"
        and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == "T"
    ]
    assert bad == [], "cfg 에서 T 를 읽는 코드가 남아 있다"


def test_wizard_calls_apply_config():
    src = (ROOT / "tg_bot" / "setup_wizard.py").read_text(encoding="utf-8")
    assert "apply_config" in src


def test_force_calc_no_longer_lies():
    """구버전은 TODO 상태로 '계산 완료' 라고 응답했다"""
    src = (ROOT / "tg_bot" / "commands_handler.py").read_text(encoding="utf-8")
    block = src[src.index("async def cmd_force_calc"):]
    block = block[:block.index("async def", 20)] if "async def" in block[20:] else block
    assert "TODO" not in block
    assert "EOD 계산 완료" not in block


def test_calceod_routed_to_scheduler():
    src = (ROOT / "tg_bot" / "bot.py").read_text(encoding="utf-8")
    assert 'CommandHandler("calceod", self._cmd_force_eod)' in src


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
