"""
================================================================
키움 미국주식 API 상수

의존성 없는 순수 상수 모듈. 전략 계산 모듈(modes/, core/)이
HTTP 클라이언트(aiohttp)를 끌어오지 않고도 주문 코드를 참조할 수 있게
api_client 에서 분리했다.
================================================================
"""

from __future__ import annotations


class TradeType:
    """해외매매구분 (trde_tp)

    매수(ust20000 / ust21200)가 지원하는 값은 지정가·시장가·LOC·
    VWAP/TWAP 계열뿐이다. MOC(33)·STOP(34/35)은 매도 전용이다.
    """
    LIMIT = "00"        # 지정가
    MARKET = "03"       # 시장가
    VWAP_LIMIT = "26"
    TWAP_LIMIT = "27"
    LOC = "30"          # Limit On Close
    MOC = "33"          # Market On Close  — 매도 전용
    STOP_LIMIT = "34"   # 매도 전용
    STOP_MARKET = "35"  # 매도 전용
    VWAP_MARKET = "36"
    TWAP_MARKET = "37"

    NAMES = {
        "00": "지정가", "03": "시장가", "26": "VWAP지정가", "27": "TWAP지정가",
        "30": "LOC", "33": "MOC", "34": "STOP LIMIT", "35": "STOP",
        "36": "VWAP시장가", "37": "TWAP시장가",
    }


#: 매수 주문에서 허용되는 trde_tp (문서상 33/34/35 없음)
BUY_ALLOWED_TRADE_TYPES = frozenset({
    TradeType.LIMIT, TradeType.MARKET, TradeType.LOC,
    TradeType.VWAP_LIMIT, TradeType.TWAP_LIMIT,
    TradeType.VWAP_MARKET, TradeType.TWAP_MARKET,
})


class Exchange:
    """거래소구분 (stex_tp)"""
    AMEX = "NA"
    NASDAQ = "ND"
    NYSE = "NY"

    ALL = ("ND", "NY", "NA")


class ReserveType:
    """예약주문구분 (rsrv_ord_tp)"""
    NORMAL = "1"          # 일반예약 (당일 1회)
    PERIOD_REMAIN = "2"   # 기간예약 (잔량주문)
    PERIOD_FIXED = "3"    # 기간예약 (지정수량주문)


class SideFilter:
    """조회용 매도매수구분 (slby_tp)"""
    ALL = "0"
    SELL = "1"
    BUY = "2"


# ================================================================
# 종목 → 거래소 매핑
# ================================================================

#: TQQQ 는 NASDAQ 확정. SOXL 은 NYSE Arca 상장이라 키움이 NY/NA 중
#: 무엇으로 잡는지 문서만으로 알 수 없다. 운영 전에
#: KiwoomAPIClient.resolve_exchange("SOXL") 로 실측해 확정할 것.
EXCHANGE_MAP: dict[str, str] = {
    "TQQQ": Exchange.NASDAQ,
    "SOXL": Exchange.AMEX,   # ← 미확정
}


def exchange_of(ticker: str) -> str:
    try:
        return EXCHANGE_MAP[ticker.upper()]
    except KeyError:
        raise ValueError(
            f"{ticker} 의 거래소구분이 등록되지 않았습니다. "
            f"KiwoomAPIClient.resolve_exchange('{ticker}') 로 확인 후 "
            f"EXCHANGE_MAP 에 추가하세요."
        )
