"""
입력 검증
"""


def validate_date(date_str: str) -> bool:
    """YYYYMMDD 검증"""
    try:
        from datetime import datetime
        datetime.strptime(date_str, "%Y%m%m%d")
        return True
    except ValueError:
        return False


def validate_quantity(qty: str) -> int:
    """수량 검증"""
    try:
        q = int(qty)
        return q if q > 0 else 0
    except (ValueError, TypeError):
        return 0


def validate_price(price: str) -> float:
    """가격 검증"""
    try:
        p = float(price)
        return p if p > 0 else 0.0
    except (ValueError, TypeError):
        return 0.0
