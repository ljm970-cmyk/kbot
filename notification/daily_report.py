"""
일일 보고서 생성 및 텔레그램 발송.
"""

from datetime import datetime
from typing import Dict, List


class DailyReport:
    """장마감 후 텔레그램 보고서 생성."""
    
    @staticmethod
    def generate(state: 'State', transactions: List[Dict]) -> str:
        """Markdown 형식 일일 보고서."""
        
        now = datetime.now().strftime('%Y년 %m월 %d일')
        
        # 주문 통계
        buy_count = sum(1 for t in transactions if t.get('side') == 'BUY')
        sell_count = sum(1 for t in transactions if t.get('side') == 'SELL')
        
        # 포지션
        position_value = state.total_quantity * state.avg_price
        
        # 수익률
        if state.avg_price > 0 and state.total_quantity > 0:
            unrealized_pct = ((state.avg_price - state.avg_price) / state.avg_price) * 100  # 현재가 필요
        else:
            unrealized_pct = 0
        
        return f"""📊 *{now} 일일 매매 보고서*

━━━━━━━━━━━━━━━━━━━━

📋 *오늘의 주문*
├ 매수: {buy_count}건
└ 매도: {sell_count}건

━━━━━━━━━━━━━━━━━━━━

💼 *현재 포지션*
├ 종목: {state.stock_code}
├ 수량: {state.total_quantity}주
├ 평균단가: ${state.avg_price:.2f}
├ 평가금액: ${position_value:.2f}
└ T값: {state.t_value:.4f}

━━━━━━━━━━━━━━━━━━━━

💰 *자금 현황*
├ 원금: ${state.principal:.2f}
├ 잔금: ${state.balance:.2f}
├ 누적사이클: {state.completed_cycles}회
└ 누적손익: ${state.total_realized_pnl:+.2f}

━━━━━━━━━━━━━━━━━━━━

⏰ 보고 시각: {datetime.now().strftime('%H:%M:%S')} KST
"""
