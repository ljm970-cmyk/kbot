"""무한매수법 V4.0 핵심 알고리즘"""

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from enum import Enum, auto
from typing import Optional, List, Dict
from collections import deque
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class MarketPhase(Enum):
    EARLY_GAME = auto()
    LATE_GAME = auto()
    REVERSE_MODE = auto()


@dataclass
class InfiniteBuyV4Config:
    ticker: str
    principal_usd: Decimal
    total_splits: int
    compound: bool = False
    
    TARGET_PROFIT_PCT: Dict[str, Decimal] = field(default_factory=lambda: {
        "TQQQ": Decimal("15"),
        "SOXL": Decimal("20"),
    })
    
    REVERSE_EXIT_PCT: Dict[str, Decimal] = field(default_factory=lambda: {
        "TQQQ": Decimal("0.85"),
        "SOXL": Decimal("0.80"),
    })
    
    STAR_PARAMS: Dict = field(default_factory=lambda: {
        ("TQQQ", 20): (Decimal("15"), Decimal("1.5")),
        ("TQQQ", 40): (Decimal("15"), Decimal("0.75")),
        ("SOXL", 20): (Decimal("20"), Decimal("2")),
        ("SOXL", 40): (Decimal("20"), Decimal("1")),
    })
    
    MAX_CRASH_ORDERS: int = 5


