"""
================================================================
일반모드 (Normal Mode)

[3] 매수중심 + 쿼터매도 + 지정가GTC
================================================================
"""

from typing import List, Optional

from core.star_point import StarPointCalculator
from modes.base_mode import BaseTradingMode


class NormalMode(BaseTradingMode):
    """
    일반모드 구현
    
    [3] 핵심:
    - 처음매수: 전일종가 +15% LOC
    - 1회매수금: 잔금/(division-T)
    - 전반전/후반전: 별지점-0.01 LOC + 폭락대비 LOC 5개
    - 쿼터매도: 보유 1/4, 별지점 LOC
    - 지정가매도: TQQQ+15%/SOXL+20% GTC
    """
    
    TARGET_PCT = {
        'TQQQ': 0.15,
        'SOXL': 0.20,
    }
    
    def generate_orders(self, state: dict) -> List[dict]:
        """다음 거래일 주문 생성"""
        orders = []
        T = state['T']
        avg_price = state['avg_price']
        holdings = state['holdings']
        cash = state['cash']
        
        # 별지점 계산
        star_calc = StarPointCalculator(self.ticker, self.division, 'normal')
        star = star_calc.calculate(avg_price, T) if avg_price > 0 else None
        
        # 1. LOC 매수 (전반전/후반전 공통)
        if cash > 0:
            buy_amount = self.calculate_buy_amount(cash, T)
            buy_price = star.buy_price if star else 0  # 초기는 시장가 대응
            
            qty = int(buy_amount / buy_price) if buy_price > 0 else 0
            
            if qty > 0:
                orders.append({
                    'type': 'LOC_BUY',
                    'api_id': 'ust21200',
                    'stk_cd': self.ticker,
                    'ord_uv': str(buy_price),
                    'ord_qty': str(qty),
                    'ord_gubun': '30',
                    'tag': 'main_buy'
                })
                
                # 폭락대비 LOC 매수 (최대 5개) [3]
                for i in range(1, 6):
                    fallback_qty = max(1, qty // (2 + i))
                    fallback_price = round(buy_price * (1 - 0.02 * i), 2)
                    orders.append({
                        'type': 'LOC_BUY_FALLBACK',
                        'api_id': 'ust21200',
                        'stk_cd': self.ticker,
                        'ord_uv': str(fallback_price),
                        'ord_qty': str(fallback_qty),
                        'ord_gubun': '30',
                        'tag': f'fallback_{i}'
                    })
        
        # 2. 쿼터 LOC 매도 (보유 있을 때)
        if holdings > 0:
            sell_qty = round(holdings / 4)
            if sell_qty > 0 and star:
                orders.append({
                    'type': 'LOC_SELL_QUARTER',
                    'api_id': 'ust21201',
                    'stk_cd': self.ticker,
                    'ord_uv': str(star.sell_price),
                    'ord_qty': str(sell_qty),
                    'ord_gubun': '30',
                    'tag': 'quarter_sell'
                })
                
                # 3. 지정가매도 (GTC) - 남은 물량
                remaining = holdings - sell_qty
                if remaining > 0:
                    target_pct = self.TARGET_PCT[self.ticker]
                    target_price = round(avg_price * (1 + target_pct), 2)
                    
                    orders.append({
                        'type': 'GTC_SELL',
                        'api_id': 'ust21201',
                        'stk_cd': self.ticker,
                        'ord_uv': str(target_price),
                        'ord_qty': str(remaining),
                        'ord_gubun': '00',  # 지정가
                        'rsrv_ord_tp': '2',  # 기간예약
                        'tag': 'target_gtc'
                    })
        
        return orders
    
    def apply_fill(self, state: dict, fill: dict) -> dict:
        """체결 반영"""
        from core.t_calculator import TCalculator
        
        calc = TCalculator(state['division'])
        calc.set_state(state['T'])
        
        fill_type = fill.get('fill_type')  # 'buy' or 'sell'
        qty = fill.get('fill_qty', 0)
        price = fill.get('fill_price', 0)
        
        if fill_type == 'buy':
            # 잔금 감소
            trade_amount = price * qty
            fee = trade_amount * state['fee_rate']
            state['cash'] -= (trade_amount + fee)
            
            # 평단 재계산
            old_value = state['avg_price'] * state['holdings']
            new_value = trade_amount
            state['holdings'] += qty
            if state['holdings'] > 0:
                state['avg_price'] = (old_value + new_value) / state['holdings']
            
            # T값
            buy_ratio = self._calc_buy_ratio(state, trade_amount)
            result = calc.add_buy_normal(buy_ratio)
            state['T'] = result.t_after
            
        elif fill_type == 'sell':
            # 잔금 증가
            trade_amount = price * qty
            fee = trade_amount * state['fee_rate']
            state['cash'] += (trade_amount - fee)
            state['holdings'] -= qty
            
            # 쿼터매도
            result = calc.apply_quarter_sell_normal()
            state['T'] = result.t_after
            
            # 보유 0 → 사이클 종료
            if state['holdings'] <= 0:
                state['holdings'] = 0
                state['avg_price'] = 0
                state['T'] = 0
                state['mode'] = 'normal'
        
        state['history'].append({
            'date': fill.get('fill_date', ''),
            'action': fill_type,
            'qty': qty,
            'price': price,
            'fee': fee,
            'T_before': result.t_before if 'result' in dir() else state['T'],
            'T_after': state['T']
        })
        
        return state
    
    def check_transition(self, state: dict) -> Optional[str]:
        """모드 전환 체크"""
        if state['T'] > (state['division'] - 1):
            return "reverse"  # 소진 → 리버스
        
        if state['holdings'] <= 0 and state['T'] == 0:
            # 사이클 종료, 재시작 가능
            state['cash'] = state['principal']  # 원금 복원
            return None
        
        return None  # 유지
    
    def _calc_buy_ratio(self, state: dict, trade_amount: float) -> float:
        """매수 비율 계산"""
        standard = self.calculate_buy_amount(state['cash'] + trade_amount, state['T'])
        if standard <= 0:
            return 1.0
        ratio = trade_amount / standard
        return min(ratio, 1.0)
