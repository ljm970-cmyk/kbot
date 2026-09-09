"""APScheduler 프리장 30분 지연 실행"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")


class PremarketDelayScheduler:
    def __init__(self, portfolio, executor, telegram=None):
        self.portfolio = portfolio        # V4 포트폴리오 상태
        self.executor = executor          # V4OrderExecutor
        self.telegram = telegram          # 텔레그램 알림
        self.scheduler = AsyncIOScheduler(timezone=KST)
    
    async def initialize(self):
        now = datetime.now(KST)
        is_summer = self._is_summer(now.date())
        
        # 여름/겨울 시간에 따른 프리장 시작 시간
        # 미국 프리장 4:00 AM EST → KST 변환
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
        """프리장 주문 실행"""
        logger.info("⏰ 프리장 주문 배치 시작")
        
        if self.telegram:
            await self.telegram.send_message("🚀 프리장 주문 시작")
        
        try:
            # 1. 현재 포트폴리오 상태 확인
            balance = await self.executor.client.get_balance()
            logger.info(f"현재 잔고: {balance}")
            
            # 2. V4 전략 계산
            orders = self.portfolio.calculate_orders()
            # ↑ calculate_orders() 메서드가 strategy에 있어야 함
            
            if not orders:
                logger.info("주문 없음")
                return
            
            # 3. 주문 실행
            for order in orders:
                ticker = order.get("ticker", "TQQQ")
                quantity = order["quantity"]
                price = order.get("price", "0")
                side = order["side"]  # "buy" 또는 "sell"
                
                if side == "buy":
                    result = await self.executor.execute_buy_with_big_number_fallback(
                        ticker=ticker,
                        quantity=quantity,
                        price=price
                    )
                    logger.info(f"매수 주문: {ticker} {quantity}주 @ {price}")
                    
                elif side == "sell":
                    # 매도 유형 분기
                    sell_type = order.get("sell_type", "loc")
                    
                    if sell_type == "loc":
                        result = await self.executor.execute_sell_order_loc(
                            ticker=ticker,
                            price=price,
                            quantity=quantity
                        )
                    elif sell_type == "moc":
                        result = await self.executor.execute_sell_order_moc(
                            ticker=ticker,
                            quantity=quantity
                        )
                    
                    logger.info(f"매도 주문: {ticker} {quantity}주 type={sell_type}")
                
                # 4. 주문 결과 알림
                if self.telegram:
                    await self.telegram.send_message(
                        f"{'✅' if result.get('return_code') == 0 else '❌'} "
                        f"{side.upper()} 주문: {ticker} {quantity}주\n"
                        f"주문번호: {result.get('ord_no', 'N/A')}"
                    )
                    
        except Exception as e:
            logger.error(f"주문 배치 실패: {e}", exc_info=True)
            
            if self.telegram:
                await self.telegram.send_message(
                    f"❌ 주문 배치 실패: {str(e)}"
                )
    
    async def shutdown(self):
        """스케줄러 종료"""
        self.scheduler.shutdown()
        logger.info("스케줄러 종료")
