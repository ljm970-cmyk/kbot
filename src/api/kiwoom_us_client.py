"""키움증권 미국주식 REST API 클라이언트"""

import httpx
from typing import Dict, List
import logging

logger = logging.getLogger(__name__)


class OrderError(Exception):
    """주문 실패 예외"""
    pass


class KiwoomUSClient:
    """
    키움증권 미국주식 REST API 클라이언트
    """
    
    PRD_URL = "https://api.kiwoom.com"
    MOCK_URL = "https://mockapi.kiwoom.com"
    
    # 엔드포인트
    ORDER_ENDPOINT = "/api/us/ordr"
    ACCOUNT_ENDPOINT = "/api/us/acnt"
    MARKET_ENDPOINT = "/api/us/mrkcond"  # 시세 엔드포인트
    STOCKINFO_ENDPOINT = "/api/us/stkinfo"  # 종목정보
    
    # 거래소 코드 매핑
    EXCHANGE_MAP = {
        "AMEX": "NA",
        "NASDAQ": "ND",
        "NYSE": "NY",
        "NASD": "ND",
    }
    
    def __init__(
        self,
        app_key: str,
        app_secret: str,
        access_token: str = "",
        is_mock: bool = False,
        base_url: str = ""
    ):
        self.app_key = app_key
        self.app_secret = app_secret
        self.access_token = access_token
        
        if base_url:
            self.base_url = base_url
        else:
            self.base_url = self.MOCK_URL if is_mock else self.PRD_URL
        
        self.is_mock = is_mock
        
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=30.0,
            headers={"Content-Type": "application/json;charset=UTF-8"}
        )
        
        logger.info(f"KiwoomUSClient: {self.base_url} (mock={is_mock})")
    
    # === 토큰 발급 ===
    async def issue_token(self) -> str:
        url = f"{self.base_url}/oauth2/token"
        
        headers = {"Content-Type": "application/json;charset=UTF-8"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.app_secret
        }
        
        try:
            response = await self.client.post(url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            
            self.access_token = data["token"]
            logger.info(f"토큰 발급: 만료 {data.get('expires_dt', 'unknown')}")
            return self.access_token
            
        except httpx.HTTPStatusError as e:
            logger.error(f"토큰 발급 실패: {e.response.status_code} - {e.response.text}")
            raise
        except KeyError:
            logger.error(f"토큰 응답 파싱 실패: {data}")
            raise
    
    def _headers(self, api_id: str) -> dict:
        if not self.access_token:
            raise RuntimeError("access_token 없음. issue_token() 먼저 호출")
        return {
            "authorization": f"Bearer {self.access_token}",
            "api-id": api_id,
            "appkey": self.app_key,
            "secretkey": self.app_secret,
        }
    
    async def close(self):
        await self.client.aclose()
    
    # === 시세 API ===
    
    async def get_stock_info(self, ticker: str, exchange: str = "ND") -> Dict:
        """
        미국주식 종목정보 조회 - usa10100
        """
        body = {
            "api_id": "usa10100",
            "stex_tp": exchange,
            "stk_cd": ticker,
        }
        
        headers = self._headers("usa10100")
        
        response = await self.client.post(
            self.STOCKINFO_ENDPOINT,
            headers=headers,
            json=body
        )
        response.raise_for_status()
        return response.json()
    
    async def get_current_price(self, ticker: str, exchange: str = "ND") -> Dict:
        """
        미국주식 현재가 종목정보 - usa20100
        """
        body = {
            "api_id": "usa20100",
            "stex_tp": exchange,
            "stk_cd": ticker,
        }
        
        headers = self._headers("usa20100")
        
        response = await self.client.post(
            self.MARKET_ENDPOINT,
            headers=headers,
            json=body
        )
        response.raise_for_status()
        return response.json()
    
    async def get_daily_prices(self, ticker: str, exchange: str = "ND", base_date: str = "") -> Dict:
        """
        미국주식 일별주가 - usa20590
        base_date: 기준일자 (YYYYMMDD), 이전 내역 조회
        """
        body = {
            "api_id": "usa20590",
            "stex_tp": exchange,
            "stk_cd": ticker,
        }
        
        if base_date:
            body["base_dt"] = base_date
        
        headers = self._headers("usa20590")
        
        response = await self.client.post(
            self.MARKET_ENDPOINT,
            headers=headers,
            json=body
        )
        response.raise_for_status()
        return response.json()
    
    # === 주문 API ===
    async def _order(self, api_id: str, exchange: str, ticker: str,
                     quantity: int, trade_type: str, price: str = "") -> Dict:
        body = {
            "api_id": api_id,
            "stex_tp": exchange,
            "stk_cd": ticker,
            "ord_qty": str(quantity),
            "trde_tp": trade_type,
        }
        
        if trade_type in ("00", "30") and price:
            body["ord_uv"] = str(price)
        
        headers = self._headers(api_id=api_id)
        
        logger.info(f"주문: {api_id} {ticker} {quantity}주 type={trade_type} price={price or '시장가'}")
        
        response = await self.client.post(
            self.ORDER_ENDPOINT,
            headers=headers,
            json=body
        )
        response.raise_for_status()
        data = response.json()
        
        ret_code = data.get("return_code", -1)
        ret_msg = data.get("return_msg", "unknown")
        
        if ret_code != 0:
            logger.error(f"주문 실패: {ret_code} - {ret_msg}")
            raise OrderError(f"주문 실패: {ret_msg}")
        
        logger.info(f"주문 성공: ord_no={data.get('ord_no')}")
        return data
    
    # 매수
    async def buy_loc(self, ticker: str, quantity: int, price: str, exchange: str = "ND"):
        return await self._order("ust20000", exchange, ticker, quantity, "30", price)
    
    async def buy_moo(self, ticker: str, quantity: int, exchange: str = "ND"):
        return await self._order("ust20000", exchange, ticker, quantity, "31")
    
    async def buy_moc(self, ticker: str, quantity: int, exchange: str = "ND"):
        return await self._order("ust20000", exchange, ticker, quantity, "32")
    
    async def buy_limit(self, ticker: str, quantity: int, price: str, exchange: str = "ND"):
        return await self._order("ust20000", exchange, ticker, quantity, "00", price)
    
    # 매도
    async def sell_loc(self, ticker: str, quantity: int, price: str, exchange: str = "ND"):
        return await self._order("ust20001", exchange, ticker, quantity, "30", price)
    
    async def sell_moo(self, ticker: str, quantity: int, exchange: str = "ND"):
        return await self._order("ust20001", exchange, ticker, quantity, "31")
    
    async def sell_moc(self, ticker: str, quantity: int, exchange: str = "ND"):
        return await self._order("ust20001", exchange, ticker, quantity, "32")
    
    async def sell_limit(self, ticker: str, quantity: int, price: str, exchange: str = "ND"):
        return await self._order("ust20001", exchange, ticker, quantity, "00", price)
    
    # === 계좌/조회 API ===
    async def get_balance(self) -> Dict:
        body = {"api_id": "ust21070"}
        headers = self._headers("ust21070")
        
        response = await self.client.post(
            self.ACCOUNT_ENDPOINT,
            headers=headers,
            json=body
        )
        response.raise_for_status()
        return response.json()
    
    async def get_unfilled_orders(self) -> List[Dict]:
        body = {"api_id": "ust21050"}
        headers = self._headers("ust21050")
        
        response = await self.client.post(
            self.ACCOUNT_ENDPOINT,
            headers=headers,
            json=body
        )
        response.raise_for_status()
        return response.json().get("output", [])
    
    async def get_trade_history(self) -> List[Dict]:
        body = {"api_id": "ust21100"}
        headers = self._headers("ust21100")
        
        response = await self.client.post(
            self.ACCOUNT_ENDPOINT,
            headers=headers,
            json=body
        )
        response.raise_for_status()
        return response.json().get("output", [])
    
    async def get_deposit(self) -> Dict:
        body = {"api_id": "ust21110"}
        headers = self._headers("ust21110")
        
        response = await self.client.post(
            self.ACCOUNT_ENDPOINT,
            headers=headers,
            json=body
        )
        response.raise_for_status()
        return response.json()


class V4OrderExecutor:
    def __init__(self, client: KiwoomUSClient):
        self.client = client
    
    async def execute_buy_with_big_number_fallback(self, ticker: str, quantity: int, price: str):
        return await self.client.buy_loc(ticker, quantity, price)
    
    async def execute_sell_order_loc(self, ticker: str, price: str, quantity: int):
        return await self.client.sell_loc(ticker, quantity, price)
    
    async def execute_sell_order_moc(self, ticker: str, quantity: int):
        return await self.client.sell_moc(ticker, quantity)
    
    async def execute_sell_order_gtc(self, ticker: str, price: str, quantity: int):
        logger.warning("GTC는 LOC로 대체, 미체결 시 다음날 재주문 필요")
        return await self.client.sell_loc(ticker, quantity, price)
