"""키움증권 미국주식 REST API 클라이언트"""

import httpx
import os
from decimal import Decimal
from typing import Optional, List
import logging

logger = logging.getLogger(__name__)


class KiwoomUSClient:
    """
    키움증권 미국주식 REST API 클라이언트
    v4 실전/모의 설정 연결
    """
    
    # 공식 문서 기준 도메인 (수정됨)
    PRD_URL = "https://openapi.kiwoom.com"      # ✅ openapi 추가
    MOCK_URL = "https://mockapi.kiwoom.com"       # ✅ hyphen 제거
    
    def __init__(
        self,
        app_key: str,
        app_secret: str,
        access_token: str = "",
        is_mock: bool = False,
        base_url: str = ""  # 외부에서 주입 가능
    ):
        self.app_key = app_key
        self.app_secret = app_secret
        self.access_token = access_token
        
        # base_url 우선순위: 주입값 > mock 분기 > 기본값
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
        
        logger.info(f"KiwoomUSClient 초기화: {self.base_url} (mock={is_mock})")
    
    # === 토큰 발급 ===
    async def issue_token(self) -> str:
        """
        OAuth 2.0 클라이언트 자격증명으로 토큰 발급
        공식 문서: au10001 접근토큰발급
        """
        url = f"{self.base_url}/oauth2/token"
        
        headers = {"Content-Type": "application/json;charset=UTF-8"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.app_secret  # 키움은 "secretkey" 필드명 사용
        }
        
        try:
            response = await self.client.post(url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            
            self.access_token = data["token"]
            expires = data.get("expires_dt", "unknown")
            
            logger.info(f"토큰 발급 성공, 만료: {expires}")
            return self.access_token
            
        except httpx.HTTPStatusError as e:
            logger.error(f"토큰 발급 실패: {e.response.status_code} - {e.response.text}")
            raise
        except KeyError:
            logger.error(f"토큰 응답 파싱 실패: {data}")
            raise
    
    def _headers(self, api_id: str) -> dict:
        """API 호출용 공통 헤더"""
        if not self.access_token:
            raise RuntimeError("access_token 없음. issue_token() 먼저 호출")
        
        return {
            "authorization": f"Bearer {self.access_token}",
            "api-id": api_id,
            "appkey": self.app_key,           # 일부 TR에 필요
            "secretkey": self.app_secret,      # 일부 TR에 필요
        }
    
    async def close(self):
        await self.client.aclose()
    
    # === 미국주식 API (TODO: 실제 TR 코드로 교체) ===
    async def get_positions(self) -> List[dict]:
        """원장잔고확인 (TODO: 실제 TR 코드 확인 필요)"""
        # TR: ust21070 → 실제 키움 문서에서 미국주식 잔고 TR 확인
        logger.warning("get_positions: TODO - 실제 API 호출 구현 필요")
        return []
    
    async def get_today_fills(self, ticker: str) -> List[dict]:
        """당일 주문체결 (TODO: 실제 TR 코드 확인 필요)"""
        logger.warning("get_today_fills: TODO - 실제 API 호출 구현 필요")
        return []


class V4OrderExecutor:
    """V4 전략 주문 실행기"""
    
    def __init__(self, client: KiwoomUSClient):
        self.client = client
    
    async def execute_buy_with_big_number_fallback(self, **kwargs):
        """큰수매수 fallback 포함 매수 (TODO)"""
        logger.warning("execute_buy: TODO")
        pass
    
    async def execute_sell_order_loc(self, ticker, price, quantity):
        """LOC 매도 (TODO)"""
        logger.warning("execute_sell_order_loc: TODO")
        pass
    
    async def execute_sell_order_moc(self, ticker, quantity):
        """MOC 매도 (TODO)"""
        logger.warning("execute_sell_order_moc: TODO")
        pass
    
    async def execute_sell_order_gtc(self, ticker, price, quantity):
        """GTC 매도 (TODO)"""
        logger.warning("execute_sell_order_gtc: TODO")
        pass
