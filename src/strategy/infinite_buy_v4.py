"""무한매수법 V4.0 핵심 알고리즘"""

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from enum import Enum, auto
from typing import Optional, List, Dict
from collections import deque
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
    
    def update_avg_price(self, new_qty: int, new_price: Decimal):
        total = self.quantity + new_qty
        if total <= 0:
            self.avg_price = Decimal("0")
            return
        total_cost = (self.avg_price * self.quantity) + (new_price * new_qty)
        self.avg_price = (total_cost / total).quantize(Decimal("0.0001"), ROUND_HALF_UP)


# datetime import 추가
from datetime import datetime