"""
================================================================
USMarketTimezone — 구버전 호환 래퍼

시각 계산의 단일 출처는 core/market_calendar.py 다.
이 모듈은 tg_bot 이 쓰던 인터페이스를 유지하되, 내부적으로는
market_calendar 에 위임한다.

기존 구현의 문제
  - pytz 의 'US/Eastern' 을 쓰면서도 휴장일 개념이 없어
    휴일·주말에도 다음 주문 시각을 돌려줬다
  - 서머타임 경계를 직접 계산해 market_calendar 와 답이 갈릴 수 있었다

신규 코드에서는 market_calendar 를 직접 쓴다.
================================================================
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from core.market_calendar import (
    ET,
    KST,
    DaySchedule,
    is_dst,
    is_trading_day,
    market_close_kst,
    premarket_open_kst,
    upcoming_session,
)


class USMarketTimezone:
    """미국 증시 시각 유틸 (구버전 호환)"""

    KST = KST
    EASTERN = ET

    @classmethod
    def is_summer_time(cls, kst_datetime: Optional[datetime] = None) -> bool:
        """미국 서머타임 여부"""
        return is_dst(kst_datetime)

    @classmethod
    def get_dst_info(cls) -> tuple:
        """(서머타임 여부, 설명 문자열)

        방법론 6번: 지정가매도는 프리장 시작인
        저녁 5시(서머타임) 또는 6시(비서머타임)에 건다.
        """
        summer = is_dst()
        label = "서머타임 (프리장 17:00 KST)" if summer else "비서머타임 (프리장 18:00 KST)"
        return summer, label

    @classmethod
    def get_next_order_time(cls) -> datetime:
        """다음 주문 접수 시각 (KST).

        주말·휴장일을 건너뛴 실제 거래일 기준이다.
        """
        return premarket_open_kst(upcoming_session())

    @classmethod
    def get_next_eod_time(cls) -> datetime:
        """다음 EOD 정산 시각 (KST). 조기폐장일은 3시간 앞당겨진다."""
        return DaySchedule.build(upcoming_session()).eod

    @classmethod
    def get_next_close_time(cls) -> datetime:
        """다음 장 마감 시각 (KST)"""
        return market_close_kst(upcoming_session())

    @classmethod
    def is_trading_day(cls, d=None) -> bool:
        d = d or upcoming_session()
        return is_trading_day(d)

    @classmethod
    def next_session(cls):
        """다음 거래일 (date)"""
        return upcoming_session()

    @classmethod
    def summary(cls) -> str:
        """텔레그램 표시용"""
        return DaySchedule.build(upcoming_session()).describe()
