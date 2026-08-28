"""키움증권 미국주식 REST API 클라이언트"""

import httpx
from decimal import Decimal
from typing import Optional, List
import logging

logger = logging.getLogger(__name__)


class KiwoomUSClient:
    PRD_URL = "https://api.kiwoom.com"
    MOCK_URL = "https://mock-api.kiwoom.com"
    
    def __init__(self, access_token: str, is_mock: bool = False):
        self.access_token = access_token
        self.is_mock = is_mock
        self.base_url = self.MOCK_URL if is_mock else self.PRD_URL
        
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=30.0,
            headers={"Content-Type": "application/json;charset=UTF-8"}
        )
    
    def _headers(self, api_id: str) -> dict:
        return {
            "authorization": f"Bearer {self.access_token}",
            "api-id": api_id,
        }
    
    async def close(self):
        await self.client.aclose()
    
    async def get_positions(self) -> List[dict]:
        """ust21070 원장잔고확인"""
        # TODO: 실제 API 호출
        return []
    
    async def get_today_fills(self, ticker: str) -> List[dict]:
        """ust21510 당일 주문체결"""
        # TODO: 실제 API 호출
        return []


class V4OrderExecutor:
    def __init__(self, client: KiwoomUSClient):
        self.client = client
    
    async def execute_buy_with_big_number_fallback(self, **kwargs):
        """큰수매수 fallback 포함 매수"""
        # TODO: 구현
        pass
    
    async def execute_sell_order_loc(self, ticker, price, quantity):
        """LOC 매도"""
        # TODO: 구현
        pass
    
    async def execute_sell_order_moc(self, ticker, quantity):
        """MOC 매도"""
        # TODO: 구현
        pass
    
    async def execute_sell_order_gtc(self, ticker, price, quantity):
        """GTC 매도"""
        # TODO: 구현
        pass