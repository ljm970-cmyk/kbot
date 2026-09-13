"""
공통 헬퍼 함수
"""


def round_down(value: float, decimals: int = 2) -> float:
    """내림 (소수점 거래 단위)"""
    from math import floor
    factor = 10 ** decimals
    return floor(value * factor) / factor


def round_price_usd(price: float) -> float:
    """미국주식 가격 반올림 (0.01 단위)"""
    return round(price, 2)


def format_currency(value: float, symbol: str = "$") -> str:
    """화폐 포맷"""
    return f"{symbol}{value:,.2f}"
