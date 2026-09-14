"""
================================================================
키움 REST API 클라이언트 (운영 전용)

[1] 키움 REST API 문서 기반
모의투자 관련 코드 완전 제거
================================================================
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

import aiohttp
import requests

from config.settings import KiwoomConfig

logger = logging.getLogger("kbot.kiwoom")


class KiwoomAPIClient:
    """
    키움증권 REST API 클라이언트
    
    [1] API ID 목록:
    - ust21200: 미국주식 예약매수
    - ust21201: 미국주식 예약매도  
    - ust21202: 미국주식 예약정정
    - ust21203: 미국주식 예약취소
    - ust21102: 미국주식 예약주문가능금액
    """
    
    def __init__(self, config: KiwoomConfig):
        self.config = config
        self.token = ""
        self.token_expiry = None
        
        # 운영만 사용
        self.base_url = config.api_base
    
    # ============================================================
    # 인증
    # ============================================================
    
    def authenticate(self) -> bool:
        """
        OAuth2 토큰 발급
        
        Returns:
            성공 여부
        """
        try:
            url = f"{self.base_url}/oauth2/token"
            
            payload = {
                "grant_type": "client_credentials",
                "appkey": self.config.app_key,
                "secretkey": self.config.app_secret,
            }
            
            response = requests.post(url, json=payload, timeout=30)
            data = response.json()
            
            if response.status_code == 200 and 'token' in data:
                self.token = data['token']
                # expires_in 기반 만료 시각 설정
                expires_in = data.get('expires_in', 86400)
                self.token_expiry = datetime.now().timestamp() + expires_in - 300  # 5분 여유
                
                logger.info("키움 토큰 발급 성공")
                return True
            
            logger.error(f"토큰 발급 실패: {data}")
            return False
            
        except Exception as e:
            logger.error(f"토큰 발급 예외: {e}")
            return False
    
    def _get_headers(self) -> dict:
        """API 호출 헤더"""
        return {
            "Content-Type": "application/json;charset=UTF-8",
            "Authorization": f"Bearer {self.token}"
        }
    
    def _ensure_token(self):
        """토큰 유효성 확인 및 갱신"""
        now = datetime.now().timestamp()
        if not self.token or (self.token_expiry and now > self.token_expiry):
            self.authenticate()
    
    # ============================================================
    # 주문
    # ============================================================
    
    async def place_reserv_order(self, order: dict) -> dict:
        """
        예약주문 실행
        
        [1] ust21200(매수) / ust21201(매도)
        
        Args:
            order: order_builder 생성 dict
        
        Returns:
            API 응답
        """
        self._ensure_token()
        
        api_id = order.pop('api_id', 'ust21200')
        url = f"{self.base_url}/api/us/ordr"
        
        payload = {
            "api-id": api_id,
            "cont_yn": "N",
            "cont_key": "",
            "header": {
                "cont_yn": "N",
                "cont_key": "",
                "trx_id": api_id
            },
            "rows": [{
                "stk_cd": order.get('stk_cd'),
                "frn_prd_cd": "",  # 상품코드
                "slby_gubun": "2" if "BUY" in order.get('type', '') else "1",  # 2:매수, 1:매도
                "ord_gubun": order.get('ord_gubun', '30'),  # 30:LOC, 32:MOC, 00:지정가
                "ord_uv": order.get('ord_uv', '0'),         # 주문단가
                "ord_qty": order.get('ord_qty', '0'),       # 주문수량
                "rsrv_ord_tp": order.get('rsrv_ord_tp', '1'),  # 1:일반예약, 2:기간예약
                "srfid": order.get('tag', ''),  # 사용자 구분
            }]
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self._get_headers(), 
                                  json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                result = await resp.json()
                logger.info(f"주문 응답: {result.get('return_code')} - {result.get('return_msg')}")
                return result
    
    async def get_reserv_orders(self, fr_rsrv_dt: str, to_rsrv_dt: str,
                                ticker: str = "") -> List[dict]:
        """
        예약주문 내역조회 [1]
        
        ust21205: 미국주식 예약주문 내역조회
        """
        self._ensure_token()
        
        url = f"{self.base_url}/api/us/acnt"
        
        payload = {
            "api-id": "ust21205",
            "header": {
                "cont_yn": "N",
                "cont_key": "",
                "trx_id": "ust21205"
            },
            "rows": [{
                "fr_rsrv_dt": fr_rsrv_dt,  # 예약시작일 (YYYYMMDD)
                "to_rsrv_dt": to_rsrv_dt,  # 예약종료일
                "stk_cd": ticker,          # 종목코드
            }]
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self._get_headers(),
                                  json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                data = await resp.json()
                orders = data.get('response', {}).get('output2', [])
                return orders if orders else []
    
    async def cancel_reserv_order(self, rsrv_dt: str, rsrv_ord_no: str,
                                  ticker: str) -> dict:
        """
        예약주문취소 [1]
        
        ust21203: 미국주식 예약취소
        """
        self._ensure_token()
        
        url = f"{self.base_url}/api/us/ordr"
        
        payload = {
            "api-id": "ust21203",
            "header": {
                "cont_yn": "N",
                "cont_key": "",
                "trx_id": "ust21203"
            },
            "rows": [{
                "rsrv_dt": rsrv_dt,           # 예약일
                "rsrv_ord_no": rsrv_ord_no,     # 예약주문번호
                "stk_cd": ticker,
            }]
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self._get_headers(),
                                  json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                return await resp.json()
    
    # ============================================================
    # 시세
    # ============================================================
    
    async def get_current_price(self, ticker: str) -> float:
        """현재가 조회"""
        # ust16001 또는 별도 시세 API 활용
        # TODO: 키움 시세 API ID 확인 필요
        return 0.0
