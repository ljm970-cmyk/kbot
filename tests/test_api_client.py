"""
================================================================
API 클라이언트 안전장치 테스트

네트워크가 없으므로 요청 계층을 가짜로 대체해 동작만 검증한다.
가장 중요한 것은 **주문이 재시도되지 않는 것**이다.

실행:  python tests/test_api_client.py
================================================================
"""

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import aiohttp  # noqa: F401
except ImportError:
    _a = types.ModuleType("aiohttp")
    _a.ClientSession = object
    _a.ClientTimeout = lambda **k: None
    _a.ClientError = type("ClientError", (Exception,), {})
    sys.modules["aiohttp"] = _a
    import aiohttp  # noqa: F401

from kiwoom.api_client import (  # noqa: E402
    NON_IDEMPOTENT_APIS,
    KiwoomAPIClient,
    KiwoomOrderUncertainError,
    fmt_price,
    to_float,
    to_int,
)
from kiwoom.constants import BUY_ALLOWED_TRADE_TYPES, TradeType  # noqa: E402


def _client() -> KiwoomAPIClient:
    c = KiwoomAPIClient("key", "secret")
    c.token = "t"
    from datetime import datetime, timedelta
    c.token_expiry = datetime.now() + timedelta(hours=1)
    return c


class _Boom:
    """항상 통신 오류를 내는 가짜 세션"""

    def __init__(self):
        self.calls = 0

    def post(self, *a, **k):
        self.calls += 1
        raise aiohttp.ClientError("연결 끊김")


# ================================================================
# 주문 재시도 금지 — 가장 중요한 안전장치
# ================================================================

def test_order_apis_are_marked_non_idempotent():
    for api in ["ust20000", "ust20001", "ust20003",
                "ust21200", "ust21201", "ust21203"]:
        assert api in NON_IDEMPOTENT_APIS, f"{api} 가 비멱등 목록에 없다"


def test_query_apis_are_idempotent():
    for api in ["ust21070", "ust21050", "usa20100", "usa20590", "ust21205"]:
        assert api not in NON_IDEMPOTENT_APIS


def test_order_is_not_retried_on_network_error():
    """요청이 서버에 도달한 뒤 응답만 유실됐을 수 있다.
    재시도하면 같은 주문이 한 번 더 들어간다."""
    c = _client()
    boom = _Boom()
    c._session = boom
    c._ensure_session = lambda: boom

    try:
        asyncio.run(c.request("ust21200", "/api/us/ordr", {"stk_cd": "TQQQ"}))
    except KiwoomOrderUncertainError as e:
        assert "재시도하지 않습니다" in str(e)
    else:
        raise AssertionError("주문 불명 예외가 나야 한다")

    assert boom.calls == 1, f"주문이 {boom.calls}회 전송됐다 — 중복 주문 위험"


def test_query_is_retried_on_network_error():
    """조회는 몇 번을 다시 해도 안전하므로 재시도한다"""
    c = _client()
    c.max_retries = 2
    boom = _Boom()
    c._session = boom
    c._ensure_session = lambda: boom

    try:
        asyncio.run(c.request("ust21070", "/api/us/acnt", {}))
    except Exception:
        pass
    assert boom.calls == 3      # 최초 1 + 재시도 2


def test_uncertain_error_message_guides_recovery():
    e = KiwoomOrderUncertainError("ust21200", RuntimeError("timeout"))
    assert "ust21205" in str(e) or "확인" in str(e)


def test_scheduler_halts_on_uncertain_order():
    src = (ROOT / "scheduler" / "engine.py").read_text(encoding="utf-8")
    assert "KiwoomOrderUncertainError" in src
    assert src.index("KiwoomOrderUncertainError as e") < src.index("KiwoomAPIError as e")


# ================================================================
# 주문 유형 검증
# ================================================================

def test_moc_buy_rejected_before_sending():
    """키움은 매수에 MOC 를 지원하지 않는다. 전송 전에 막아야 한다."""
    c = _client()
    try:
        asyncio.run(c.buy("TQQQ", "ND", 10, TradeType.MOC))
    except ValueError as e:
        assert "지원되지 않는" in str(e)
    else:
        raise AssertionError("MOC 매수는 거부돼야 한다")


