"""
================================================================
써머타임/비써머타임 자동 구분

기존 [4]의 _get_dst_info() 메서드를 독립 모듈로 분리
================================================================
"""

from datetime import datetime, timedelta

import pytz


class USMarketTimezone:
    """
    미국 증시 시간대 (동부 시간 기준)
    
    [2][3] 영웅문 Global 기준:
    - 써머타임: KST 17:30 주문 실행
    - 비써머타임: KST 18:30 주문 실행
    """
    
    KST = pytz.timezone('Asia/Seoul')
    EASTERN = pytz.timezone('US/Eastern')
    
    # 주문 실행 시간 (영웅문 Global 기준) [3]
    ORDER_SCHEDULE = {
        'summer': (17, 30),   # 써머타임: KST 17:30
        'winter': (18, 30),   # 비써머타임: KST 18:30
    }
    
    # EOD 계산 시간 (써머타임/비써머타임 공용) [2]
    EOD_HOUR = 6
    EOD_MINUTE = 0
    
    @classmethod
    def is_summer_time(cls, kst_datetime: datetime = None) -> bool:
        """써머타임 여부"""
        if kst_datetime is None:
            kst_datetime = datetime.now(cls.KST)
        
        eastern_dt = kst_datetime.astimezone(cls.EASTERN)
        return eastern_dt.dst() != timedelta(0)
    
    @classmethod
    def get_dst_info(cls) -> tuple:
        """
        기존 [4]의 _get_dst_info()와 호환
        
        Returns:
            (target_hour, season_text)
            - summer: (17, "🌞 서머타임 (17:30)")
            - winter: (18, "❄️ 겨울 (18:30)")
        """
        is_summer = cls.is_summer_time()
        if is_summer:
            return 17, "🌞 서머타임 (17:30)"
        return 18, "❄️ 겨울 (18:30)"
    
    @classmethod
    def get_next_order_time(cls) -> datetime:
        """다음 주문 실행 시각"""
        now = datetime.now(cls.KST)
        is_summer = cls.is_summer_time(now)
        mode = 'summer' if is_summer else 'winter'
        hour, minute = cls.ORDER_SCHEDULE[mode]
        
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        
        # 이미 지났으면 내일
        if target <= now:
            target += timedelta(days=1)
            # 내일 DST 바뀔 수 있으므로 재확인
            is_summer = cls.is_summer_time(target)
            mode = 'summer' if is_summer else 'winter'
            hour, minute = cls.ORDER_SCHEDULE[mode]
            target = target.replace(hour=hour, minute=minute)
        
        return target
    
    @classmethod
    def get_next_eod_time(cls) -> datetime:
        """다음 EOD 계산 시각 (06:00 공용)"""
        now = datetime.now(cls.KST)
        target = now.replace(hour=cls.EOD_HOUR, minute=cls.EOD_MINUTE, second=0, microsecond=0)
        
        if target <= now:
            target += timedelta(days=1)
        
        return target
