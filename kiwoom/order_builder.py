"""
================================================================
주문 객체 생성기

modes에서 생성한 주문을 키움 API 파라미터로 변환
================================================================
"""

from typing import Dict, List


class OrderBuilder:
    """주문 JSON 조립"""
    
    @staticmethod
    def build_loc_buy(ticker: str, price: float, qty: int, tag: str = "") -> dict:
        """LOC 매수"""
        return {
            'type': 'LOC_BUY',
            'api_id': 'ust21200',
            'stk_cd': ticker,
            'ord_uv': str(round(price, 2)),
            'ord_qty': str(qty),
            'ord_gubun': '30',  # LOC
            'rsrv_ord_tp': '1',  # 일반예약
            'tag': tag
        }
    
    @staticmethod
    def build_loc_sell(ticker: str, price: float, qty: int, tag: str = "") -> dict:
        """LOC 매도"""
        return {
            'type': 'LOC_SELL',
            'api_id': 'ust21201',
            'stk_cd': ticker,
            'ord_uv': str(round(price, 2)),
            'ord_qty': str(qty),
            'ord_gubun': '30',
            'rsrv_ord_tp': '1',
            'tag': tag
        }
    
    @staticmethod
    def build_moc_sell(ticker: str, qty: int, tag: str = "first_moc") -> dict:
        """MOC 매도"""
        return {
            'type': 'MOC_SELL',
            'api_id': 'ust21201',
            'stk_cd': ticker,
            'ord_qty': str(qty),
            'ord_gubun': '32',  # MOC
            'tag': tag
        }
    
    @staticmethod
    def build_gtc_sell(ticker: str, price: float, qty: int, period: str = "30") -> dict:
        """
        지정가매도 (GTC)
        
        [3] TQQQ+15%, SOXL+20%
        rsrv_ord_tp: 2 (기간예약), period 영업일
        """
        return {
            'type': 'GTC_SELL',
            'api_id': 'ust21201',
            'stk_cd': ticker,
            'ord_uv': str(round(price, 2)),
            'ord_qty': str(qty),
            'ord_gubun': '00',  # 지정가
            'rsrv_ord_tp': '2',  # 기간예약
            'period': period,
            'tag': 'target_gtc'
        }
    
    @staticmethod
    def build_batch(orders: List[dict]) -> List[dict]:
        """다건 주문 배치 처리"""
        # 키움 API 한 번에 보낼 수 있는 주문 수 확인 필요
        return orders
