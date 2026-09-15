"""
================================================================
누적 통계

일별 정산 아카이브(data/archive/YYYYMM.jsonl)에서 사이클 단위 성과를
집계한다. 상태 파일은 '지금'만 담고 있어서, 며칠째 잘 돌고 있는지
누적으로 얼마를 벌었는지 알 수 없다.

주의 — 실현손익의 두 가지 정의
  이동평균 실현손익 : (매도가 - 이동평균 평단) × 수량
  사이클 손익       : 사이클 종료 시 잔금 - 사이클 시작 잔금

둘은 다르다. 151달러에 산 물량이 있는 상태에서 120달러에 추매하면
이동평균 평단은 143.5달러가 되고, 130달러에 팔면 계좌에는 실현손실로
찍히지만 실제로는 그 물량에서 이익이 났다. 사이클 전체로 보면
현금이 얼마나 늘었는지가 진짜 성적이므로 둘을 함께 보여준다.
================================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Cycle:
    """사이클 하나의 성과"""
    index: int
    start_date: str
    end_date: str
    days: int
    start_cash: float
    end_cash: float
    max_T: float
    max_holdings: int
    reverse_days: int
    realized_sum: float          # 이동평균 기준 실현손익 합

    @property
    def pnl(self) -> float:
        """사이클 손익 = 종료 시 잔금 - 시작 잔금"""
        return self.end_cash - self.start_cash

    @property
    def pnl_pct(self) -> float:
        return (self.pnl / self.start_cash * 100) if self.start_cash else 0.0

    @property
    def is_win(self) -> bool:
        return self.pnl > 0


@dataclass
class Stats:
    ticker: str
    cycles: list[Cycle] = field(default_factory=list)
    open_cycle: Optional[Cycle] = None    # 진행 중 (미완결)
    total_days: int = 0
    reverse_days: int = 0

    @property
    def closed(self) -> int:
        return len(self.cycles)

    @property
    def wins(self) -> int:
        return sum(1 for c in self.cycles if c.is_win)

    @property
    def win_rate(self) -> float:
        return (self.wins / self.closed * 100) if self.closed else 0.0

    @property
    def total_pnl(self) -> float:
        return sum(c.pnl for c in self.cycles)

    @property
    def avg_days(self) -> float:
        return (sum(c.days for c in self.cycles) / self.closed) if self.closed else 0.0

    @property
    def best(self) -> Optional[Cycle]:
        return max(self.cycles, key=lambda c: c.pnl) if self.cycles else None

    @property
    def worst(self) -> Optional[Cycle]:
        return min(self.cycles, key=lambda c: c.pnl) if self.cycles else None


def _months(start: str, end: str) -> list[str]:
    out, cur = [], datetime.strptime(start, "%Y%m")
    last = datetime.strptime(end, "%Y%m")
    while cur <= last:
        out.append(cur.strftime("%Y%m"))
        cur = datetime(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
    return out


def collect(state_mgr, ticker: str, months: Optional[list[str]] = None) -> Stats:
    """아카이브를 훑어 사이클 단위로 묶는다."""
    ticker = ticker.upper()
    if months is None:
        now = datetime.now()
        start = datetime(now.year - 1, now.month, 1).strftime("%Y%m")
        months = _months(start, now.strftime("%Y%m"))

    rows = []
    for m in months:
        rows.extend(state_mgr.read_archive(m, ticker))
    rows.sort(key=lambda r: r.get("trade_date", ""))

    stats = Stats(ticker=ticker)
    idx = 1
    cur: Optional[dict] = None

    for r in rows:
        stats.total_days += 1
        if r.get("mode") == "reverse":
            stats.reverse_days += 1

        if cur is None:
            cur = {"start_date": r["trade_date"], "start_cash": r.get("cash", 0.0),
                   "days": 0, "max_T": 0.0, "max_holdings": 0,
                   "reverse_days": 0, "realized": 0.0}
            # 사이클 시작 잔금은 '직전 종료 시점' 잔금이다.
            # 첫 날 매수가 이미 반영돼 있으므로 되돌려 추정한다.
            cur["start_cash"] = r.get("cash", 0.0) + r.get("holdings", 0) * r.get("avg_price", 0.0)

        cur["days"] += 1
        cur["max_T"] = max(cur["max_T"], r.get("T", 0.0))
        cur["max_holdings"] = max(cur["max_holdings"], r.get("holdings", 0))
        cur["realized"] += r.get("realized_pl", 0.0)
        if r.get("mode") == "reverse":
            cur["reverse_days"] += 1

        if r.get("cycle_closed"):
            stats.cycles.append(Cycle(
                index=idx, start_date=cur["start_date"], end_date=r["trade_date"],
                days=cur["days"], start_cash=cur["start_cash"], end_cash=r.get("cash", 0.0),
                max_T=cur["max_T"], max_holdings=cur["max_holdings"],
                reverse_days=cur["reverse_days"], realized_sum=cur["realized"],
            ))
            idx += 1
            cur = None

    if cur and rows:
        last = rows[-1]
        stats.open_cycle = Cycle(
            index=idx, start_date=cur["start_date"], end_date="진행중",
            days=cur["days"], start_cash=cur["start_cash"], end_cash=last.get("cash", 0.0),
            max_T=cur["max_T"], max_holdings=cur["max_holdings"],
            reverse_days=cur["reverse_days"], realized_sum=cur["realized"],
        )
    return stats


def format_stats(stats: Stats, recent: int = 5) -> str:
    """텔레그램 /stats 출력"""
    if not stats.closed and stats.open_cycle is None:
        return f"[{stats.ticker}] 아직 집계할 이력이 없습니다."

    L = [f"[{stats.ticker}] 누적 성과", ""]
    if stats.closed:
        L += [
            f"  완료 사이클  {stats.closed}회 (승 {stats.wins} / 패 {stats.closed - stats.wins})",
            f"  승률         {stats.win_rate:.0f}%",
            f"  누적 손익    ${stats.total_pnl:,.2f}",
            f"  평균 소요    {stats.avg_days:.1f}거래일",
        ]
        if stats.best:
            L.append(f"  최고         #{stats.best.index} ${stats.best.pnl:+,.2f} "
                     f"({stats.best.pnl_pct:+.2f}%)")
        if stats.worst:
            L.append(f"  최저         #{stats.worst.index} ${stats.worst.pnl:+,.2f} "
                     f"({stats.worst.pnl_pct:+.2f}%)")
    L.append(f"  운용 일수    {stats.total_days}거래일 "
             f"(리버스 {stats.reverse_days}일)")

    if stats.cycles:
        L += ["", f"  최근 사이클 {min(recent, stats.closed)}건", ""]
        L.append("  No   기간              일수   최대T  손익")
        L.append("  " + "-" * 44)
        for c in stats.cycles[-recent:]:
            L.append(f"  #{c.index:<3} {c.start_date[4:]}~{c.end_date[4:]}   "
                     f"{c.days:>3}일  {c.max_T:>5.1f}  ${c.pnl:>+9,.2f}")

    if stats.open_cycle:
        c = stats.open_cycle
        L += ["", f"  진행 중 #{c.index}  {c.start_date[4:]}~  {c.days}거래일",
              f"    최대T {c.max_T:.2f} · 최대보유 {c.max_holdings}주"
              + (f" · 리버스 {c.reverse_days}일" if c.reverse_days else "")]

    return "\n".join(L)


def cycle_pnl_line(state, stats: Stats) -> str:
    """일별 리포트에 붙일 사이클 손익 한 줄.

    이동평균 실현손익과 사이클 손익은 다르다. 이동평균 평단 때문에
    실제로는 이익인 매도가 계좌에 손실로 찍히는 경우가 있어서,
    현금이 실제로 얼마나 늘었는지를 함께 보여준다.
    """
    c = stats.open_cycle
    if c is None:
        return ""
    unreal = state.holdings * state.avg_price
    return (f"  사이클 #{c.index} {c.days}거래일차 · "
            f"투입 ${unreal:,.2f} · 누적손익 ${stats.total_pnl:+,.2f}")
