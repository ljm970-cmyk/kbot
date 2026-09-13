"""
================================================================
APScheduler 기반 스케줄러

써머타임/비써머타임 자동 구분:
- KST 17:30 (써머타임) / 18:30 (비써머타임): 예약주문 실행
- KST 06:00: EOD 계산
================================================================
"""

import asyncio
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config.settings import AppConfig
from core.timezone_handler import USMarketTimezone

logger = logging.getLogger("kbot.scheduler")


class SchedulerEngine:
    """
    스케줄러 엔진
    
    기존 [5]의 polling 수동 방식을 Cron 기반 자동으로 대체
    """
    
    def __init__(self, config: AppConfig):
        self.config = config
        self.scheduler = AsyncIOScheduler(timezone='Asia/Seoul')
        self.tz_handler = USMarketTimezone()
    
    def start(self):
        """스케줄러 시작"""
        self._setup_jobs()
        self.scheduler.start()
        
        # 다음 작업 로깅
        jobs = self.scheduler.get_jobs()
        logger.info(f"스케줄러 시작, 등록 작업: {len(jobs)}개")
        for job in jobs:
            logger.info(f"  - {job.name}: {job.next_run_time}")
    
    def shutdown(self):
        """스케줄러 종료"""
        self.scheduler.shutdown()
        logger.info("스케줄러 종료")
    
    def _setup_jobs(self):
        """작업 등록"""
        
        # ============================================================
        # 1. 일일 주문 실행 (써머타임 17:30, 비써머타임 18:30)
        # ============================================================
        
        # 써머타임 체크: 매일 17:29 실행
        self.scheduler.add_job(
            func=self._check_and_order,
            trigger=CronTrigger(hour=17, minute=29),
            id='order_summer_check',
            name='주문 시간 체크 (써머타임)',
            misfire_grace_time=300
        )
        
        # 비써머타임 체크: 매일 18:29 실행
        self.scheduler.add_job(
            func=self._check_and_order,
            trigger=CronTrigger(hour=18, minute=29),
            id='order_winter_check',
            name='주문 시간 체크 (비써머타임)',
            misfire_grace_time=300
        )
        
        # ============================================================
        # 2. EOD 계산 (06:00 공용)
        # ============================================================
        
        self.scheduler.add_job(
            func=self._run_eod,
            trigger=CronTrigger(hour=6, minute=0),
            id='eod_daily',
            name='장마감 후 EOD 계산',
            misfire_grace_time=600
        )
        
        # ============================================================
        # 3. 보조 작업
        # ============================================================
        
        # 토큰 갱신 (새벽 2시)
        self.scheduler.add_job(
            func=self._refresh_token,
            trigger=CronTrigger(hour=2, minute=0),
            id='token_refresh',
            name='키움 토큰 선제적 갱신'
        )
        
        # 상태 체크 (매일 아침 9시)
        self.scheduler.add_job(
            func=self._health_check,
            trigger=CronTrigger(hour=9, minute=0),
            id='health_check',
            name='시스템 상태 체크'
        )
    
    # ============================================================
    # 작업 구현
    # ============================================================
    
    async def _check_and_order(self):
        """시간 체크 후 실제 주문 실행 결정"""
        is_summer = self.tz_handler.is_summer_time()
        now = datetime.now(self.tz_handler.KST)
        hour = now.hour
        
        expected = 17 if is_summer else 18
        
        # 매칭되면 주문 실행
        if hour == expected:
            await self._execute_daily_orders(is_summer)
    
    async def _execute_daily_orders(self, is_summer: bool):
        """일일 예약주문 실행"""
        season = "써머타임" if is_summer else "비써머타임"
        logger.info(f"[{season}] 일일 주문 실행 시작")
        
        # 실행 지연 (정확한 30분 맞춤)
        now = datetime.now()
        target = now.replace(minute=30, second=0, microsecond=0)
        if now < target:
            await asyncio.sleep((target - now).total_seconds())
        
        # TODO: kiwoom_api_client.place_reserv_order() 호출
        # TODO: telegram 알림 전송
        
        logger.info("일일 주문 실행 완료")
    
    async def _run_eod(self):
        """EOD 계산 실행"""
        logger.info("[EOD] 장마감 후 계산 시작")
        
        # TODO: eod.calculator 호출
        
        logger.info("[EOD] 계산 완료")
    
    async def _refresh_token(self):
        """키움 토큰 갱신"""
        logger.info("토큰 갱신 예약")
        # TODO: kiwoom_api_client.authenticate() 호출
    
    async def _health_check(self):
        """시스템 상태 체크"""
        logger.info("상태 체크")
        # TODO: WebSocket 연결, 상태 파일, 디스크 공간 확인
    
    # ============================================================
    # Cloud Scheduler Webhook (선택)
    # ============================================================
    
    def get_trigger_endpoint(self):
        """HTTP 트리거 (Cloud Scheduler 연동 시)"""
        # Flask/FastAPI 엔드포인트 등록용
        pass
