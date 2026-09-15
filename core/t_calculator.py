"""
================================================================
T값 계산 엔진

무한매수법의 T(Turn)는 '개별 체결'이 아니라 '그 날 무엇이 체결됐는가'의
조합으로 정해진다. 같은 날 매도와 매수가 함께 나면 순서와 조합에 따라
결과가 달라지므로, 이 모듈은 일(日) 단위 API를 기본으로 한다.

--- 일반모드 -------------------------------------------------
  매수
    1회 매수  (별지점LOC + 평단LOC 모두 체결)   T + 1
    절반 매수 (둘 중 하나만 체결)                T + 0.5
    폭락대비 매수                                 T + 0   (T 불변)
  매도
    쿼터 LOC 매도 (별지점)                        T × 0.75
    3/4 지정가매도 단독                           T × 0.25
    둘 다 같은 날 체결                            T = 0  (사이클 종료)
  조합
    지정가매도 후 LOC 매수                        T × 0.25 + 1
                                                  T × 0.25 + 0.5
  순서
    같은 날 둘 이상 발생 시 항상 매도를 먼저 계산하고 매수를 나중에 계산

--- 체결 조합의 배타성 (중요) --------------------------------
  쿼터 LOC 매도(별지점)와 모든 LOC 매수(별지점-0.01, 평단, 폭락대비)는
  같은 '종가' 하나로 판정된다. 종가가 별지점 이상이면 매도만, 이하면
  매수만 체결되므로 둘은 같은 날 동시에 발생할 수 없다.

  반면 3/4 지정가매도는 장중 지정가라 고가가 목표가를 찍은 뒤 급락해
  종가가 매수가 아래로 내려가면 LOC 매수와 함께 체결될 수 있다.

  따라서 실제로 가능한 조합은 다음 둘뿐이다.
      지정가매도 + 쿼터매도   → T = 0 (사이클 종료)
      지정가매도 + LOC매수    → T × 0.25 + 1  또는  + 0.5

  불가능한 조합이 관측되면 데이터 이상(수동주문, 체결 누락, 태그 매핑
  오류)이므로 TResult.anomalies 에 기록해 알린다.

--- 리버스모드 ------------------------------------------------
  20분할   매도 T × 0.90 / 매수 T + (20 - T) × 0.25
  40분할   매도 T × 0.95 / 매수 T + (40 - T) × 0.25
  첫날 MOC 매도도 같은 매도 계수를 쓴다.

--- 소진 ------------------------------------------------------
  T > 분할수 - 1  →  리버스모드 전환
================================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ================================================================
# 체결 종류
# ================================================================

class FillKind:
    """주문 태그와 1:1로 대응하는 체결 종류.

    키움 API 에는 사용자 주문 태그 필드가 없으므로, 주문 접수 시
    ord_no ↔ FillKind 매핑을 로컬에 저장해두고 체결 때 역조회해야 한다.
    """
    # 일반모드 매수
    STAR_BUY = "star_buy"       # 별지점 LOC 매수
    AVG_BUY = "avg_buy"         # 평단 LOC 매수 (전반전 전용)
    HALF_BUY = "half_buy"       # 후반전: 1회매수액 전체를 별지점 LOC 한 건으로
    CRASH_BUY = "crash_buy"     # 폭락대비 LOC 매수 (T 불변)
    ENTRY_BUY = "entry_buy"     # 처음매수 (전일종가 +15% LOC)
    GUARD_BUY = "guard_buy"     # 방법론 8번 대체주문 (현재가 +15% LOC 단건)
    MERGED_BUY = "merged_buy"   # 별지점·평단 단가가 같아 한 건으로 합친 매수

    # 일반모드 매도
    QUARTER_SELL = "quarter_sell"   # 별지점 쿼터 LOC 매도
    TARGET_SELL = "target_sell"     # 3/4 목표 지정가매도

    # 리버스모드
    REVERSE_MOC_SELL = "reverse_moc_sell"
    REVERSE_SELL = "reverse_sell"
    REVERSE_QUARTER_BUY = "reverse_quarter_buy"


#: T값을 올리는 '본 매수' (폭락대비는 제외)
MAIN_BUY_KINDS = frozenset({
    FillKind.STAR_BUY, FillKind.AVG_BUY, FillKind.HALF_BUY,
    FillKind.ENTRY_BUY, FillKind.GUARD_BUY, FillKind.MERGED_BUY,
})

#: 단건으로 1회 매수액 전체를 소화하는 태그 → 체결 시 T +1
SINGLE_FULL_BUY_KINDS = frozenset({
    FillKind.HALF_BUY, FillKind.ENTRY_BUY, FillKind.GUARD_BUY,
    FillKind.MERGED_BUY,
})


# ================================================================
# 결과 구조
# ================================================================

@dataclass
class TStep:
    """T 변화 한 단계"""
    rule: str
    t_before: float
    t_after: float
    detail: str


@dataclass
class TResult:
    """하루치 T 계산 결과"""
    t_before: float
    t_after: float
    steps: list[TStep] = field(default_factory=list)
    cycle_closed: bool = False      # 사이클 종료 (보유 0 / 7-(3) 조건)
    exhausted: bool = False         # 소진 → 리버스 전환 대상
    anomalies: list[str] = field(default_factory=list)   # 이론상 불가능한 체결 조합

    @property
    def detail(self) -> str:
        return " → ".join(s.detail for s in self.steps) or "변화 없음"


@dataclass
class DayFills:
    """하루 동안 체결된 것들의 요약.

    EOD 계산기가 ord_no ↔ FillKind 매핑으로 채워서 넘긴다.

    Args:
        kinds: 그 날 체결된 FillKind 집합
        holdings_after: 그 날 모든 체결 반영 후 보유수량
    """
    kinds: set[str] = field(default_factory=set)
    holdings_after: int = 0

    @classmethod
    def from_list(cls, kinds, holdings_after: int = 0) -> "DayFills":
        return cls(set(kinds), holdings_after)

    def has(self, *kinds: str) -> bool:
        return any(k in self.kinds for k in kinds)

    @property
    def main_buys(self) -> set[str]:
        return self.kinds & MAIN_BUY_KINDS


# ================================================================
# 계산기
# ================================================================

class TCalculator:
    """T값 계산기.

    상태(T)를 들고 있으며, 하루 단위로 `apply_normal_day()` /
    `apply_reverse_day()` 를 호출한다.
    """

    QUARTER_SELL_FACTOR = 0.75      # 쿼터 LOC 매도
    TARGET_SELL_FACTOR = 0.25       # 3/4 지정가매도
    REVERSE_SELL_FACTOR = {20: 0.90, 40: 0.95}
    REVERSE_BUY_RATIO = 0.25

    def __init__(self, division: int, t: float = 0.0):
        if division not in (20, 40):
            raise ValueError(f"분할수는 20 또는 40이어야 합니다: {division}")
        self.division = division
        self.T = float(t)

    # ------------------------------------------------------------
    # 상태
    # ------------------------------------------------------------

    def get_state(self) -> float:
        return self.T

    def set_state(self, t: float) -> None:
        self.T = float(t)

    def is_exhausted(self) -> bool:
        """소진 여부. T > 분할수-1 이면 리버스모드로 전환한다.

        20분할은 T > 19, 40분할은 T > 39.
        """
        return self.T > (self.division - 1)

    def is_second_half(self) -> bool:
        """후반전 여부. T ≥ 분할수/2 부터 후반전(별% ≤ 0)."""
        return self.T >= (self.division / 2)

    # ------------------------------------------------------------
    # 일반모드 — 하루 단위
    # ------------------------------------------------------------

    def apply_normal_day(self, fills: DayFills) -> TResult:
        """일반모드 하루치 T 계산.

        순서: 지정가매도 → 쿼터매도 → 매수 (방법론 9번)
        """
        before = self.T
        steps: list[TStep] = []
        anomalies: list[str] = []
        closed = False

        target = fills.has(FillKind.TARGET_SELL)
        quarter = fills.has(FillKind.QUARTER_SELL)
        loc_buys = fills.kinds & (MAIN_BUY_KINDS | {FillKind.CRASH_BUY})

        # --- 배타성 검증 ---
        # 쿼터 LOC 매도와 LOC 매수는 같은 종가로 판정되므로 공존할 수 없다.
        if quarter and loc_buys:
            anomalies.append(
                f"쿼터 LOC매도와 LOC매수가 같은 날 체결됨 ({', '.join(sorted(loc_buys))}). "
                "종가 판정상 불가능한 조합이므로 수동주문·체결누락·태그매핑 오류를 확인하세요."
            )

        # --- 매도 ---
        if target and quarter:
            # 방법론 7-(3): 둘 다 체결되면 보유가 0이 되므로 T=0, 사이클 종료.
            steps.append(self._step(
                "sell_all", 0.0,
                "지정가매도+쿼터매도 동일일 체결 → T=0 (사이클 종료)"
            ))
            closed = True
            if fills.main_buys:
                # 이 조합은 이론상 발생하지 않는다. 발생했다면 매수를 반영하지
                # 않고 종료로 확정한 뒤 알린다 (상태 오염 방지).
                anomalies.append(
                    "매도 2건 동시 체결일에 매수 체결이 함께 관측됨. "
                    "매수는 T에 반영하지 않고 사이클 종료로 처리했습니다. 수동 확인 필요."
                )
            return TResult(before, self.T, steps, cycle_closed=True,
                           exhausted=False, anomalies=anomalies)

        if target:
            steps.append(self._mul(self.TARGET_SELL_FACTOR, "target_sell", "3/4 지정가매도"))
        elif quarter:
            steps.append(self._mul(self.QUARTER_SELL_FACTOR, "quarter_sell", "쿼터 LOC매도"))

        # --- 매수 ---
        # 방법론 7-(2): 지정가매도 후 크게 하락해 LOC 매수가 나면
        # 사이클은 종료되지 않고 이어진다.
        inc = self._buy_increment(fills)
        if inc > 0:
            steps.append(self._add(inc, "buy", self._buy_label(fills, inc)))
            closed = False

        # --- 보유 0 → 종료 ---
        if fills.holdings_after <= 0 and steps:
            if self.T != 0.0:
                steps.append(self._step("cycle_end", 0.0, "보유수량 0 → 사이클 종료"))
            closed = True

        return TResult(before, self.T, steps, cycle_closed=closed,
                       exhausted=self.is_exhausted(), anomalies=anomalies)

    def _buy_increment(self, fills: DayFills) -> float:
        """본 매수 체결 조합 → T 증분.

        1회 매수(별지점+평단 모두)는 +1, 절반 매수는 +0.5.
        후반전/처음매수는 주문이 한 건뿐이므로 체결되면 +1.
        폭락대비 매수는 T를 움직이지 않는다.
        """
        kinds = fills.main_buys
        if not kinds:
            return 0.0
        # 후반전 단건 · 처음매수 · 가격제한폭 대체주문 → 1회분
        if kinds & SINGLE_FULL_BUY_KINDS:
            return 1.0
        star = FillKind.STAR_BUY in kinds
        avg = FillKind.AVG_BUY in kinds
        if star and avg:
            return 1.0
        return 0.5

    @staticmethod
    def _buy_label(fills: DayFills, inc: float) -> str:
        names = ", ".join(sorted(fills.main_buys))
        kind = "1회 매수" if inc >= 1.0 else "절반 매수"
        crash = " (+폭락대비)" if fills.has(FillKind.CRASH_BUY) else ""
        return f"{kind}[{names}]{crash}"

    # ------------------------------------------------------------
    # 리버스모드 — 하루 단위
    # ------------------------------------------------------------

    def apply_reverse_day(self, fills: DayFills) -> TResult:
        """리버스모드 하루치 T 계산.

        매도 계수는 20분할 0.90 / 40분할 0.95 고정이다.
        실제 매도 수량 비율을 쓰면 안 된다 (내림 처리 때문에 어긋난다).
        """
        before = self.T
        steps: list[TStep] = []

        sold = fills.has(FillKind.REVERSE_MOC_SELL, FillKind.REVERSE_SELL)
        bought = fills.has(FillKind.REVERSE_QUARTER_BUY)

        if sold:
            factor = self.REVERSE_SELL_FACTOR[self.division]
            label = "첫날 MOC 매도" if fills.has(FillKind.REVERSE_MOC_SELL) else "리버스 LOC 매도"
            steps.append(self._mul(factor, "reverse_sell", label))

        if bought:
            inc = (self.division - self.T) * self.REVERSE_BUY_RATIO
            steps.append(self._add(
                inc, "reverse_quarter_buy",
                f"쿼터매수 +({self.division}-{before:.4f})×0.25"
            ))

        closed = fills.holdings_after <= 0 and bool(steps)
        if closed and self.T != 0.0:
            steps.append(self._step("cycle_end", 0.0, "보유수량 0 → 사이클 종료"))

        return TResult(before, self.T, steps, cycle_closed=closed)

    # ------------------------------------------------------------
    # 내부 연산
    # ------------------------------------------------------------

    def _mul(self, factor: float, rule: str, label: str) -> TStep:
        b = self.T
        self.T = b * factor
        return TStep(rule, b, self.T, f"{label}: {b:.4f}×{factor} = {self.T:.4f}")

    def _add(self, inc: float, rule: str, label: str) -> TStep:
        b = self.T
        self.T = b + inc
        return TStep(rule, b, self.T, f"{label}: {b:.4f}+{inc:.4f} = {self.T:.4f}")

    def _step(self, rule: str, value: float, label: str) -> TStep:
        b = self.T
        self.T = value
        return TStep(rule, b, self.T, label)

    # ------------------------------------------------------------
    # 단건 API (디버깅 / 수동 보정용)
    # ------------------------------------------------------------

    def normal_buy(self, ratio: float = 1.0) -> TResult:
        """수동 보정용: T + ratio (0.5 또는 1.0)"""
        before = self.T
        step = self._add(ratio, "manual_buy", "수동 매수")
        return TResult(before, self.T, [step], exhausted=self.is_exhausted())

    def normal_quarter_sell(self) -> TResult:
        before = self.T
        step = self._mul(self.QUARTER_SELL_FACTOR, "manual_quarter_sell", "수동 쿼터매도")
        return TResult(before, self.T, [step])

    def normal_target_sell(self) -> TResult:
        before = self.T
        step = self._mul(self.TARGET_SELL_FACTOR, "manual_target_sell", "수동 지정가매도")
        return TResult(before, self.T, [step])

    def reverse_sell(self) -> TResult:
        before = self.T
        factor = self.REVERSE_SELL_FACTOR[self.division]
        step = self._mul(factor, "manual_reverse_sell", "수동 리버스 매도")
        return TResult(before, self.T, [step])

    def reverse_quarter_buy(self) -> TResult:
        before = self.T
        inc = (self.division - self.T) * self.REVERSE_BUY_RATIO
        step = self._add(inc, "manual_reverse_buy", "수동 쿼터매수")
        return TResult(before, self.T, [step])


# ================================================================
# 1회 매수금
# ================================================================

def unit_buy_amount(cash: float, division: int, t: float) -> float:
    """1회 매수 시도액 = 잔금 / (분할수 - T)

    매도대금은 즉시 잔금에 더해지므로(방법론 10), 이 값은 매일 조금씩 변한다.
    리버스모드에서는 쓰지 않는다 (쿼터매수는 잔금/4).
    """
    denom = division - t
    if denom <= 0:
        return 0.0
    return cash / denom