def test_stop_buy_rejected():
    c = _client()
    for tt in [TradeType.STOP_LIMIT, TradeType.STOP_MARKET]:
        try:
            asyncio.run(c.buy("TQQQ", "ND", 1, tt))
        except ValueError:
            pass
        else:
            raise AssertionError(f"{tt} 매수는 거부돼야 한다")


def test_loc_buy_allowed():
    assert TradeType.LOC in BUY_ALLOWED_TRADE_TYPES
    assert TradeType.MOC not in BUY_ALLOWED_TRADE_TYPES


# ================================================================
# 파싱
# ================================================================

def test_price_sign_is_direction_not_negative():
    """키움 시세는 등락 방향을 부호로 준다. 음수 가격이 아니다."""
    assert to_float("-198.4500") == 198.45
    assert to_float("+201.4700") == 201.47


def test_padded_int():
    assert to_int("000000000005") == 5
    assert to_int("") == 0


def test_price_format():
    assert fmt_price(52.8199) == "52.82"
    assert fmt_price(39.365) == "39.37"


def test_expiry_parsed_from_expires_dt():
    """토큰 응답에 expires_in 은 없다. expires_dt(YYYYMMDDHHMMSS) 가 온다."""
    from datetime import datetime
    got = KiwoomAPIClient._parse_expiry("20261107083713")
    assert got < datetime(2026, 11, 7, 8, 37, 13)     # 여유분이 빠진다
    assert got.date() == datetime(2026, 11, 7).date()


def test_expiry_fallback_on_bad_input():
    from datetime import datetime
    got = KiwoomAPIClient._parse_expiry("이상한값")
    assert got > datetime.now()


# ================================================================
# 설정
# ================================================================

def test_mock_switches_base_url():
    c = KiwoomAPIClient("k", "s", mock=True)
    assert "mockapi" in c.base_url


def test_config_constructor_compat():
    from dataclasses import dataclass

    @dataclass
    class Cfg:
        app_key: str = "K"
        app_secret: str = "S"
        api_base: str = "https://api.kiwoom.com"
        mode: str = "real"

    c = KiwoomAPIClient(Cfg())
    assert c.app_key == "K" and c.app_secret == "S"


# ================================================================
# 일 차트 (usa06012)
#
# 일별주가(usa20590)에는 수정주가 옵션이 없다. TQQQ·SOXL 은 액면분할
# 이력이 있어서, 수정주가를 적용하지 않으면 분할이 낀 구간의 과거 종가가
# 현재 주가 체계와 달라진다. 리버스 별지점(직전 5거래일 종가 평균)을
# 오늘 주가와 비교하므로 같은 기준이어야 한다.
# ================================================================

def test_daily_chart_uses_adjusted_prices():
    src = (ROOT / "kiwoom" / "api_client.py").read_text(encoding="utf-8")
    assert "usa06012" in src
    assert "upd_stkpc_tp" in src
    assert '"exrt_appl_tp": "0"' in src


def test_recent_closes_prefers_chart_with_fallback():
    src = (ROOT / "kiwoom" / "api_client.py").read_text(encoding="utf-8")
    block = src[src.index("async def get_recent_closes"):]
    block = block[:block.index("async def", 20)]
    assert "get_daily_chart" in block
    assert "get_daily_prices" in block          # 폴백 유지


def test_chart_path_is_separate():
    from kiwoom.api_client import KiwoomAPIClient
    assert KiwoomAPIClient.PATH_CHART == "/api/us/chart"
    assert KiwoomAPIClient.PATH_MARKET == "/api/us/mrkcond"


def test_chart_price_has_no_sign_prefix():
    """usa06012 의 cur_prc 는 부호가 없고(201.3612),
    usa20590 은 부호가 붙는다(-198.4500). 둘 다 처리돼야 한다."""
    assert to_float("201.3612") == 201.3612
    assert to_float("-198.4500") == 198.45


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
