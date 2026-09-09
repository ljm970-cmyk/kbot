"""APScheduler 프리장 30분 지연 실행 + WebSocket STAR 실시간 매도"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo
from datetime import datetime
from decimal import Decimal
import asyncio
import logging

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")


class PremarketDelayScheduler:
    """
    프리장 주문 스케줄러
    v4.1: WebSocket 실시간 STAR 매도 추가
    """
    
    def __init__(self, portfolio, executor, telegram=None, use_websocket=False):
        self.portfolio = portfolio
        self.executor = executor
        self.telegram = telegram
        self.use_websocket = use_websocket
        
        self.scheduler = AsyncIOScheduler(timezone=KST)
        self.ws_client = None
    
    async def initialize(self):
        """스케줄러 초기화"""
        now = datetime.now(KST)
        is_summer = self._is_summer(now.date())
        
        hour = 17 if is_summer else 18
        minute = 30
        
        # 1. 프리장 주문 스케줄 (매일)
        self.scheduler.add_job(
            self._place_orders,
            CronTrigger(hour=hour, minute=minute, timezone=KST),
            id="premarket_orders",
            replace_existing=True
        )
        
        # 2. WebSocket 실시간 STAR 체크 (선택사항)
        if self.use_websocket:
            await self._init_websocket()
            
            self.scheduler.add_job(
                self._check_star_realtime,
                "cron", minute="*/5",
                hour="16-21", timezone=KST,
                id="star_check",
            )
        
        logger.info(f"스케줄 등록: 매일 {hour:02d}:{minute:02d} KST, WebSocket={self.use_websocket}")
        self.scheduler.start()
    
    def _is_summer(self, check_date):
        from ..market.market_session import USMarketSession
        return USMarketSession.is_summer_time(check_date)
    
    # === WebSocket 관련 ===
    
    async def _init_websocket(self):
        """WebSocket 실시간 시세 연결"""
        from .kiwoom_us_websocket import KiwoomUSWebSocket   # 상대 import 또는 수정
        
        self.ws_client = KiwoomUSWebSocket(
            access_token=self.executor.client.access_token,
            app_key=self.executor.client.app_key,
            is_mock=self.executor.client.is_mock,
        )
        
        await self.ws_client.connect()
        await self.ws_client.subscribe_stock(self.portfolio.cfg.ticker, "FE")
        asyncio.create_task(self.ws_client.receive_loop())
        logger.info("WebSocket 실시간 시세 연결 완료")
    
    async def _check_star_realtime(self):
        """장중 STAR 조건 실시간 체크 → LOC 매도"""
        if not self.ws_client:
            return
        
        ticker = self.portfolio.cfg.ticker
        current_price = self.ws_client.get_current_price(ticker)
        
        if current_price is None:
            return
        
        star_target = self.portfolio.star_pct
        if star_target <= 0:
            return
        
        target_price = self.portfolio.avg_price * (Decimal("100") + star_target) / Decimal("100")
        
        if current_price >= target_price and self.portfolio.quantity > 0:
            logger.info(f"🎯 STAR 실시간 매도: {current_price} >= {target_price}")
            
            result = await self.executor.execute_sell_order_loc(
                ticker=ticker,
                price=str(target_price.quantize(Decimal("0.01"))),
                quantity=self.portfolio.quantity,
            )
            
            if self.telegram:
                await self.telegram.send_message(
                    f"🎯 STAR 실시간 매도!\n"
                    f"가격: {current_price} (목표: {target_price})\n"
                    f"주문번호: {result.get('ord_no')}"
                )
    
    # === 주문 실행 ===
    
    async def _get_current_price(self, ticker: str = "TQQQ") -> Decimal:
        """
        현재 시세 조회
        WebSocket 우선, 없으면 REST API
        """
        # WebSocket 체크
        if self.ws_client:
            ws_price = self.ws_client.get_current_price(ticker)
            if ws_price:
                return ws_price
        
        # REST API fallback
        return await self._get_current_price_rest(ticker)
    
    async def _get_current_price_rest(self, ticker: str = "TQQQ") -> Decimal:
        """REST API로 시세 조회"""
        try:
            result = await self.executor.client.get_current_price(ticker)
            
            price_str = result.get("output", {}).get("last", "0")
            if not price_str:
                price_str = result.get("last", "0")
            
            price = Decimal(str(price_str))
            logger.info(f"현재가(REST): {ticker} = {price}")
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
                result = await self._execute_single_order(order)
                
                # 결과 알림
                if self.telegram:
                    await self._notify_order(order, result)
                    
        except Exception as e:
            logger.error(f"주문 배치 실패: {e}", exc_info=True)
            if self.telegram:
                await self.telegram.send_message(f"❌ 주문 배치 실패: {str(e)}")
    
    async def _execute_single_order(self, order: dict) -> dict:
        """단일 주문 실행"""
        ticker = order.get("ticker", "TQQQ")
        quantity = order["quantity"]
        price = order.get("price", "0")
        side = order["side"]
        
        if side == "buy":
            return await self.executor.execute_buy_with_big_number_fallback(
                ticker=ticker,
                quantity=quantity,
                price=price
            )
        
        elif side == "sell":
            sell_type = order.get("sell_type", "loc")
            
            if sell_type == "loc":
                return await self.executor.execute_sell_order_loc(
                    ticker=ticker,
                    price=price,
                    quantity=quantity
                )
            elif sell_type == "moc":
                return await self.executor.execute_sell_order_moc(
                    ticker=ticker,
                    quantity=quantity
                )
        
        raise ValueError(f"Unknown side: {side}")
    
    async def _notify_order(self, order: dict, result: dict):
        """주문 결과 텔레그램 알림"""
        status = "✅" if result.get("return_code") == 0 else "❌"
        side = order["side"].upper()
        reason = order.get("reason", "")
        
        msg = (
            f"{status} {side}: {order['ticker']} {order['quantity']}주 @ {order.get('price', '0')}\n"
            f"주문번호: {result.get('ord_no', 'N/A')}\n"
            f"사유: {reason}"
        )
        await self.telegram.send_message(msg)
    
    async def shutdown(self):
        """스케줄러 종료"""
        if self.ws_client:
            await self.ws_client.close()
        
        self.scheduler.shutdown()
        logger.info("스케줄러 종료")
