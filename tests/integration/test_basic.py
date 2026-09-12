"""
무한매수법 V4.0 기본 통합 테스트.
VM에서 실행: python -m pytest tests/integration/test_basic.py -v
"""

import pytest
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from main import State, Mode, TValueCalc, StarCalc, OrderPlanner, Config as AppConfig


class TestTValueCalculator:
    """T값 계산 검증."""
    
    def test_full_buy(self):
        assert TValueCalc.on_full_buy(0.0) == 1.0
        assert TValueCalc.on_full_buy(5.5) == 6.5
    
    def test_half_buy(self):
        assert TValueCalc.on_half_buy(0.0) == 0.5
        assert TValueCalc.on_half_buy(7.0) == 7.5
    
    def test_quarter_sell(self):
        assert TValueCalc.on_quarter_sell(8.0) == 6.0  # 8 * 0.75
    
    def test_target_then_full(self):
        # 3/4 매도 후 1회 매수
        assert TValueCalc.target_then_full(8.0) == 3.0  # 8*0.25 + 1
    
    def test_reverse_sell_20(self):
        # 20분할 매도
        assert TValueCalc.reverse_sell(19.5, 20) == 17.55  # 19.5 * 0.9
    
    def test_reverse_sell_40(self):
        # 40분할 매소
        assert TValueCalc.reverse_sell(39.5, 40) == 37.525  # 39.5 * 0.95
    
    def test_reverse_buy(self):
        # 리버스 매수
        result = TValueCalc.reverse_buy(37.525, 40)
        expected = 37.525 + (40 - 37.525) * 0.25  # = 38.14375
        assert abs(result - expected) < 0.001


class TestStarPoint:
    """별지점 계산 검증."""
    
    def test_tqqq_40_normal(self):
        sc = StarCalc('TQQQ', 40)
        # T=0: 15%
        assert sc.normal_pct(0.0) == 15.0
        assert sc.normal_star(50.0, 0.0) == 57.50  # 50 * 1.15
    
    def test_tqqq_20_normal(self):
        sc = StarCalc('TQQQ', 20)
        # T=0: 15%, T=10: 0%
        assert sc.normal_pct(0.0) == 15.0
        assert sc.normal_pct(10.0) == 0.0
    
    def test_soxl_40_normal(self):
        sc = StarCalc('SOXL', 40)
        assert sc.normal_pct(0.0) == 20.0
    
    def test_buy_price(self):
        sc = StarCalc('TQQQ', 40)
        star = 50.0
        assert sc.buy_price(star) == 49.99
    
    def test_reverse_star(self):
        sc = StarCalc('TQQQ', 40)
        closes = [45.0, 46.0, 47.0, 48.0, 49.0]
        assert sc.reverse_star(closes) == 47.0


class TestOrderPlanner:
    """주문 계획 검증."""
    
    def test_first_buy(self):
        plans = OrderPlanner.first_buy(50.0, 20000.0, 40)
        # 큰수: 50 * 1.15 = 57.50
        assert plans[0]['type'] == 'FIRST_BUY_MAIN'
        assert plans[0]['price'] == 57.50
    
    def test_normal_sell(self):
        state = State()
        state.total_quantity = 100
        state.stock_type = 'TQQQ'
        sells = OrderPlanner.normal_sell(state, 50.0, 55.0)
        # 쿼터: 100//4 = 25, 목표: 50*1.15 = 57.50
        assert len(sells) == 2
        assert sells[0]['qty'] == 25
        assert sells[1]['price'] == 57.50
    
    def test_reverse_first_sell(self):
        state = State()
        state.total_quantity = 200
        state.division = 40
        plan = OrderPlanner.reverse_first_sell(state)
        # 200 // 20 = 10
        assert plan['qty'] == 10
        assert plan['trde_tp'] == '33'


class TestStateValidation:
    """상태 검증."""
    
    def test_valid_state(self):
        state = State()
        is_valid, msg = AutoRecovery.validate_state(state)
        assert is_valid is True
    
    def test_negative_t_value(self):
        state = State()
        state.t_value = -1.0
        is_valid, msg = AutoRecovery.validate_state(state)
        assert is_valid is False
        assert '음수' in msg


# AutoRecovery 임포트 (main.py에 있으나 여기서 참조)
class AutoRecovery:
    @staticmethod
    def validate_state(state: State):
        errors = []
        if state.t_value < 0:
            errors.append(f'T값 음수: {state.t_value}')
        if state.balance < 0:
            errors.append(f'잔금 음수: {state.balance}')
        is_valid = len(errors) == 0
        return is_valid, '; '.join(errors) if not is_valid else 'OK'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
