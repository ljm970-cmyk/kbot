"""
================================================================
사이클 진입 RSI 필터 백테스트

질문: 새 사이클을 시작할 때 "RSI(14) 가 N 이하일 때만 진입" 조건을
      걸면 무한매수법 성과가 좋아지나? 좋아진다면 N 은 얼마가 좋나?

봇의 실제 계획·정산 코드(별지점·T값·쿼터매도·리버스·수수료)를 그대로
쓴다. 체결만 일봉으로 흉내 낸다.

  LOC 매수        종가 ≤ 주문가  → 종가에 체결
  LOC 매도(쿼터)   종가 ≥ 주문가  → 종가에 체결
  지정가매도(목표) 고가 ≥ 주문가  → 주문가에 체결 (시가가 더 높으면 시가)
  MOC 매도        종가에 체결

RSI 필터는 **보유 0 (사이클 사이)** 일 때만 적용한다. 전일 RSI(14) 가
기준 이하가 될 때까지 새 사이클 진입(처음매수)을 미룬다. 진행 중인
사이클에는 관여하지 않는다.

실행 (VM):
    cd ~/kbot
    venv/bin/pip install -q yfinance
    venv/bin/python backtest/rsi_entry.py

옵션:
    --tickers SOXL TQQQ     종목
    --divisions 40          분할 (20 도 보려면 --divisions 40 20)
    --null 30               우연 검증용 섞은 가격 개수 (0 이면 생략)
    --start 2012-01-01      시작일
    --principal 20000       원금
    --source yahoo|kiwoom   가격 출처 (기본: yahoo, 실패 시 kiwoom)
================================================================
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import math
import random
import sqlite3
import sys
from contextlib import contextmanager
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.disable(logging.WARNING)      # 정산 로그가 수만 줄 찍히지 않게

from core.order_registry import _SCHEMA, OrderRegistry  # noqa: E402
from eod.calculator import EndOfDayCalculator, FillEvent  # noqa: E402
from kiwoom.constants import TradeType  # noqa: E402
from modes.base_mode import MarketSnapshot, PositionState  # noqa: E402
from modes.normal_mode import NormalMode  # noqa: E402
from modes.reverse_mode import ReverseMode  # noqa: E402

CACHE = ROOT / "data" / "backtest"
THRESHOLDS = [None, 75, 70, 65, 60, 55, 50, 45, 40, 35, 30]
FEE = 0.0007


# ================================================================
# 가격 데이터
# ================================================================

@dataclass
class Bar:
    d: date
    o: float
    h: float
    l: float
    c: float


def _save(ticker: str, bars: list[Bar]) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    with open(CACHE / f"{ticker}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close"])
        for b in bars:
            w.writerow([b.d.isoformat(), b.o, b.h, b.l, b.c])


def _load_cache(ticker: str) -> list[Bar] | None:
    p = CACHE / f"{ticker}.csv"
    if not p.exists():
        return None
    if time.time() - p.stat().st_mtime > 3 * 86400:
        return None
    with open(p) as f:
        r = csv.DictReader(f)
        return [Bar(date.fromisoformat(x["date"]), float(x["open"]), float(x["high"]),
                    float(x["low"]), float(x["close"])) for x in r]


def _from_yahoo(ticker: str) -> list[Bar]:
    import yfinance as yf
    df = yf.download(ticker, start="2010-01-01", auto_adjust=True,
                     progress=False, multi_level_index=False)
    bars = []
    for idx, row in df.iterrows():
        vals = [float(row[k]) for k in ("Open", "High", "Low", "Close")]
        if any(math.isnan(v) or v <= 0 for v in vals):
            continue
        bars.append(Bar(idx.date(), *vals))
    return bars


def _from_kiwoom(ticker: str) -> list[Bar]:
    from config.settings import ConfigLoader
    from kiwoom.api_client import KiwoomAPIClient, to_float
    from kiwoom.constants import exchange_of

    async def fetch():
        async with KiwoomAPIClient(ConfigLoader.load().kiwoom) as api:
            body = {"stex_tp": exchange_of(ticker), "stk_cd": ticker,
                    "strt_dt": "20100101", "upd_stkpc_tp": "1", "exrt_appl_tp": "0"}
            return await api.request_all("usa06012", api.PATH_CHART, body)

    rows = asyncio.run(fetch())
    bars = {}
    for r in rows:
        dt = str(r.get("dt", ""))
        if len(dt) != 8:
            continue
        vals = [abs(to_float(r.get(k))) for k in ("open_pric", "high_pric", "low_pric", "cur_prc")]
        if min(vals) <= 0:
            continue
        bars[dt] = Bar(datetime.strptime(dt, "%Y%m%d").date(), *vals)
    return [bars[k] for k in sorted(bars)]


def load_prices(ticker: str, source: str) -> tuple[list[Bar], str]:
    cached = _load_cache(ticker)
    if cached:
        return cached, "cache"
    order = ["yahoo", "kiwoom"] if source == "yahoo" else ["kiwoom", "yahoo"]
    last = None
    for src in order:
        try:
            bars = _from_yahoo(ticker) if src == "yahoo" else _from_kiwoom(ticker)
            if len(bars) > 250:
                _save(ticker, bars)
                return bars, src
            last = f"{src}: {len(bars)}일뿐"
        except Exception as e:
            last = f"{src}: {e}"
    raise SystemExit(f"[{ticker}] 가격을 받지 못했습니다 — {last}")


# ================================================================
# RSI (Wilder, 14)
# ================================================================

def rsi_series(closes: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(ch, 0)) / n
        al = (al * (n - 1) + max(-ch, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


# ================================================================
# 시뮬레이션
# ================================================================

class MemoryRegistry(OrderRegistry):
    """백테스트 전용 원장 — 메모리 안에만 둔다.

    실제 원장은 매 작업마다 디스크에 commit 해서, 수천 거래일을 돌리면
    시간 대부분이 디스크 동기화에 쓰인다. 로직은 그대로 쓰고 저장만 바꾼다.
    """

    def __init__(self):
        self.db_path = ":memory:"
        self._mem = sqlite3.connect(":memory:")
        self._mem.row_factory = sqlite3.Row
        self._mem.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        try:
            yield self._mem
        except Exception:
            self._mem.rollback()
            raise

@dataclass
class Result:
    ticker: str
    division: int
    limit: float | None
    start: date
    end: date
    principal: float
    final: float = 0.0
    cagr: float = 0.0
    mdd: float = 0.0
    cycles: int = 0
    wins: int = 0
    cycle_days: list = field(default_factory=list)
    invested_days: int = 0
    waiting_days: int = 0
    reverse_days: int = 0
    total_days: int = 0
    errors: int = 0

    @property
    def label(self) -> str:
        return "필터 없음" if self.limit is None else f"RSI≤{self.limit:g}"


def _fills_for(plan, bar: Bar, ticker: str, day: str) -> list[FillEvent]:
    fills = []
    for k, o in enumerate(plan.orders):
        px = None
        if o.trade_type == TradeType.MOC:
            px = bar.c
        elif o.trade_type == TradeType.LIMIT:
            if o.side == "sell" and bar.h >= o.price:
                px = max(bar.o, o.price)
            elif o.side == "buy" and bar.l <= o.price:
                px = min(bar.o, o.price)
        else:                                   # LOC
            if o.side == "buy" and bar.c <= o.price:
                px = bar.c
            elif o.side == "sell" and bar.c >= o.price:
                px = bar.c
        if px is None:
            continue
        fills.append(FillEvent(ticker, o.side, o.qty, round(px, 4),
                               order_price=o.price, ord_no=f"{day}-{k}",
                               fill_no="1", trade_type=o.trade_type))
    return fills


def simulate(ticker: str, division: int, bars: list[Bar], rsi: list,
             limit, i0: int, i1: int, principal: float) -> Result:
    reg = MemoryRegistry()
    eod = EndOfDayCalculator(reg)
    st = PositionState(ticker=ticker, division=division, principal=principal,
                       fee_rate=FEE, cash=principal)

    res = Result(ticker, division, limit, bars[i0].d, bars[i1 - 1].d, principal)
    peak = principal
    cycle_start = None

    for i in range(i0, i1):
        bar, prev = bars[i], bars[i - 1]
        day = bar.d.strftime("%Y%m%d")
        res.total_days += 1

        idle = st.holdings <= 0 and st.T <= 0
        if idle and limit is not None and (rsi[i - 1] is None or rsi[i - 1] > limit):
            res.waiting_days += 1
        else:
            if idle and cycle_start is None:
                cycle_start = i
            closes = [b.c for b in bars[max(0, i - 5):i]][::-1]
            mk = MarketSnapshot(prev_close=prev.c, current_price=prev.c, recent_closes=closes)
            try:
                mode = ReverseMode(st) if st.mode == "reverse" else NormalMode(st)
                plan = mode.plan(mk)
                # 실제 봇처럼 주문번호를 붙여둔다. 안 붙이면 체결 매칭이
                # 원장 전체를 훑어서 기간이 길수록 느려진다.
                for k, rid in enumerate(reg.record_plan(day, ticker, plan.orders)):
                    reg.attach_ord_no(rid, f"{day}-{k}")
                fills = _fills_for(plan, bar, ticker, day)
                nxt = MarketSnapshot(prev_close=bar.c, current_price=bar.c,
                                     recent_closes=[b.c for b in bars[max(0, i - 4):i + 1]][::-1])
                r = eod.run(st, day, fills, bar.c, nxt)
                if r.cycle_closed and cycle_start is not None:
                    res.cycles += 1
                    res.cycle_days.append(i - cycle_start + 1)
                    cycle_start = None
            except Exception:
                res.errors += 1

        if st.holdings > 0:
            res.invested_days += 1
        if st.mode == "reverse":
            res.reverse_days += 1

        equity = st.cash + st.holdings * bar.c
        peak = max(peak, equity)
        res.mdd = max(res.mdd, (peak - equity) / peak if peak > 0 else 0)

    res.final = st.cash + st.holdings * bars[i1 - 1].c
    years = max((res.end - res.start).days / 365.25, 1e-9)
    res.cagr = (res.final / principal) ** (1 / years) - 1 if res.final > 0 else -1.0
    return res


def buy_and_hold(bars: list[Bar], i0: int, i1: int) -> tuple[float, float]:
    peak, mdd = bars[i0].c, 0.0
    for b in bars[i0:i1]:
        peak = max(peak, b.c)
        mdd = max(mdd, (peak - b.c) / peak)
    years = max((bars[i1 - 1].d - bars[i0].d).days / 365.25, 1e-9)
    return (bars[i1 - 1].c / bars[i0].c) ** (1 / years) - 1, mdd


# ================================================================
# 보고
# ================================================================

def _row(r: Result, base: Result | None) -> str:
    avg_days = sum(r.cycle_days) / len(r.cycle_days) if r.cycle_days else 0
    diff = ""
    if base is not None and r is not base:
        diff = f"{(r.cagr - base.cagr) * 100:+6.1f}%p"
    wait = r.waiting_days / r.total_days * 100 if r.total_days else 0
    rev = r.reverse_days / r.total_days * 100 if r.total_days else 0
    err = f" ⚠{r.errors}" if r.errors else ""
    return (f"  {r.label:9} CAGR {r.cagr * 100:6.1f}% {diff:>8}  "
            f"MDD {r.mdd * 100:5.1f}%  사이클 {r.cycles:3d}회 "
            f"평균 {avg_days:5.1f}일  대기 {wait:4.1f}%  리버스 {rev:4.1f}%{err}")


def run_block(ticker, division, bars, rsi, i0, i1, principal, title) -> list[Result]:
    bh_cagr, bh_mdd = buy_and_hold(bars, i0, i1)
    print(f"\n■ {ticker} {division}분할 · {title}  "
          f"({bars[i0].d} ~ {bars[i1 - 1].d}, {i1 - i0}거래일)")
    print(f"  단순보유  CAGR {bh_cagr * 100:6.1f}%            MDD {bh_mdd * 100:5.1f}%")
    results = []
    for lim in THRESHOLDS:
        t0 = time.time()
        r = simulate(ticker, division, bars, rsi, lim, i0, i1, principal)
        results.append(r)
        print(_row(r, results[0]), f"({time.time() - t0:.0f}s)", flush=True)
    return results


def rolling(ticker, division, bars, rsi, i0, i1, principal,
            window: int = 756, step: int = 126) -> dict:
    """3년(756거래일) 구간을 6개월(126거래일)씩 밀며 반복한다.

    무한매수법은 언제 시작했느냐에 따라 결과가 크게 갈린다. 전체 기간
    한 번, 또는 전반·후반 두 번만 비교하면 우연을 실력으로 착각하기 쉽다
    (무작위 가격으로 돌려도 '모든 기간 승리' 기준이 여러 개 나왔다).
    여러 시작점에서 몇 번 이겼는지와 중앙값·최악을 본다.
    """
    starts = list(range(i0, i1 - window + 1, step))
    base = {s0: simulate(ticker, division, bars, rsi, None, s0, s0 + window, principal)
            for s0 in starts}
    out = {}
    for lim in THRESHOLDS[1:]:
        diffs, waits = [], []
        for s0 in starts:
            r = simulate(ticker, division, bars, rsi, lim, s0, s0 + window, principal)
            diffs.append((r.cagr - base[s0].cagr) * 100)
            waits.append(r.waiting_days / max(r.total_days, 1) * 100)
        diffs.sort()
        n = len(diffs)
        out[lim] = {
            "median": diffs[n // 2] if n % 2 else (diffs[n // 2 - 1] + diffs[n // 2]) / 2,
            "win": sum(d > 0 for d in diffs) / n * 100,
            "worst": diffs[0],
            "best": diffs[-1],
            "wait": sum(waits) / n,
            "n": n,
        }
    base_cagrs = sorted(b.cagr * 100 for b in base.values())
    return {"rows": out, "n": len(starts),
            "base_median": base_cagrs[len(base_cagrs) // 2] if base_cagrs else 0.0}


def print_rolling(ticker, division, roll) -> str | None:
    print(f"\n■ {ticker} {division}분할 · 3년 구간 {roll['n']}개 (6개월 간격)"
          f" — 필터 없음 중앙 CAGR {roll['base_median']:.1f}%")
    print("  기준      중앙값 차이  승률   최악     최고    평균 대기")
    best, best_med = None, 0.0
    for lim, r in roll["rows"].items():
        mark = ""
        if r["median"] > 0 and r["win"] >= 60:
            mark = " ◀"
            if r["median"] > best_med:
                best, best_med = lim, r["median"]
        print(f"  RSI≤{lim:<4g}  {r['median']:+8.1f}%p  {r['win']:4.0f}%  "
              f"{r['worst']:+6.1f}p  {r['best']:+6.1f}p   {r['wait']:4.1f}%{mark}")
    return best


def _best_edge(ticker, division, bars, rsi, i0, i1, principal) -> tuple[float, float | None]:
    """전체 기간에서 필터 없음 대비 가장 좋은 기준의 CAGR 차이(%p)"""
    base = simulate(ticker, division, bars, rsi, None, i0, i1, principal).cagr
    best, best_lim = -1e9, None
    for lim in THRESHOLDS[1:]:
        d = (simulate(ticker, division, bars, rsi, lim, i0, i1, principal).cagr - base) * 100
        if d > best:
            best, best_lim = d, lim
    return best, best_lim


def shuffled(bars: list[Bar], i0: int, i1: int, rng: random.Random) -> list[Bar]:
    """하루 단위 수익률·캔들 모양의 순서만 섞은 가격.

    변동성·추세·캔들 모양은 그대로이고, 며칠에 걸친 흐름(과매도 후 반등
    같은 패턴)만 사라진다. RSI 가 이용할 수 있는 정보가 없는 가격이다.
    """
    days = []
    for i in range(max(i0, 1), i1):
        pc, b = bars[i - 1].c, bars[i]
        days.append((b.c / pc, b.o / pc, b.h / max(b.o, b.c), b.l / min(b.o, b.c)))
    rng.shuffle(days)
    out = list(bars[:max(i0, 1)])
    c = out[-1].c
    for k, (rc, ro, rh, rl) in enumerate(days):
        o, nc = c * ro, c * rc
        out.append(Bar(bars[max(i0, 1) + k].d, o, max(o, nc) * rh, min(o, nc) * rl, nc))
        c = nc
    return out


def null_test(ticker, division, bars, i0, i1, principal, k: int, seed: int = 1) -> list[float]:
    rng = random.Random(seed)
    out = []
    for n in range(k):
        sb = shuffled(bars, i0, i1, rng)
        edge, _ = _best_edge(ticker, division, sb, rsi_series([b.c for b in sb]), i0, i1, principal)
        out.append(edge)
        print(f"\r  무작위 가격 {n + 1}/{k} ...", end="", flush=True)
    print()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="+", default=["SOXL", "TQQQ"])
    ap.add_argument("--divisions", nargs="+", type=int, default=[40])
    ap.add_argument("--null", type=int, default=30,
                    help="무작위로 섞은 가격 개수 (0 이면 생략)")
    ap.add_argument("--start", default="2012-01-01")
    ap.add_argument("--principal", type=float, default=20000.0)
    ap.add_argument("--source", default="yahoo", choices=["yahoo", "kiwoom"])
    args = ap.parse_args()
    start = date.fromisoformat(args.start)

    verdicts = []
    for ticker in args.tickers:
        bars, src = load_prices(ticker, args.source)
        rsi = rsi_series([b.c for b in bars])
        i0 = next(i for i, b in enumerate(bars) if b.d >= start and i >= 20)
        i1 = len(bars)
        print(f"\n[{ticker}] 가격 {len(bars)}일 ({bars[0].d} ~ {bars[-1].d}, 출처 {src})")

        for division in args.divisions:
            run_block(ticker, division, bars, rsi, i0, i1, args.principal, "전체 기간")
            roll = rolling(ticker, division, bars, rsi, i0, i1, args.principal)
            best = print_rolling(ticker, division, roll)

            real_edge, real_lim = _best_edge(ticker, division, bars, rsi, i0, i1, args.principal)
            pval = None
            if args.null > 0:
                print(f"\n■ {ticker} {division}분할 · 우연 검증 — 수익률 순서를 섞은 가격 {args.null}개")
                null = null_test(ticker, division, bars, i0, i1, args.principal, args.null)
                pval = sum(e >= real_edge for e in null) / len(null) * 100
                ns = sorted(null)
                print(f"  실제 가격   최선 RSI≤{real_lim:g}  {real_edge:+.1f}%p")
                print(f"  섞은 가격   최선 기준 성과  중앙 {ns[len(ns) // 2]:+.1f}%p · "
                      f"상위10% {ns[int(len(ns) * 0.9)]:+.1f}%p · 최고 {ns[-1]:+.1f}%p")
                print(f"  → 섞은 가격에서도 실제만큼 나온 비율 {pval:.0f}%"
                      + ("  (우연으로 설명 안 됨)" if pval <= 10 else "  (우연으로도 흔히 나옴)"))
            verdicts.append((ticker, division, best, roll["rows"].get(best), pval))

    print("\n" + "=" * 70)
    print("결론")
    print("  채택 조건: 3년 구간 중앙값 +, 승률 60% 이상, 섞은 가격 비율 10% 이하")
    print("=" * 70)
    for ticker, division, best, r, pval in verdicts:
        ptxt = "" if pval is None else f", 섞은 가격 {pval:.0f}%"
        if best is None:
            print(f"  {ticker} {division}분할  필터 없음 유지 — 꾸준히 나은 기준 없음{ptxt}")
        elif pval is not None and pval > 10:
            print(f"  {ticker} {division}분할  필터 없음 유지 — RSI≤{best:g} 가 나아 보이지만 "
                  f"우연으로도 흔히 나오는 수준{ptxt}")
        else:
            print(f"  {ticker} {division}분할  RSI≤{best:g} 채택 후보 "
                  f"(중앙 {r['median']:+.1f}%p, 승률 {r['win']:.0f}%, 최악 {r['worst']:+.1f}p{ptxt})")


if __name__ == "__main__":
    main()
