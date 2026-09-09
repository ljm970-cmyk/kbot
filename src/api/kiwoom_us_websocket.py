"""키움증권 미국주식 WebSocket 실시간 시세"""

import websockets
import json
import asyncio
import logging
from decimal import Decimal

logger = logging.getLogger(__name__)


class KiwoomUSWebSocket:
    """
    키움증권 미국주식 WebSocket 실시간 시세
    """
    
    def __init__(self, access_token: str, app_key: str, is_mock: bool = False):
        self.access_token = access_token
        self.app_key = app_key
        self.is_mock = is_mock
        
        self.ws_url = (
            "wss://mockapi.kiwoom.com:10000" if is_mock 
            else "wss://api.kiwoom.com:10000"
        )
        
        self.ws = None
        self.running = False
        self.current_prices = {}  # {ticker: Decimal}
    
    async def connect(self):
        """WebSocket 연결"""
        import ssl
        
        # SSL 설정 (실전만)
        ssl_context = ssl.create_default_context() if not self.is_mock else None
        
        headers = {
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
        }
        
        self.ws = await websockets.connect(
            self.ws_url,
            extra_headers=headers,
            ssl=ssl_context
        )
        
        self.running = True
        logger.info(f"WebSocket 연결: {self.ws_url}")
    
    async def subscribe_stock(self, ticker: str = "TQQQ", tr_id: str = "FE"):
        """
        실시간 시세 등록 (체결가)
        tr_id: FE=체결가, FT=10호가
        """
        msg = {
            "trnm": "SUBS",
            "tr_id": tr_id,
            "item": ticker,
        }
        
        await self.ws.send(json.dumps(msg))
        
        response = await self.ws.recv()
        data = json.loads(response)
        
        logger.info(f"실시간 등록: {ticker} ({tr_id})")
        return data
    
    async def unsubscribe_stock(self, ticker: str, tr_id: str = "FE"):
        """실시간 해제"""
        msg = {
            "trnm": "UNSUBS",
            "tr_id": tr_id,
            "item": ticker,
        }
        
        await self.ws.send(json.dumps(msg))
        logger.info(f"실시간 해제: {ticker}")
    
    async def receive_loop(self, callback=None):
        """실시간 메시지 수신"""
        while self.running:
            try:
                message = await self.ws.recv()
                data = json.loads(message)
                
                # 체결가 파싱
                if "last" in data:
                    ticker = data.get("item", "TQQQ")
                    price = Decimal(str(data["last"]))
                    self.current_prices[ticker] = price
                    
                    if callback:
                        await callback(ticker, price)
                        
            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket 연결 종료")
                break
            except Exception as e:
                logger.error(f"WebSocket 오류: {e}")
    
    def get_current_price(self, ticker: str) -> Decimal:
        """최신 체결가 반환"""
        return self.current_prices.get(ticker)
    
    async def close(self):
        self.running = False
        if self.ws:
            await self.ws.close()
            logger.info("WebSocket 종료")
