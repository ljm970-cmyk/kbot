"""서머타임/비서머타임 + 프리장 30분 지연"""

from datetime import datetime, timedelta, date, time as dt_time
from zoneinfo import ZoneInfo
from typing import Optional, NamedTuple
import logging

logger = logging.getLogger(__name__)


class MarketTimes(NamedTuple):
    premarket_start: dt_time
    premarket_order: dt_time
    premarket_end: dt_time
    regular_start: dt_time
    regular_end: dt_time
    aftermarket_end: dt_time


class USMarketSession:
    KST = ZoneInfo("Asia/Seoul")
    
    SUMMER = MarketTimes(
        premarket_start=dt_time(17, 0),
        premarket_order=dt_time(17, 30),
        premarket_end=dt_time(22, 30),
        regular_start=dt_time(22, 30),
        regular_end=dt_time(5, 0),
        aftermarket_end=dt_time(8, 0),
    )
    
    WINTER = MarketTimes(
        premarket_start=dt_time(18, 0),
        premarket_order=dt_time(18, 30),
        premarket_end=dt_time(23, 30),
        regular_start=dt_time(23, 30),
        regular_end=dt_time(6, 0),
        aftermarket_end=dt_time(8, 0),
    )
    
    @classmethod
    def is_summer_time(cls, check_date: Optional[date] = None) -> bool:
        if check_date is None:
            check_date = datetime.now(cls.KST).date()
        
        year = check_date.year
        march_second_sun = cls._get_nth_weekday(year, 3, 2, 6)
        november_first_sun = cls._get_nth_weekday(year, 11, 1, 6)
        return march_second_sun <= check_date < november_first_sun
    
    @classmethod
    def _get_nth_weekday(cls, year, month, n, weekday):
        from datetime import date, timedelta
        first = date(year, month, 1)
        days_until = (weekday - first.weekday()) % 7
        first_occurrence = first + timedelta(days=days_until)
        return first_occurrence + timedelta(weeks=n-1)
    
    @classmethod
    def get_times(cls, check_date=None):
        return cls.SUMMER if cls.is_summer_time(check_date) else cls.WINTER


from datetime import date