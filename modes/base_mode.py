"""
================================================================
매매 모드 공통 구조

모드는 '주문 계획'만 만든다. 실제 접수는 scheduler 가, 체결 반영은
eod/calculator 가 담당한다. 모드를 순수 함수에 가깝게 유지해야
방법론 수치 예제로 테스트할 수 있다.
================================================================
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from core.star_point import round_half_up
from core.t_calculator import FillKind


# ================================================================
# 입력
# ================================================================

@dataclass
class MarketSnapshot:
    """주문 생성에 필요한 시세.

    Attributes:
        prev_close: 전일 종가 (처음매수 기준가)
        current_price: 현재가 (가격제한폭 판정용)
        recent_closes: 직전 5거래일 종가 (리버스모드 별지점)
    """
    prev_close: float = 0.0
    current_price: float = 0.0
    recent_closes: list[float] = field(default_factory=list)


@dataclass
class PositionState:
    """종목별 매매 상태.

    dict 기반 state 와 상호 변환한다 (state_manager 호환).
    """
    ticker: str
    division: int
    principal: float
    fee_rate: float
    mode: str = "normal"            # normal | reverse
    T: float = 0.0
    avg_price: float = 0.0          # 내부는 반올림하지 않은 값을 유지
    holdings: int = 0
    cash: float = 0.0
    reverse_first_day: bool = False
    reverse_start_T: float = 0.0
    last_close_price: float = 0.0

    # --- 회로차단기 (core/reconciler.py) ---
    halted: bool = False            # True 면 신규 주문을 내지 않는다
    halt_reason: str = ""
    halted_at: str = ""
    eod_failures: int = 0           # EOD 연속 실패 횟수
    last_eod_date: str = ""         # 마지막으로 정산한 거래일 (중복 정산 방지)
    last_star_point: float = 0.0    # 마지막 계획의 별지점 (텔레그램 표시용)

    # --- dict 스타일 읽기 (tg_bot 호환) ---
    # 기존 텔레그램 코드가 state['T'] 처럼 접근한다. 재작성 비용을 피하려고
    # 읽기만 dict 처럼 열어둔다. 쓰기는 속성으로만 한다 (오타로 없는 키를
    # 만들어 상태가 조용히 어긋나는 것을 막기 위해).

    def __getitem__(self, key: str):
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key) from None

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)

    def get(self, key: str, default=None):
        return getattr(self, key, default)

    def keys(self):
        return self.to_dict().keys()

    @classmethod
    def from_dict(cls, d: dict) -> "PositionState":
        return cls(
            ticker=d["ticker"],
            division=int(d["division"]),
            principal=float(d.get("principal", 0)),
            fee_rate=float(d.get("fee_rate", 0)),
            mode=d.get("mode", "normal"),
            T=float(d.get("T", 0.0)),
            avg_price=float(d.get("avg_price", 0.0)),
            holdings=int(d.get("holdings", 0)),
            cash=float(d.get("cash", 0.0)),
            halted=bool(d.get("halted", False)),
            halt_reason=d.get("halt_reason", ""),
            halted_at=d.get("halted_at", ""),
            eod_failures=int(d.get("eod_failures", 0)),
            last_eod_date=d.get("last_eod_date", ""),
            last_star_point=float(d.get("last_star_point", 0.0)),
            reverse_first_day=bool(d.get("reverse_first_day", False)),
            reverse_start_T=float(d.get("reverse_start_T", 0.0)),
            last_close_price=float(d.get("last_close_price", 0.0)),
        )

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "division": self.division,
            "principal": self.principal,
            "fee_rate": self.fee_rate,
            "mode": self.mode,
            "T": self.T,
            "avg_price": self.avg_price,
            "holdings": self.holdings,
            "cash": self.cash,
            "reverse_first_day": self.reverse_first_day,
            "reverse_start_T": self.reverse_start_T,
            "last_close_price": self.last_close_price,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "halted_at": self.halted_at,
            "eod_failures": self.eod_failures,
            "last_eod_date": self.last_eod_date,
            "last_star_point": self.last_star_point,
        }


# ================================================================
# 출력
# ================================================================

class SubmitWindow:
    """접수 시점 구분.

    지정가매도는 프리장부터 애프터까지 효력이 있으므로 프리장 시작에 맞춰
    걸고, LOC/MOC 는 종가 기준이라 언제 걸어도 무방하다.
    """
    PRE_MARKET = "pre_market"   # 프리장 시작 (서머 17:00 / 비서머 18:00 KST)
    REGULAR = "regular"         # LOC/MOC 접수 창구
    ANY = "any"                 # 기본값 — REGULAR 창구에서 함께 접수된다


@dataclass
class PlannedOrder:
    """접수 예정 주문 1건.

    tag 는 FillKind 값이며, 접수 후 ord_no ↔ tag 매핑으로 저장해야
    체결 시 T값을 정확히 계산할 수 있다 (키움 API 에 주문 태그 필드가 없다).
    """
    tag: str
    side: str                 # buy | sell
    trade_type: str           # TradeType 값
    qty: int
    price: Optional[float] = None
    window: str = SubmitWindow.ANY
    note: str = ""

    @property
    def amount(self) -> float:
        return round_half_up((self.price or 0.0) * self.qty, 2)

    def __str__(self) -> str:
        p = f"@{self.price:.2f}" if self.price is not None else "@MKT"
        return f"{self.side.upper():4} {self.trade_type} {self.qty:>4}주 {p}  [{self.tag}] {self.note}"


@dataclass
class OrderPlan:
    """하루치 주문 계획"""
    ticker: str
    mode: str
    T: float
    orders: list[PlannedOrder] = field(default_factory=list)
    unit_amount: float = 0.0
    star_point: Optional[float] = None
    warnings: list[str] = field(default_factory=list)

    #: 소진 판정 (라오어 2026-08-02 보강)
    #:   일반모드 : 1회 매수로 1주도 살 수 없는 시점 → 리버스모드 전환
    #:   리버스모드: 쿼터매수(잔금÷4)로 1주도 살 수 없는 시점
    #:               → 매수 중단, MOC 매도만 시행
    exhausted: bool = False

    @property
    def buys(self) -> list[PlannedOrder]:
        return [o for o in self.orders if o.side == "buy"]

    @property
    def sells(self) -> list[PlannedOrder]:
        return [o for o in self.orders if o.side == "sell"]

    @property
    def max_buy_amount(self) -> float:
        """모든 매수가 체결됐을 때의 최대 소요 금액"""
        return round_half_up(sum(o.amount for o in self.buys), 2)

    def summary(self) -> str:
        head = f"[{self.ticker}] {self.mode} T={self.T:.4f} 1회매수액=${self.unit_amount:,.2f}"
        if self.star_point is not None:
            head += f" 별지점={self.star_point:.2f}"
        if self.exhausted:
            head += "  [소진]"
        lines = [head] + [f"  {o}" for o in self.orders]
        lines += [f"  ⚠ {w}" for w in self.warnings]
        return "\n".join(lines)


# ================================================================
# 추상 클래스
# ================================================================

class BaseTradingMode(ABC):
    """매매 모드 공통 인터페이스"""

    def __init__(self, state: PositionState):
        self.state = state

    @property
    def ticker(self) -> str:
        return self.state.ticker

    @property
    def division(self) -> int:
        return self.state.division

    @abstractmethod
    def plan(self, market: MarketSnapshot) -> OrderPlan:
        """다음 거래일 주문 계획 생성"""

    def unit_amount(self) -> float:
        """1회 매수 시도액 = 잔금 / (분할수 - T)"""
        denom = self.division - self.state.T
        if denom <= 0 or self.state.cash <= 0:
            return 0.0
        return self.state.cash / denom
