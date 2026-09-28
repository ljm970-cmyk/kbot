"""
================================================================
자금 점검 — 매일 부족분을 채우는 운용 방식

원금은 장부상 금액(예: $20,000)으로 두고, 증권사 계좌에는 그날 주문에
필요한 달러만 매일 채워 넣는 방식을 지원한다. 무한매수법 계산
(1회매수액 = 잔금 ÷ 남은 분할)은 장부 기준으로 돌고, 실제 계좌에는
그날 주문을 받을 만큼만 있으면 된다.

이 방식에서 예수금 부족은 장부 이상이 아니라 "아직 입금 전" 이라는
일시적 상태다. 그래서
  - 회로차단기를 걸지 않는다 (걸면 입금해도 풀리지 않는다)
  - 매도는 그대로 낸다 (돈이 모자란 건 매수 문제다)
  - 매수는 우선순위대로 들어갈 수 있는 만큼만 낸다
      1순위  핵심 매수 — 별지점·평단·처음매수 등 T 를 움직이는 주문
      2순위  폭락대비 — T 불변. 핵심 매수가 모두 들어간 뒤에만,
             가격이 높은 것(체결 가능성이 큰 것)부터
  - 빠진 주문과 필요한 입금액을 알린다
================================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 체결가가 주문가보다 조금 높게 잡히거나 수수료가 붙는 것을 감안한 여유분
SAFETY_MARGIN = 0.005      # 0.5%


@dataclass
class FundingCheck:
    need: float          # 그날 매수 주문을 모두 받으려면 필요한 금액
    available: float     # 실제 매수 가능 금액 (미수불가)
    core_need: float = -1.0   # 핵심 매수(별지점·평단)만의 금액. 모르면 -1

    @property
    def shortfall(self) -> float:
        return max(0.0, self.need - self.available)

    @property
    def core_shortfall(self) -> float:
        base = self.core_need if self.core_need >= 0 else self.need
        return max(0.0, base - self.available)

    @property
    def ok(self) -> bool:
        return self.shortfall <= 0.0

    @property
    def core_ok(self) -> bool:
        return self.core_shortfall <= 0.0

    def topup_text(self) -> str:
        """입금 안내"""
        if self.need <= 0:
            return "  ✅ 매수 주문 없음 — 입금 불필요"
        if self.ok:
            return (f"  필요 ${self.need:,.2f} / 가능 ${self.available:,.2f} — ✅ 충분")

        has_core = 0 <= self.core_need < self.need
        if has_core and self.core_ok:
            # 별지점·평단은 되고 폭락대비만 일부 빠지는 경우
            return (f"  필요 ${self.need:,.2f} / 가능 ${self.available:,.2f}\n"
                    f"  ✅ 별지점·평단 매수 ${self.core_need:,.2f} 는 충분\n"
                    f"  ⚪ 폭락대비는 일부만 들어갑니다 "
                    f"(전부 걸려면 ${self.shortfall:,.2f} 더 입금)")

        lines = [f"  필요 ${self.need:,.2f} / 가능 ${self.available:,.2f}"]
        if has_core:
            lines.append(f"  ⚠️ 별지점·평단 매수에도 ${self.core_shortfall:,.2f} 부족")
            lines.append(f"     최소 ${self.core_shortfall:,.2f} · "
                         f"전체 ${self.shortfall:,.2f} 입금이 필요합니다")
        else:
            lines.append(f"  ⚠️ ${self.shortfall:,.2f} 입금이 필요합니다")
        return "\n".join(lines)


#: T 를 움직이지 않는 보조 매수. 핵심 매수가 모두 들어간 뒤에만 낸다.
AUX_BUY_TAGS = frozenset({"crash_buy"})


def is_core_buy(order) -> bool:
    return order.side == "buy" and order.tag not in AUX_BUY_TAGS


def order_cost(order, fee_rate: float) -> float:
    """주문 한 건이 묶는 금액 (수수료·여유분 포함)"""
    return order.amount * (1 + fee_rate + SAFETY_MARGIN)


@dataclass
class BuyAllocation:
    """가진 달러로 낼 수 있는 매수 주문 배분 결과"""
    placed: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    used: float = 0.0
    core_need: float = 0.0
    full_need: float = 0.0
    available: float = 0.0

    @property
    def core_complete(self) -> bool:
        return not any(is_core_buy(o) for o in self.skipped)

    @property
    def all_placed(self) -> bool:
        return not self.skipped


def allocate_buys(buys, available: float, fee_rate: float) -> BuyAllocation:
    """가진 달러 안에서 우선순위대로 매수 주문을 고른다.

    1순위 핵심 매수(T 를 움직이는 주문)를 가격이 높은 것부터 담는다.
    별지점이 평단보다 위에 있어 체결 가능성이 크다. 한 건이 안 들어가도
    다음 건은 시도한다.

    2순위 폭락대비는 핵심 매수가 **모두** 들어갔을 때만, 가격이 높은
    것부터 끊김 없이 담는다. 핵심이
    빠진 날 폭락대비만 걸리면, 크게 빠져도 T 는 그대로인 채 물량만 늘어
    방법론의 진행 속도가 어긋난다.

    주문 수량은 쪼개지 않는다. 절반 수량으로 바꾸면 T 반영(+1/+0.5)과
    계획의 뜻이 달라진다.
    """
    core = sorted((o for o in buys if is_core_buy(o)),
                  key=lambda o: -(o.price or 0))
    aux = sorted((o for o in buys if o.side == "buy" and not is_core_buy(o)),
                 key=lambda o: -(o.price or 0))

    res = BuyAllocation(
        core_need=round(sum(order_cost(o, fee_rate) for o in core), 2),
        full_need=round(sum(order_cost(o, fee_rate) for o in core + aux), 2),
        available=available,
    )
    left = available
    for o in core:
        c = order_cost(o, fee_rate)
        if c <= left + 1e-9:
            res.placed.append(o)
            left -= c
        else:
            res.skipped.append(o)

    # 폭락대비는 위에서부터 끊김 없이 채운다. 한 건이 안 들어가면 그 아래는
    # 모두 뺀다. 125 는 빼고 100 만 걸면, 종가가 100 아래로 내려간 날 125 도
    # 체결됐어야 할 가격인데 빠져 있어 사다리가 뒤집힌다.
    blocked = not res.core_complete
    for o in aux:
        c = order_cost(o, fee_rate)
        if not blocked and c <= left + 1e-9:
            res.placed.append(o)
            left -= c
        else:
            blocked = True
            res.skipped.append(o)

    res.used = round(available - left, 2)
    return res


def core_buy_need(plan, fee_rate: float) -> float:
    """핵심 매수(별지점·평단 등)만 받으려면 필요한 금액"""
    return round(sum(order_cost(o, fee_rate) for o in plan.buys if is_core_buy(o)), 2)


def buy_need(plan, fee_rate: float) -> float:
    """매수 주문을 모두 받으려면 필요한 금액.

    증권사는 예약주문을 실주문으로 넘길 때 주문가 × 수량으로 매수대금을
    묶는다. LOC 는 실제로는 종가(주문가 이하)에 체결되지만, 접수 시점에는
    주문가 기준으로 확인한다.
    """
    gross = sum(o.amount for o in plan.buys)
    if gross <= 0:
        return 0.0
    return round(gross * (1 + fee_rate + SAFETY_MARGIN), 2)


def account_summary(needs: dict, available: float, core_needs: dict = None) -> str:
    """여러 종목이 같은 달러를 나눠 쓸 때의 입금 안내.

    종목마다 따로 비교하면 각자는 충분해 보여도 합치면 모자랄 수 있다.
    계좌의 매수 가능 금액은 하나이므로 필요 금액을 합산해서 비교한다.

    Args:
        needs: {종목: 다음 거래일 매수 필요 금액}
        available: 계좌의 미수불가 주문가능금액
        core_needs: {종목: 핵심 매수(별지점·평단)만의 금액}. 없으면 구분 안 함
    """
    active = {t: n for t, n in needs.items() if n > 0}
    total = round(sum(active.values()), 2)
    core_total = -1.0
    if core_needs is not None:
        core_total = round(sum(core_needs.get(t, n) for t, n in active.items()), 2)
    chk = FundingCheck(need=total, available=available, core_need=core_total)

    lines = ["💰 오늘 매수 자금"]
    if not active:
        lines.append("  ✅ 매수 주문 없음 — 입금 불필요")
        return "\n".join(lines)
    if len(active) > 1:
        for t, n in active.items():
            lines.append(f"  {t:5} ${n:,.2f}")
        lines.append(f"  합계  ${total:,.2f}")
    lines.append(chk.topup_text())
    return "\n".join(lines)
