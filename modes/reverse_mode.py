"""
================================================================
리버스모드 (Reverse Mode)

[2] 매도중심 + 쿼터매수
================================================================
"""

from typing import List, Optional

from core.star_point import StarPointCalculator
from modes.base_mode import BaseTradingMode


class ReverseMode(BaseTradingMode):
    """
    리버스모드 구현
    
    [2] 핵심:
    - 처음매도: 무조건 MOC (전체÷10 or ÷20)
    - 이후: 별지점 LOC 매도 + 쿼터매수
    - 종료: 종가가 평단 대비 -15%(TQQQ)/-20%(SOXL) 회복
    """
    
    REVERSE_PCT = {
        'TQQQ': 0.15,
        'SOXL': 0.20,
    }
    
    def generate_orders(self, state: dict) -> List[dict]:
        """다음 거래일 주문 생성"""
        orders = []
        holdings = state['holdings']
        cash = state['cash']
        avg_price = state['avg_price']
        
        # 처음매도 여부
        is_first = state.get('reverse_first_day', True)
        
        # 별지점
        star_calc = StarPointCalculator(self.ticker, self.division, 'reverse')
        star = star_calc.calculate(avg_price, 0) if avg_price > 0 else None
        
        if is_first:
            # 1. MOC 처음매도 [2]
            if self.division == 20:
                sell_qty = max(1, holdings // 10)
            else:
                sell_qty = max(1, holdings // 20)
            
            orders.append({
                'type': 'MOC_SELL_FIRST',
                'api_id': 'ust21201',
                'stk_cd': self.ticker,
                'ord_qty': str(sell_qty),
                'ord_gubun': '32',  # MOC
                'tag': 'first_moc'
            })
            
            # 플래그 설정 (EOD 계산 후 업데이트)
            state['reverse_first_day'] = False
            
        else:
            # 2. LOC 매도 (별지점 위)
            if self.division == 20:
                sell_qty = max(1, holdings // 10)
            else:
                sell_qty = max(1, holdings // 20)
            
            if sell_qty > 0 and star:
                orders.append({
                    'type': 'LOC_SELL_REVERSE',
                    'api_id': 'ust21201',
                    'stk_cd': self.ticker,
                    'ord_uv': str(star.sell_price),
                    'ord_qty': str(sell_qty),
                    'ord_gubun': '30',
                    'tag': 'reverse_sell'
                })
            
            # 3. 쿼터매수 (별지점 아래, 잔금/4) [2]
            if cash > 0 and star:
                buy_amount = cash / 4
                buy_qty = int(buy_amount / star.buy_price) if star.buy_price > 0 else 0
                
                if buy_qty > 0:
                    orders.append({
                        'type': 'LOC_BUY_REVERSE',
                        'api_id': 'ust21200',
                        'stk_cd': self.ticker,
                        'ord_uv': str(star.buy_price),
                        'ord_qty': str(buy_qty),
                        'ord_gubun': '30',
                        'tag': 'quarter_buy'
                    })
        
        return orders
    
    def apply_fill(self, state: dict, fill: dict) -> dict:
        """체결 반영"""
        from core.t_calculator import TCalculator
        
        calc = TCalculator(state['division'])
        calc.set_state(state['T'])
        
        fill_type = fill.get('fill_type')
        qty = fill.get('fill_qty', 0)
        price = fill.get('fill_price', 0)
        
        if fill_type == 'sell':
            # 매도
            trade_amount = price * qty
            fee = trade_amount * state['fee_rate']
            state['cash'] += (trade_amount - fee)
            state['holdings'] -= qty
            
            if state.get('reverse_first_day', False):
                # 처음매도: T 감소
                result = calc.first_sell_reverse()
                state['reverse_first_day'] = False
            else:
                # 이후 매도: 비율 감소
                remaining_ratio = (state['holdings'] + qty) / max(state['holdings'], 1)
                result = calc.sell_reverse(remaining_ratio)
            
            state['T'] = result.t_after
            
        elif fill_type == 'buy':
            # 쿼터매수
            trade_amount = price * qty
            fee = trade_amount * state['fee_rate']
            state['cash'] -= (trade_amount + fee)
            state['holdings'] += qty
            
            # T 증가
            result = calc.add_quarter_buy_reverse()
            state['T'] = result.t_after
        
        return state
    
    def check_transition(self, state: dict) -> Optional[str]:
        """
        일반모드 복귀 체크
        
        [2] 종가가 평단 대비 기준% 이상 회복
        """
        avg_price = state['avg_price']
        if avg_price <= 0:
            return None
        
        star_calc = StarPointCalculator(self.ticker, self.division, 'reverse')
        
        # 최근 종가 확인 (최근 EOD 또는 현재가)
        close_price = state.get('last_close_price', 0)
        if close_price <= 0:
            return None
        
        if star_calc.is_reverse_end(close_price, avg_price):
            # 일반모드 복귀
            state['mode'] = 'normal'
            state['reverse_first_day'] = False
            return "normal"
        
        return None
