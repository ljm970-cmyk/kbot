"""미국장 휴장일 계산"""

from datetime import datetime, timedelta, date


class USMarketCalendar:
    HOLIDAYS_2025 = [
        '20250101', '20250120', '20250217', '20250418', '20250526',
        '20250619', '20250704', '20250901', '20251127', '20251225',
    ]
    
    @classmethod
    def is_trading_day(cls, check_date=None):
        if check_date is None:
            check_date = date.today()
        
        if isinstance(check_date, datetime):
            check_date = check_date.date()
        
        if check_date.weekday() >= 5:
            return False
        
        date_str = check_date.strftime('%Y%m%d')
        return date_str not in cls.HOLIDAYS_2025
    
    @classmethod
    def next_trading_day(cls, from_date=None):
        if from_date is None:
            from_date = date.today()
        next_day = from_date + timedelta(days=1)
        while not cls.is_trading_day(next_day):
            next_day += timedelta(days=1)
        return next_day