"""한 계좌 내 다종목 포트폴리오 (독립 원금)"""

from decimal import Decimal
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)


class PortfolioState:
    """
    V4.0 원칙: 종목별 완전 독립 원금
    리버스모드 중 타 종목 자금 유입 금지
    """
    
    def __init__(self, account_no: str):
        self.account_no = account_no
        self.strategies: Dict[str, 'StrategyInstance'] = {}
        self.state_managers: Dict[str, 'StateManager'] = {}
    
    def register_strategy(self, config):
        from ..strategy.infinite_buy_v4 import PositionState, InfiniteBuyV4Config
        from .state import StateManager
        
        ticker = config.ticker
        sm = StateManager(self.account_no, ticker)
        saved = sm.load()
        
        if saved:
            # 복구
            state = PositionState(config)
            # TODO: saved → state 복원
            logger.info(f"상태 복구: {ticker}")
        else:
            state = PositionState(config)
            logger.info(f"신규 생성: {ticker}")
        
        self.strategies[ticker] = StrategyInstance(config, state)
        self.state_managers[ticker] = sm
    
    def get_overview(self) -> str:
        lines = [
            f"📊 계좌 {self.account_no} 종합 현황",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for ticker, inst in sorted(self.strategies.items()):
            s = inst.state
            mode = "🔴리버스" if s.is_reverse_mode else "🟢일반"
            lines.append(f"**{ticker}** ({mode})")
            lines.append(f"  T={s.T:.4f} | 잔금 ${s.balance:,.2f} | 보유 {s.quantity}주")
        return "\n".join(lines)


class StrategyInstance:
    def __init__(self, config, state):
        self.config = config
        self.state = state
        self.is_active = True
        self.last_error = None