"""APScheduler 프리장 30분 지연 실행"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo
import logging

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")


class PremarketDelayScheduler:
    def __init__(self, portfolio, executor, telegram=None):
        self.portfolio = portfolio
        self.executor = executor
        self.telegram = telegram
        self.scheduler = AsyncIOScheduler(timezone=KST)
    
    async def initialize(self):
        from datetime import datetime
        
        now = datetime.now(KST)
        is_summer = self._is_summer(now.date())
        
        hour = 17 if is_summer else 18
        minute = 30
        
        self.scheduler.add_job(
            self._place_orders,
            CronTrigger(hour=hour, minute=minute, timezone=KST),
            id="premarket_orders",
            replace_existing=True
        )
        
        logger.info(f"스케줄 등록: 매일 {hour:02d}:{minute:02d} KST")
        self.scheduler.start()
    
    def _is_summer(self, check_date):
        from ..market.market_session import USMarketSession
        return USMarketSession.is_summer_time(check_date)
    
    async def _place_orders(self):
        logger.info("⏰ 프리장 주문 배치 시작 (30분 지연)")
        # TODO: 주문 생성 및 배치
        pass