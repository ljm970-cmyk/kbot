"""
================================================================
End of Day 계산 엔진

장마감 후 일괄 계산:
1. 미처리 실시간 체결 내역 수집 (WebSocket SQLite)
2. 동일 거래일 체결 순서 정렬 (지정가→LOC매도→LOC매수)
3. T값/잔금/평단 순차 계산 (normal/reverse 분기)
4. 모드 전환 체크
5. 다음날 주문 생성
6. 상태 저장 + Cloud Storage 백업

기존 [4]의 장부 동기화 로직을 확장
================================================================
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List

import pytz

from core.star_point import StarPointCalculator
from core.state_manager import StateManager
from core.t_calculator import TCalculator, TResult
from kiwoom.websocket_handler import RealtimeFill, WebSocketFillReceiver

logger = logging.getLogger("kbot.eod")


class EndOfDayCalculator:
    """
    EOD 계산기
    
    [2][3] 이론:
    - 동일 거래일 체결 순서: 지정가매도 → 쿼터LOC매도 → LOC매수
    - T값 연속성: 리버스→일반 복귀 시 기존 T값 유지
    """
    
    def __init__(self, state_manager: StateManager, 
                 ws_receiver: WebSocketFillReceiver = None):
        self.state = state_manager
        self.ws = ws_receiver
    
    def calculate_eod(self, user_id: str, trade_date: datetime = None) -> Dict:
        """
        EOD 계산 실행
        
        Args:
            trade_date: 해당 거래일 (기본 어제)
        
        Returns:
            {
                'user_id': ...,
                'ticker': ...,
                'fills_processed': N,
                'final_T': ...,
                'final_cash': ...,
                'next_orders': [...],
                'mode': 'normal' or 'reverse'
            }
        """
        if trade_date is None:
            trade_date = datetime.now(pytz.timezone('Asia/Seoul')) - timedelta(days=1)
        
        date_str = trade_date.strftime('%Y%m%d')
        logger.info(f"[EOD] {date_str} 계산 시작")
        
        results = {}
        
        # 사용자의 활성 종목별 계산
        tickers = self.state.get_user_tickers(user_id)
        
        for ticker in tickers:
            result = self._calculate_ticker(user_id, ticker, trade_date)
            results[ticker] = result
            
            # 아카이브
            self.state.archive_eod(user_id, ticker, trade_date, result)
            
            # 다음날 주문 저장
            state = self.state.get_state(user_id, ticker)
            state['next_orders'] = result['next_orders']
            self.state.save_state(user_id, ticker, state)
        
        return results
    
    def _calculate_ticker(self, user_id: str, ticker: str,
                         trade_date: datetime) -> Dict:
        """단일 종목 EOD 계산"""
        
        state = self.state.get_state(user_id, ticker)
        if not state:
            return {'error': 'state not found'}
        
        cfg = self.state.get_ticker_config(user_id, ticker)
        
        # 1. 미처리 체결 수집
        fills = self._collect_fills(ticker, trade_date)
        
        # 2. 체결 순서 정렬 [3]
        sorted_fills = self._sort_fills(fills)
        
        # 3. 순차 계산
        calc = TCalculator(state['division'])
        calc.set_state(state['T'])
        
        details = []
        for fill in sorted_fills:
            detail = self._apply_fill(state, fill, calc)
            details.append(detail)
            
            # 처리 완료 표시
            if self.ws:
                self.ws.mark_processed(fill.fill_no)
        
        final_T = state['T']
        
        # 4. 모드 전환 체크
        mode_transition = None
        if state['mode'] == 'normal' and calc.is_exhausted():
            mode_transition = 'reverse'
            state['mode'] = 'reverse'
            state['reverse_first_day'] = True
            state['reverse_start_T'] = final_T
        elif state['mode'] == 'reverse':
            # 종가 확인 (state에 last_close 저장 필요)
            close_price = state.get('last_close_price', 0)
            if close_price > 0:
                star = StarPointCalculator(ticker, state['division'], 'reverse')
                if star.is_reverse_end(close_price, state['avg_price']):
                    mode_transition = 'normal'
                    state['mode'] = 'normal'
                    state['reverse_first_day'] = False
        
        # 5. 다음날 주문 생성
        from modes.normal_mode import NormalMode
        from modes.reverse_mode import ReverseMode
        
        if state['mode'] == 'normal':
            mode = NormalMode(ticker, state['division'], 
                            state['principal'], state['fee_rate'])
        else:
            mode = ReverseMode(ticker, state['division'], 
                             state['principal'], state['fee_rate'])
        
        next_orders = mode.generate_orders(state)
        
        # 저장
        state['T'] = final_T
        state['last_eod'] = trade_date.isoformat()
        self.state.save_state(user_id, ticker, state)
        
        return {
            'user_id': user_id,
            'ticker': ticker,
            'date': trade_date.strftime('%Y%m%d'),
            'fills_processed': len(fills),
            'final_T': final_T,
            'final_avg': state['avg_price'],
            'final_holdings': state['holdings'],
            'final_cash': state['cash'],
            'mode': state['mode'],
            'mode_transition': mode_transition,
            'calculation_details': details,
            'next_orders': next_orders,
        }
    
    def _collect_fills(self, ticker: str, trade_date: datetime) -> List[RealtimeFill]:
        """해당 거래일 미처리 체결 수집"""
        if not self.ws:
            return []
        
        # KST 기준 거래일 필터링
        day_start = trade_date.replace(hour=0, minute=0)
        day_end = trade_date + timedelta(days=1)
        
        all_fills = self.ws.get_unprocessed_fills(ticker)
        
        return [
            f for f in all_fills
            if day_start <= datetime.fromisoformat(f.kst_timestamp) < day_end
        ]
    
    def _sort_fills(self, fills: List[RealtimeFill]) -> List[RealtimeFill]:
        """
        동일 거래일 체결 순서 [3]
        
        1. 지정가매도 (GTC)
        2. 쿼터 LOC 매도
        3. LOC 매수
        """
        def priority(f: RealtimeFill) -> int:
            # 주문 유형 추정 (fill_no로 연결하면 정확)
            # 간략: 매수 2, 매도 1, 가격 높은 것 먼저
            if f.fill_type == 'sell':
                return 1  # 매도 우선
            return 2  # 매수 나중
        
        return sorted(fills, key=priority)
    
    def _apply_fill(self, state: Dict, fill: RealtimeFill,
                    calc: TCalculator) -> Dict:
        """단일 체결 적용"""
        
        before_T = state['T']
        qty = fill.fill_qty
        price = fill.fill_price
        trade_amount = qty * price
        fee = trade_amount * state['fee_rate']
        
        if fill.fill_type == 'buy':
            # 잔금 감소
            state['cash'] -= (trade_amount + fee)
            
            # 평단
            old_value = state['avg_price'] * state['holdings']
            new_value = trade_amount
            state['holdings'] += qty
            if state['holdings'] > 0:
                state['avg_price'] = round((old_value + new_value) / state['holdings'], 2)
            
            # T값
            if state['mode'] == 'normal':
                ratio = min(trade_amount / (state['cash'] + trade_amount + fee), 1.0)
                result = calc.add_buy_normal(ratio)
            else:
                result = calc.add_quarter_buy_reverse()
            
        else:  # sell
            # 잔금 증가
            state['cash'] += (trade_amount - fee)
            state['holdings'] -= qty
            
            # T값
            if state['mode'] == 'normal':
                result = calc.apply_quarter_sell_normal()
            else:
                if state.get('reverse_first_day', False):
                    result = calc.first_sell_reverse()
                    state['reverse_first_day'] = False
                else:
                    ratio = state['holdings'] / (state['holdings'] + qty) if (state['holdings'] + qty) > 0 else 1
                    result = calc.sell_reverse(ratio)
            
            # 일반모드 종료
            if state['mode'] == 'normal' and state['holdings'] <= 0:
                state['holdings'] = 0
                state['avg_price'] = 0
                state['T'] = 0
        
        state['T'] = result.t_after
        
        return {
            'fill': {
                'stock': fill.stock,
                'type': fill.fill_type,
                'qty': qty,
                'price': price,
                'fee': fee
            },
            'T_before': before_T,
            'T_after': result.t_after,
            'calc_detail': result.detail
        }
