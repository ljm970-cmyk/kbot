"""
================================================================
장부 대조 · 회로차단기

전략 장부(state)와 증권사 실제 잔고를 매일 대조한다.

이게 없으면 봇은 어긋난 상태를 스스로 알아차리지 못한다. 체결 하나가
누락되거나 EOD 가 하루 실패하면, 그 다음부터 잘못된 평단·보유수량 기준으로
매일 주문이 나간다. 며칠이 지나면 복구가 사실상 불가능해진다.

그래서 어긋남을 발견하면 **주문을 멈춘다**. 사람이 확인하고 명시적으로
풀기 전까지 신규 주문을 내지 않는다. 손실을 막는 것보다 잘못된 상태로
계속 주문하는 것을 막는 게 목적이다.

--- 판정 -----------------------------------------------------
  보유수량 불일치        치명 → 정지
  주문 자금 부족         치명 → 정지
  EOD 연속 실패          치명 → 정지
  평단 괴리 (허용치 초과) 경고 → 알림만
  평단 미세 차이          무시 (수수료 반영 방식 차이)

정지는 자동으로 풀리지 않는다. 텔레그램 /unhalt 로만 해제한다.
EOD 정산은 정지 중에도 계속 돈다 (상태를 최신으로 유지해야
사람이 판단할 수 있다). 막히는 것은 신규 주문 접수뿐이다.
================================================================
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger("kbot.reconcile")


class Severity:
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"


#: 평단 괴리 허용치.
#:
#: 증권사마다 매입단가 산정 관행이 다르다. 수수료를 원가에 포함시키기도 하고,
#: 매도 시 원가를 줄이는 방식도 제각각이다(이동평균 / FIFO / 매도대금 차감).
#: 그래서 우리 계산과 증권사 값이 구조적으로 조금 다를 수 있다.
#:
#: 다만 우리는 매일 증권사 값을 그대로 채택하므로(auto_correct), 괴리는
#: 하루치 거래에서만 누적된다. 그런데도 10% 를 넘는다면 관행 차이로 보기
#: 어렵고 체결 해석이 틀렸다는 뜻이다.
AVG_PRICE_TOLERANCE = 0.01          # 1% — 알림만
AVG_PRICE_CRITICAL = 0.05           # 5% — 정지
#
# 5% 로 되돌렸다. 증권사 관행 차이로 오탐이 날 수 있다는 우려로 10% 까지
# 올렸었는데, 그건 실측이 아니라 추측이었다. 오탐은 /unhalt 한 번으로
# 복구되지만, 어긋난 평단으로 며칠 매매하면 복구가 어렵다.
# 실제 운영에서 관행 차이가 확인되면 그때 수치를 근거로 조정할 것.

#: EOD 가 연속으로 몇 번 실패하면 정지할지
MAX_EOD_FAILURES = 2

#: 자동 교정 허용 범위. 이 안이면 조용히 맞추고 넘어간다.
#: 부분체결 반올림, 수수료 반영 시점 차이 정도는 매일 생길 수 있다.
AUTO_CORRECT_MAX_QTY = 2            # 주
AUTO_CORRECT_MAX_PCT = 0.03         # 보유수량의 3%


@dataclass
class Finding:
    severity: str
    code: str
    message: str

    def __str__(self) -> str:
        mark = {"critical": "✗", "warning": "!", "ok": "·"}.get(self.severity, "?")
        return f"{mark} {self.message}"


@dataclass
class Correction:
    """장부를 증권사 기준으로 맞춘 기록.

    증권사 잔고가 사실이고 전략 장부가 추정이다. 어긋나면 장부를 고친다.
    다만 고친 사실은 반드시 남긴다 (비파괴 보정).
    """
    field: str
    before: float
    after: float
    reason: str

    def __str__(self) -> str:
        return f"{self.field} {self.before:g} → {self.after:g} ({self.reason})"


@dataclass
class ReconcileResult:
    ticker: str
    findings: list[Finding] = field(default_factory=list)
    broker_holdings: Optional[int] = None
    broker_avg_price: Optional[float] = None
    deposit_usd: Optional[float] = None
    corrections: list[Correction] = field(default_factory=list)
    derived_T: Optional[float] = None

    @property
    def severity(self) -> str:
        if any(f.severity == Severity.CRITICAL for f in self.findings):
            return Severity.CRITICAL
        if any(f.severity == Severity.WARNING for f in self.findings):
            return Severity.WARNING
        return Severity.OK

    @property
    def should_halt(self) -> bool:
        return self.severity == Severity.CRITICAL

    @property
    def halt_reason(self) -> str:
        crit = [f.message for f in self.findings if f.severity == Severity.CRITICAL]
        return " / ".join(crit)

    def report(self) -> str:
        if self.corrections and not self.findings:
            lines = [f"[{self.ticker}] 장부 자동 교정"]
            lines += [f"  · {c}" for c in self.corrections]
            return "\n".join(lines)
        if not self.findings:
            return f"[{self.ticker}] 장부 일치"
        head = {"critical": "장부 불일치 — 주문 정지",
                "warning": "장부 확인 필요"}.get(self.severity, "장부 점검")
        lines = [f"[{self.ticker}] {head}"]
        lines += [f"  {f}" for f in self.findings]
        if self.corrections:
            lines.append("  장부를 증권사 기준으로 교정했습니다:")
            lines += [f"    · {c}" for c in self.corrections]
        if self.derived_T is not None:
            lines.append(f"  참고: 잔고 역산 T = {self.derived_T:.4f}")
        return "\n".join(lines)


# ================================================================
# 대조
# ================================================================

def reconcile(
    state,
    broker_position: Optional[dict] = None,
    deposit_usd: Optional[float] = None,
    required_cash: float = 0.0,
) -> ReconcileResult:
    """전략 장부와 증권사 잔고를 대조한다.

    Args:
        state: PositionState
        broker_position: KiwoomAPIClient.get_position() 결과. None 이면
            조회 실패이므로 대조를 건너뛰되 경고한다.
        deposit_usd: USD 예수금 (ust21160 의 d0_usd)
        required_cash: 다음 거래일 주문에 필요한 최대 금액
    """
    r = ReconcileResult(ticker=state.ticker)

    # --- 보유수량 ---
    if broker_position is None:
        r.findings.append(Finding(
            Severity.WARNING, "no_broker_data",
            "증권사 잔고를 조회하지 못해 대조를 건너뜁니다."))
    else:
        bh = int(broker_position.get("poss_qty") or broker_position.get("qty") or 0)
        ba = float(broker_position.get("avg_price") or 0)
        r.broker_holdings = bh
        r.broker_avg_price = ba

        if bh != state.holdings:
            if is_within_auto_correct(state.holdings, bh):
                # 부분체결 반올림 수준 — 조용히 맞추고 넘어간다
                r.findings.append(Finding(
                    Severity.WARNING, "holdings_minor",
                    f"보유수량 소차 — 장부 {state.holdings}주 / 증권사 {bh}주. "
                    f"자동 교정합니다."))
            else:
                r.findings.append(Finding(
                    Severity.CRITICAL, "holdings_mismatch",
                    f"보유수량 불일치 — 장부 {state.holdings}주 / 증권사 {bh}주. "
                    f"체결 누락이나 수동 거래가 있었는지 확인하세요."))

        # --- 평단 ---
        if state.holdings > 0 and state.avg_price > 0 and ba > 0:
            drift = abs(ba - state.avg_price) / state.avg_price
            if drift >= AVG_PRICE_CRITICAL:
                r.findings.append(Finding(
                    Severity.CRITICAL, "avg_price_critical",
                    f"평단 괴리 {drift:.1%} — 장부 {state.avg_price:.4f} / "
                    f"증권사 {ba:.4f}. 별지점과 목표매도가가 크게 어긋납니다."))
            elif drift >= AVG_PRICE_TOLERANCE:
                r.findings.append(Finding(
                    Severity.WARNING, "avg_price_drift",
                    f"평단 차이 {drift:.1%} — 장부 {state.avg_price:.4f} / "
                    f"증권사 {ba:.4f}. 증권사 값으로 교정합니다."))

        # --- T 교차검증 ---
        # 잔고에서 역산한 T 와 장부 T 가 크게 벌어지면, 보유수량·평단이
        # 맞더라도 체결 종류를 잘못 해석했을 수 있다.
        if bh > 0 and ba > 0:
            r.derived_T = derive_t(bh, ba, state.principal, state.division)
            drift = abs(r.derived_T - state.T)
            if drift >= T_DRIFT_TOLERANCE:
                r.findings.append(Finding(
                    Severity.WARNING, "t_drift",
                    f"T값 괴리 {drift:.2f}회차 — 장부 {state.T:.4f} / "
                    f"잔고 역산 {r.derived_T:.4f}. 폭락대비 매수가 많았다면 "
                    f"정상이지만, 크게 벌어지면 체결 해석 오류를 의심하세요."))

    # --- 자금 ---
    if deposit_usd is not None:
        r.deposit_usd = deposit_usd
        if required_cash > 0 and deposit_usd < required_cash:
            r.findings.append(Finding(
                Severity.CRITICAL, "insufficient_funds",
                f"예수금 부족 — 필요 ${required_cash:,.2f} / "
                f"보유 ${deposit_usd:,.2f}. 주문이 거부됩니다."))
        elif state.cash > 0 and deposit_usd < state.cash * 0.5:
            r.findings.append(Finding(
                Severity.WARNING, "cash_ledger_drift",
                f"장부 잔금 ${state.cash:,.2f} 대비 실제 예수금 "
                f"${deposit_usd:,.2f} 이 크게 적습니다."))

    return r


# ================================================================
# T값 역산
# ================================================================

def derive_t(holdings: int, avg_price: float, principal: float, division: int) -> float:
    """보유 원가에서 T를 역산한다.

        T ≈ (보유수량 × 평단) / (원금 / 분할수)

    라오어 규칙과 거의 일치한다. 원가 기준으로 보면
        1회 매수    원가 +1회매수금   → T +1
        절반 매수   원가 +0.5회분     → T +0.5
        쿼터매도    원가 ×0.75        → T ×0.75
        지정가매도  원가 ×0.25        → T ×0.25
        리버스 매도 원가 ×0.9 / ×0.95 → 동일

    다만 정확히 같지는 않다.
      - 폭락대비 매수는 원가를 늘리지만 T 를 올리지 않는다
      - 1회매수금이 매일 잔금/(분할-T) 로 변한다 (여기서는 초기값 고정)

    그래서 이 값은 **장부 T 를 대체하지 않고 교차검증에만 쓴다.**
    두 값이 크게 벌어지면 체결 누락이나 계산 오류를 의심해야 한다.
    """
    unit = principal / division
    if unit <= 0 or holdings <= 0 or avg_price <= 0:
        return 0.0
    return (holdings * avg_price) / unit


#: 장부 T 와 역산 T 의 허용 괴리 (회차)
T_DRIFT_TOLERANCE = 2.0


# ================================================================
# 자동 교정
# ================================================================

def auto_correct(state, broker_position: dict) -> list[Correction]:
    """장부를 증권사 기준으로 맞춘다.

    증권사 잔고가 사실이고 전략 장부는 추정이다. 어긋났다면 장부가 틀린
    것이므로 맞춘다. 다만 **T 는 건드리지 않는다.** T 는 체결 순서와
    종류에 따라 결정되므로 잔고만으로는 복원할 수 없다. 보유수량과
    평단만 고치고, 괴리가 크면 호출부가 주문을 멈춘다.

    Returns:
        수행한 교정 목록 (없으면 빈 리스트)
    """
    if not broker_position:
        return []

    out: list[Correction] = []
    bh = int(broker_position.get("poss_qty") or broker_position.get("qty") or 0)
    ba = float(broker_position.get("avg_price") or 0)

    if bh != state.holdings:
        out.append(Correction("보유수량", state.holdings, bh, "증권사 잔고 기준"))
        state.holdings = bh

    if ba > 0 and abs(ba - state.avg_price) > 1e-6:
        out.append(Correction("평단", round(state.avg_price, 4), round(ba, 4),
                              "증권사 매입단가 기준"))
        state.avg_price = ba

    if state.holdings <= 0:
        # 보유가 0이면 사이클이 끝난 것이다
        if state.T != 0.0:
            out.append(Correction("T", state.T, 0.0, "보유 0 → 사이클 종료"))
            state.T = 0.0
        state.avg_price = 0.0

    return out


def is_within_auto_correct(state_qty: int, broker_qty: int) -> bool:
    """조용히 넘어가도 되는 수준의 차이인지.

    부분체결 반올림이나 수수료 반영 시점 차이 정도는 매일 생길 수 있다.
    그 범위를 넘으면 체결이 누락됐다는 뜻이므로 멈춰야 한다.
    """
    diff = abs(state_qty - broker_qty)
    if diff == 0:
        return True
    limit = max(AUTO_CORRECT_MAX_QTY, int(max(state_qty, broker_qty) * AUTO_CORRECT_MAX_PCT))
    return diff <= limit


# ================================================================
# 회로차단기
# ================================================================

class CircuitBreaker:
    """상태에 정지 플래그를 얹고 관리한다.

    정지는 자동으로 풀리지 않는다. 자동 복구를 넣으면 근본 원인이
    남은 채로 다시 주문이 나가기 때문이다.
    """

    @staticmethod
    def halt(state, reason: str) -> bool:
        """정지. 이미 정지 상태면 False."""
        if getattr(state, "halted", False):
            return False
        state.halted = True
        state.halt_reason = reason
        state.halted_at = datetime.now().isoformat(timespec="seconds")
        logger.error("[%s] 주문 정지: %s", state.ticker, reason)
        return True

    @staticmethod
    def release(state) -> bool:
        """해제. 정지 상태가 아니었으면 False."""
        if not getattr(state, "halted", False):
            return False
        state.halted = False
        state.halt_reason = ""
        state.halted_at = ""
        state.eod_failures = 0
        logger.warning("[%s] 주문 정지 해제", state.ticker)
        return True

    @staticmethod
    def is_halted(state) -> bool:
        return bool(getattr(state, "halted", False))

    @staticmethod
    def record_eod_failure(state, error: str) -> bool:
        """EOD 실패 누적. 연속 실패가 한계를 넘으면 정지한다.

        EOD 가 실패하면 T값·평단이 갱신되지 않은 채 다음날 주문이 나간다.
        한 번은 일시 오류일 수 있지만 두 번 연속이면 멈추는 편이 안전하다.
        """
        state.eod_failures = getattr(state, "eod_failures", 0) + 1
        if state.eod_failures >= MAX_EOD_FAILURES:
            return CircuitBreaker.halt(
                state,
                f"EOD 정산 {state.eod_failures}회 연속 실패 — {error}")
        logger.warning("[%s] EOD 실패 %d/%d: %s",
                       state.ticker, state.eod_failures, MAX_EOD_FAILURES, error)
        return False

    @staticmethod
    def record_eod_success(state) -> None:
        state.eod_failures = 0

    @staticmethod
    def status_line(state) -> str:
        if not CircuitBreaker.is_halted(state):
            return ""
        return (f"주문 정지 중 ({getattr(state, 'halted_at', '')})\n"
                f"  사유: {getattr(state, 'halt_reason', '')}\n"
                f"  확인 후 /unhalt {state.ticker} 로 해제하세요.")


def apply_reconcile(state, result: ReconcileResult,
                   broker_position: Optional[dict] = None) -> bool:
    """대조 결과를 상태에 반영한다.

    증권사 잔고가 사실이므로 장부를 항상 맞춘다(비파괴 — 교정 내역은
    result.corrections 에 남는다). 그 위에서 괴리가 컸다면 정지한다.

    교정과 정지를 함께 하는 이유: 장부만 맞추고 계속 매매하면 T 가 틀린
    채로 주문이 나가고, 정지만 하고 장부를 안 맞추면 사람이 /fix 로
    일일이 고쳐야 한다. 둘 다 해야 한다.

    Returns:
        새로 정지됐으면 True
    """
    if broker_position:
        result.corrections = auto_correct(state, broker_position)

    if result.should_halt:
        return CircuitBreaker.halt(state, result.halt_reason)
    return False