class PriceHistory:
    def __init__(self, max_len: int = 5):
        self.closes: deque[Decimal] = deque(maxlen=max_len)
    
    def add(self, price: Decimal):
        self.closes.append(price)
    
    @property
    def five_day_avg(self) -> Optional[Decimal]:
        if len(self.closes) < 5:
            return None
        avg = sum(self.closes) / len(self.closes)
        return avg.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class PositionState:
    """V4 포지션 상태 및 계산"""
    
    def __init__(self, config: InfiniteBuyV4Config):
        self.cfg = config
        self.T: Decimal = Decimal("0")
        self.balance: Decimal = config.principal_usd
        self.quantity: int = 0
        self.avg_price: Decimal = Decimal("0")
        self.realized_pnl: Decimal = Decimal("0")
        self.price_history = PriceHistory()
        self.today_fills: List[dict] = []
        self.reverse_mode_day: int = -1
        self.cycle_count: int = 0
        self.cycle_start_date: Optional[datetime] = None
        self.cycle_start_principal: Decimal = config.principal_usd
    
    # === 속성 ===
    
    @property
    def phase(self) -> MarketPhase:
        threshold = Decimal(str(self.cfg.total_splits - 1))
        if self.T > threshold:
            return MarketPhase.REVERSE_MODE
        elif self.T >= Decimal(str(self.cfg.total_splits / 2)):
            return MarketPhase.LATE_GAME
        elif self.T >= Decimal("1.0"):
            return MarketPhase.EARLY_GAME
        else:
            return MarketPhase.EARLY_GAME if self.quantity > 0 else MarketPhase.EARLY_GAME
    
    @property
    def is_reverse_mode(self) -> bool:
        return self.T > Decimal(str(self.cfg.total_splits - 1))
    
    @property
    def one_buy_amount(self) -> Decimal:
        if self.T >= self.cfg.total_splits:
            return Decimal("0")
        denom = Decimal(str(self.cfg.total_splits)) - self.T
        return (self.balance / denom).quantize(Decimal("0.01"), ROUND_DOWN)
    
    @property
    def star_pct(self) -> Decimal:
        if self.phase == MarketPhase.REVERSE_MODE:
            return Decimal("0")
        base, coeff = self.cfg.STAR_PARAMS[(self.cfg.ticker, self.cfg.total_splits)]
        return base - coeff * self.T
    
    # === 기본 메서드 ===
    
    def update_avg_price(self, new_qty: int, new_price: Decimal):
        total = self.quantity + new_qty
        if total <= 0:
            self.avg_price = Decimal("0")
            return
        total_cost = (self.avg_price * self.quantity) + (new_price * new_qty)
        self.avg_price = (total_cost / total).quantize(Decimal("0.0001"), ROUND_HALF_UP)
    
    # === 주문 계산 ===
    
    def calculate_orders(self, current_price: Optional[Decimal] = None) -> List[Dict]:
        """
        V4 전략 기반 주문 계산
        """
        orders = []
        
        if self.quantity > 0 and current_price:
            sell_orders = self._calculate_sell_orders(current_price)
            orders.extend(sell_orders)
        
        buy_orders = self._calculate_buy_orders(current_price)
        orders.extend(buy_orders)
        
        return orders
    
    def _calculate_sell_orders(self, current_price: Decimal) -> List[Dict]:
        orders = []
        star_target = self.star_pct
        
        if star_target > 0:
            target_price = self.avg_price * (Decimal("100") + star_target) / Decimal("100")
            if current_price >= target_price:
                orders.append({
                    "side": "sell",
                    "quantity": self.quantity,
                    "price": str(target_price.quantize(Decimal("0.01"))),
                    "ticker": self.cfg.ticker,
                    "sell_type": "loc",
                    "reason": f"STAR({star_target}%)"
                })
        
        if self.is_reverse_mode and self.quantity > 0:
            exit_pct = self.cfg.REVERSE_EXIT_PCT.get(self.cfg.ticker, Decimal("0.85"))
            exit_price = self.avg_price * exit_pct
            if current_price <= exit_price:
                orders.append({
                    "side": "sell",
                    "quantity": self.quantity,
                    "price": str(exit_price.quantize(Decimal("0.01"))),
                    "ticker": self.cfg.ticker,
                    "sell_type": "moc",
                    "reason": f"REVERSE({exit_pct*100}%)"
                })
        
        return orders
    
    def _calculate_buy_orders(self, current_price: Optional[Decimal]) -> List[Dict]:
        orders = []
        
        if self.T >= self.cfg.total_splits:
            return orders
        
        buy_amount = self.one_buy_amount
        if buy_amount <= 0:
            return orders
        
        # 주식 수량 계산 (금액 / 현재가)
        qty = self._calculate_quantity(buy_amount, current_price)
        
        orders.append({
            "side": "buy",
            "quantity": qty,
            "price": str(current_price.quantize(Decimal("0.01"))) if current_price else "0",
            "ticker": self.cfg.ticker,
            "reason": f"T={self.T}"
        })
        
        return orders
    
    def _calculate_quantity(self, amount: Decimal, price: Optional[Decimal]) -> int:
        """금액 기준 주식 수량 계산"""
        if not price or price <= 0:
            return 1  # 기본값
        
        qty = int(amount / price)
        return max(qty, 1)  # 최소 1주
    
    # === 체결 처리 ===
    
    def record_fill(self, order: Dict, filled_qty: int, filled_price: Decimal):
        self.today_fills.append({
            "order": order,
            "filled_qty": filled_qty,
            "filled_price": filled_price,
            "time": datetime.now()
        })
        
        if order["side"] == "buy":
            self.update_avg_price(filled_qty, filled_price)
            self.quantity += filled_qty
            self.balance -= filled_price * filled_qty
            self.T += Decimal("1")
        elif order["side"] == "sell":
            self.quantity -= filled_qty
            pnl = (filled_price - self.avg_price) * filled_qty
            self.realized_pnl += pnl
            if self.quantity <= 0:
                self._reset_cycle()
    
    def _reset_cycle(self):
        self.quantity = 0
        self.avg_price = Decimal("0")
        self.T = Decimal("0")
        self.cycle_count += 1
        self.cycle_start_date = datetime.now().date()
        if self.cfg.compound:
            self.balance = self.balance + self.realized_pnl
            self.cycle_start_principal = self.balance
        else:
            self.balance = self.cfg.principal_usd
