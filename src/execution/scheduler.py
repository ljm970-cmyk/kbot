"""APScheduler 프리장 30분 지연 실행"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo
from datetime import datetime
from decimal import Decimal
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
    
    async def _get_current_price(self, ticker: str = "TQQQ") -> Decimal:
        """
        현재 시세 조회 - usa20100 사용
        """
        try:
            result = await self.executor.client.get_current_price(ticker)
            
            # 응답 파싱 - 실제 응답 구조에 맞게 수정 필요
            price_str = result.get("output", {}).get("last", "0")
            if not price_str:
                price_str = result.get("last", "0")
            
            price = Decimal(str(price_str))
            logger.info(f"현재가: {ticker} = {price}")
            return price
            
        except Exception as e:
            logger.error(f"시세 조회 실패: {e}")
            raise
    
    async def _place_orders(self):
        """프리장 주문 실행"""
        logger.info("⏰ 프리장 주문 배치 시작")
        
        if self.telegram:
            await self.telegram.send_message("🚀 프리장 주문 시작")
        
        try:
            # 1. 현재 시세
            ticker = self.portfolio.cfg.ticker
            current_price = await self._get_current_price(ticker)
            
            # 2. 잔고 확인
            balance = await self.executor.client.get_balance()
            logger.info(f"현재 잔고: {balance}")
            
            # 3. 주문 계산
            orders = self.portfolio.calculate_orders(current_price)
            
            if not orders:
                logger.info("주문 없음")
                if self.telegram:
                    await self.telegram.send_message("ℹ️ 주문 없음 (STAR 미달 또는 분할 완료)")
                return
            
            # 4. 주문 실행
            for order in orders:
                ticker = order.get("ticker", "TQQQ")
                quantity = order["quantity"]
                price = order.get("price", "0")
                side = order["side"]
                
                if side == "buy":
                    result = await self.executor.execute_buy_with_big_number_fallback(
                        ticker=ticker,
                        quantity=quantity,
                        price=price
                    )
                    
                elif side == "sell":
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
                
                # 결과 알림
                if self.telegram:
                    status = "✅" if result.get("return_code") == 0 else "❌"
                    reason = order.get("reason", "")
                    msg = (
                        f"{status} {side.upper()}: {ticker} {quantity}주 @ {price}\n"
                        f"주문번호: {result.get('ord_no', 'N/A')}\n"
                        f"사유: {reason}"
                    )
                    await self.telegram.send_message(msg)
                    
        except Exception as e:
            logger.error(f"주문 배치 실패: {e}", exc_info=True)
            if self.telegram:
                await self.telegram.send_message(f"❌ 주문 배치 실패: {str(e)}")
    
    async def shutdown(self):
        self.scheduler.shutdown()
        logger.info("스케줄러 종료")
