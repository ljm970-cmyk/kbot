"""
================================================================
스케줄러 엔진

기존 구현의 문제
  - 모든 작업이 TODO 로 비어 있어 실제로 아무것도 실행되지 않았다
  - SchedulerEngine 이 config 만 받아 kiwoom·state·eod 에 접근할 수 없었다
  - 17:29/18:29 에 깨어나 asyncio.sleep 으로 30분을 기다리는 구조였고,
    그 안에서 타임존 없는 datetime.now() 를 써서 서버가 UTC 면 어긋났다
  - 미국 휴장일·주말·조기폐장 판정이 전혀 없었다

여기서는 고정 cron 대신 **매일 한 번 그날의 일정을 계산해 일회성 작업으로
등록**한다. 서머타임·조기폐장이 자동으로 반영되고, 시각 계산이 한 곳에
모인다.

  매일 12:00 KST   plan_day()  — 다음 세션의 작업들을 등록
    ├─ 프리장 시작       지정가매도 접수 (방법론 6)
    ├─ 프리장 +15분      LOC/MOC 예약 접수
    ├─ 접수 +20분        예약주문 검증 (ust21205)
    └─ 마감 +30분        EOD 정산 → 다음날 계획

  1시간마다        토큰 갱신
  30분마다         헬스체크
================================================================
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Awaitable, Callable, Optional, Protocol

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from core.market_calendar import (
    KST,
    DaySchedule,
    describe,
    is_early_close,
    market_close_kst,
    premarket_open_kst,
    regular_open_kst,
    upcoming_session,
)
from core.order_registry import OrderRegistry
from core.reconciler import CircuitBreaker, apply_reconcile, reconcile
from core.state_manager import StateManager
from core.t_calculator import FillKind
from eod.calculator import EndOfDayCalculator, FillEvent, fills_from_api_rows, merge_fill_sources
from eod.report import daily_report
from eod.stats import collect as collect_stats
from kiwoom.api_client import KiwoomAPIClient, KiwoomAPIError, KiwoomOrderUncertainError
from kiwoom.constants import TradeType, exchange_of
from modes.base_mode import MarketSnapshot, OrderPlan, PlannedOrder, SubmitWindow
from modes.normal_mode import NormalMode
from modes.reverse_mode import ReverseMode

logger = logging.getLogger("kbot.scheduler")


def _core_or_full(st) -> float:
    """핵심 매수 금액. 아직 계산 전(-1)이면 전체 금액으로 대신한다."""
    core = getattr(st, "next_core_need", -1.0)
    return core if core >= 0 else getattr(st, "next_buy_need", 0.0)


class Notifier(Protocol):
    """텔레그램 등 알림 채널"""
    async def send(self, text: str) -> None: ...


@dataclass
class _NullNotifier:
    async def send(self, text: str) -> None:
        logger.info("[알림] %s", text)


# ================================================================
# 엔진
# ================================================================

class SchedulerEngine:
    """매매 스케줄러"""

    PLAN_HOUR = 12          # 매일 이 시각(KST)에 그날 일정을 계산한다
    #: 아침 요약 발송 시각.
    #: EOD 리포트는 05:30 에 오는데 대개 자고 있어 읽히지 않는다.
    BRIEF_HOUR = 8
    #: 주간 장부 백업. 금요일 밤 정산(토 05:30)이 끝난 뒤.
    BACKUP_CRON = {"day_of_week": "sat", "hour": 9, "minute": 10}
    #: 주간 성과 요약. 백업 바로 앞.
    WEEKLY_CRON = {"day_of_week": "sat", "hour": 9, "minute": 5}
    #: 토큰 갱신 주기(시간). minute 필드는 0~59 라 "*/60" 은 쓸 수 없다.
    TOKEN_REFRESH_HOURS = 1
    #: 헬스체크 주기(분)
    HEALTH_CHECK_MIN = 30

    def __init__(
        self,
        config,
        kiwoom: KiwoomAPIClient,
        state_mgr: StateManager,
        registry: OrderRegistry,
        eod: EndOfDayCalculator,
        notifier: Optional[Notifier] = None,
        fill_source: Optional[Callable[[str, str], Awaitable[list[FillEvent]]]] = None,
    ):
        """
        Args:
            fill_source: WebSocket 수신 체결을 돌려주는 콜백.
                (trade_date, ticker) -> list[FillEvent]. 없으면 API 조회만 쓴다.
        """
        self.config = config
        self.kiwoom = kiwoom
        self.state_mgr = state_mgr
        self.registry = registry
        self.eod = eod
        self.notifier = notifier or _NullNotifier()
        self.fill_source = fill_source
        self.scheduler = AsyncIOScheduler(timezone=KST)
        self._current: Optional[DaySchedule] = None

    # ------------------------------------------------------------
    # 수명주기
    # ------------------------------------------------------------

    def start(self) -> None:
        self.scheduler.add_job(
            self.plan_day, CronTrigger(hour=self.PLAN_HOUR, minute=0, timezone=KST),
            id="plan_day", replace_existing=True, misfire_grace_time=3600,
        )
        self.scheduler.add_job(
            self._refresh_token,
            CronTrigger(hour=f"*/{self.TOKEN_REFRESH_HOURS}", minute=0, timezone=KST),
            id="refresh_token", replace_existing=True,
        )
        self.scheduler.add_job(
            self._health_check, CronTrigger(minute=f"*/{self.HEALTH_CHECK_MIN}", timezone=KST),
            id="health_check", replace_existing=True,
        )
        self.scheduler.add_job(
            self._morning_brief,
            CronTrigger(hour=self.BRIEF_HOUR, minute=0, timezone=KST),
            id="morning_brief", replace_existing=True, misfire_grace_time=3600,
        )
        self.scheduler.add_job(
            self.send_weekly, CronTrigger(timezone=KST, **self.WEEKLY_CRON),
            id="weekly_report", replace_existing=True, misfire_grace_time=6 * 3600,
        )
        self.scheduler.add_job(
            self.send_backup, CronTrigger(timezone=KST, **self.BACKUP_CRON),
            id="weekly_backup", replace_existing=True, misfire_grace_time=6 * 3600,
            kwargs={"reason": "주간"},
        )
        # 생존 신호 — 봇과 따로 도는 감시(ops/watchdog.py)가 읽는다
        self.scheduler.add_job(
            self._heartbeat, CronTrigger(minute="*", timezone=KST),
            id="heartbeat", replace_existing=True, misfire_grace_time=30,
        )
        self.scheduler.start()
        self._write_heartbeat()
        logger.info("스케줄러 시작 (KST)")
        # 기동 직후 한 번 계산해 둔다
        asyncio.create_task(self.plan_day())

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def status(self) -> str:
        lines = ["스케줄러 상태"]
        if self._current:
            lines.append(self._current.describe())
        else:
            lines.append("  예정된 세션 없음")
        for job in self.scheduler.get_jobs():
            nxt = job.next_run_time
            lines.append(f"  {job.id:16} {nxt:%m/%d %H:%M} KST" if nxt else f"  {job.id:16} 대기")
        return "\n".join(lines)

    # ------------------------------------------------------------
    # 일정 등록
    # ------------------------------------------------------------

    async def plan_day(self) -> None:
        """다음 거래일 작업을 일회성으로 등록한다.

        서머타임·조기폐장이 여기서 한 번에 반영되므로, 개별 작업은
        시각 계산을 신경 쓰지 않는다.
        """
        try:
            session = upcoming_session()
        except RuntimeError as e:
            await self.notifier.send(f"거래일 계산 실패: {e}")
            return

        sched = DaySchedule.build(session)
        if self._current and self._current.session == session:
            return          # 이미 등록됨
        self._current = sched

        now = datetime.now(KST)
        jobs = [
            ("submit_target_sell", sched.premarket, self.submit_target_sells),
            ("submit_loc_orders", sched.submit_loc, self.submit_loc_orders),
            ("verify_reserved", sched.verify, self.verify_reserved),
            ("verify_after_open", sched.verify_open, self.verify_reserved),
            ("run_eod", sched.eod, self.run_eod),
        ]
        # 주문 1시간 전 자금 점검 — 매일 부족분을 채우는 운용용
        from datetime import timedelta as _td
        # 프리장에서 증권사가 거부한 핵심 LOC 매수를 정규장에서 되살린다.
        # 1차 같은 가격 재접수 → 2차 현재가×1.15 대체(방법론 8) → 3차 알림.
        # 거부는 접수 몇 초 뒤에 오므로 단계마다 15분 간격으로 확인한다.
        # 18:02 에 돈이 모자라 빠진 매수를 자동으로 다시 시도한다.
        # 원화주문 몫이 프리장 시작 직후엔 덜 잡히는 일이 있었다 (2026-11-05: 18:02 약 $644,
        # 20:07 에는 $1,000 이상 더 잡힘). 이미 낸 주문·사용자가 취소한 주문은 다시 내지 않는다.
        jobs += [
            ("loc_retry_1", sched.submit_loc + _td(minutes=38), self._loc_retry_1),
            ("loc_retry_2", sched.submit_loc + _td(minutes=88), self._loc_retry_2),
            ("loc_retry_3", regular_open_kst(session) - _td(minutes=90), self._loc_retry_final),
        ]
        # (functools.partial 대신 진짜 async 메서드를 넘긴다 — 스케줄러가 코루틴으로 알아보게)
        for i, (mins, fn) in enumerate(((1, self._recover_first), (16, self._recover_second),
                                        (31, self._recover_final)), start=1):
            jobs.append((f"recover_rejected_{i}", sched.verify_open + _td(minutes=mins), fn))
        jobs.append(("funding_reminder", sched.premarket - _td(hours=1),
                     self._funding_reminder))

        for job_id, when, fn in jobs:
            if when <= now:
                logger.info("%s 시각(%s)이 이미 지나 건너뜁니다", job_id, when)
                continue
            self.scheduler.add_job(
                fn, DateTrigger(run_date=when, timezone=KST),
                id=job_id, replace_existing=True, misfire_grace_time=1800,
                kwargs={"session": session},
            )

        logger.info("일정 등록\n%s", sched.describe())
        await self.notifier.send(sched.describe())

    # ------------------------------------------------------------
    # 주문 접수
    # ------------------------------------------------------------

    async def submit_target_sells(self, session: date) -> None:
        """프리장 시작에 3/4 목표 지정가매도만 먼저 건다 (방법론 6).

        지정가매도는 프리장~본장~애프터장까지 효력이 있으므로
        가장 이른 시각에 걸어 체결 기회를 최대한 확보한다.
        """
        await self._submit(session, SubmitWindow.PRE_MARKET, "지정가매도")

    async def submit_loc_orders(self, session: date) -> None:
        """LOC/MOC 예약주문 접수"""
        await self._submit(session, SubmitWindow.REGULAR, "LOC 접수")

    async def _loc_retry_1(self, session: date) -> None:
        await self._submit(session, SubmitWindow.REGULAR, "LOC 자동 재시도", auto_retry=True)

    async def _loc_retry_2(self, session: date) -> None:
        await self._submit(session, SubmitWindow.REGULAR, "LOC 자동 재시도", auto_retry=True)

    async def _loc_retry_final(self, session: date) -> None:
        await self._submit(session, SubmitWindow.REGULAR, "LOC 자동 재시도", auto_retry=True,
                           final_retry=True)

    async def _submit(self, session: date, window: str, label: str,
                      auto_retry: bool = False, final_retry: bool = False) -> None:
        trade_date = session.strftime("%Y%m%d")
        tickers = self.state_mgr.list_tickers()

        # 두 종목 이상이면 달러를 나눠 쓴다.
        #
        # 종목을 차례로 접수하므로 앞 종목이 폭락대비까지 걸면 뒤 종목의
        # 별지점·평단 매수가 빠질 수 있다. 뒤 종목들의 핵심 매수 몫을
        # 남겨두고, 앞 종목이 실제로 묶은 만큼 예산에서 뺀다.
        budget = None
        if window == SubmitWindow.REGULAR and len(tickers) > 1:
            budget = await self._account_available()

        for i, ticker in enumerate(tickers):
            try:
                reserve = 0.0
                if budget is not None:
                    try:
                        reserve = self._core_reserve(tickers[i + 1:], trade_date)
                    except Exception:
                        logger.exception("다른 종목 핵심 매수 몫 계산 실패 — 예약 없이 진행")
                # 첫 종목은 아직 쓴 돈이 없으니 증권사 금액 그대로 쓴다.
                # (예산 조회 기준가가 달라 한 주 차이로 핵심 매수가 빠지는 일을 막는다)
                spent = await self._submit_ticker(
                    ticker, trade_date, window, label, aux_reserve=reserve,
                    budget=budget if (budget is not None and i > 0) else None,
                    auto_retry=auto_retry, final_retry=final_retry)
                if budget is not None:
                    budget -= spent or 0.0
            except Exception as e:
                logger.exception("%s %s 접수 실패", ticker, label)
                await self.notifier.send(f"⚠ [{ticker}] {label} 접수 실패: {e}")

    #: 하루에 여러 건 나가는 태그 — 태그가 아니라 가격으로 중복을 본다
    MULTI_TAGS = frozenset({FillKind.CRASH_BUY})
    #: 하루치 본 매수의 여러 모양. 이 중 무엇이든 그날 이미 냈으면 다른 모양으로 또 내지 않는다
    MAIN_BUY_GROUP = frozenset({FillKind.STAR_BUY, FillKind.AVG_BUY, FillKind.HALF_BUY,
                                FillKind.ENTRY_BUY, FillKind.GUARD_BUY, FillKind.MERGED_BUY})
    #: 원래 모양(별지점·평단·후반전·처음매수)을 대신하는 단건
    MAIN_REPLACEMENTS = frozenset({FillKind.GUARD_BUY, FillKind.MERGED_BUY})

    def _drop_already_submitted(self, ticker: str, trade_date: str, orders: list,
                                include_cancelled: bool = False):
        """이미 접수한 주문을 뺀다. (남은 주문, 중복으로 본 태그들)

        대부분의 태그는 하루 한 건이라 태그로 거른다. 폭락대비는 한 번에
        5건이 나가므로 태그로 거르면, 돈이 모자라 1건만 들어간 날 /run loc
        로 나머지를 다시 낼 수 없었다 (실제 2026-11-02). 폭락대비는
        같은 가격이 이미 있으면 빼고, 전체 건수가 계획보다 많아지지 않게 한다.
        """
        single = [o.tag for o in orders if o.tag not in self.MULTI_TAGS]
        dup_single = set(self.registry.already_submitted(trade_date, ticker, single,
                                                         include_cancelled=include_cancelled))

        # 그날의 본 매수는 시세에 따라 모양이 바뀐다 — 별지점+평단 / 후반전 단건 /
        # 가격제한 대체 단건 / 합친 단건. 같은 하루치 매수인데 태그가 달라, 다시 계산한
        # 계획이 다른 모양이면 '빠진 주문' 으로 보고 한 번 더 냈다
        # (실제 2026-11-09 TQQQ: 18:02 대체 8주 접수 → 22:00 재시도가 별지점 4 + 평단 4 를 또 내려 함).
        # 대체·합친 단건이 이미 있으면 본 매수 전부를, 원래 모양이 이미 있으면 대체·합친 단건을 뺀다.
        placed_main = set(self.registry.already_submitted(
            trade_date, ticker, list(self.MAIN_BUY_GROUP), include_cancelled=include_cancelled))
        if placed_main & self.MAIN_REPLACEMENTS:
            dup_single |= {t for t in single if t in self.MAIN_BUY_GROUP}
        elif placed_main:
            dup_single |= {t for t in single if t in self.MAIN_REPLACEMENTS}
        out, dup_tags = [], set(dup_single)
        multi_planned: dict[str, list] = {}
        for o in orders:
            if o.tag in self.MULTI_TAGS:
                multi_planned.setdefault(o.tag, []).append(o)
            elif o.tag not in dup_single:
                out.append(o)
        for tag, planned in multi_planned.items():
            live = self.registry.live_prices(trade_date, ticker, tag,
                                             include_cancelled=include_cancelled)
            live_set = set(live)
            room = max(0, len(planned) - len(live))
            fresh = [o for o in sorted(planned, key=lambda x: -(x.price or 0))
                     if round(float(o.price or 0.0), 2) not in live_set][:room]
            if len(fresh) < len(planned):
                dup_tags.add(tag)
            out += fresh
        # 원래 순서 유지
        order_index = {id(o): i for i, o in enumerate(orders)}
        out.sort(key=lambda o: order_index[id(o)])
        return out, dup_tags

    def _core_reserve(self, tickers: list, trade_date: str = "") -> float:
        """뒤에 접수할 종목들의 핵심 매수 예상 금액 (정지 종목 제외).

        그날 핵심 매수를 이미 낸 종목은 빼고 센다. 그 돈은 증권사 주문가능금액에서
        이미 빠져 있다. 두 번 빼면 /run loc 재접수 때 앞 종목 폭락대비가 통째로
        빠진다 (실제 2026-11-04: 가능 $391.51 인데 TQQQ 몫 $481.71 을 또 남겨 0건).
        """
        total = 0.0
        for t in tickers:
            st = self.state_mgr.get_state(t)
            if (st is None or CircuitBreaker.is_halted(st)
                    or getattr(st, "awaiting_principal", False)):
                continue
            if trade_date and self._core_already_placed(t, trade_date):
                continue
            total += max(0.0, _core_or_full(st))
        # 예상치는 전날 정산 때 계산한 값이라 당일 시세(가격제한 대체 등)로 실제
        # 핵심 매수가 조금 커질 수 있다. 여유를 둔다.
        # (실제 2026-11-06: 예상 $461.50 · 실제 $483.54 → $19.92 차이로 TQQQ 별지점·평단 빠짐)
        return round(total * self.CORE_RESERVE_MARGIN, 2)

    #: 다른 종목 핵심 매수 몫을 남길 때의 여유 배율
    CORE_RESERVE_MARGIN = 1.10

    def _core_already_placed(self, ticker: str, trade_date: str) -> bool:
        """그날 핵심 LOC 매수가 접수돼 있거나 체결됐는지"""
        try:
            return any(self._is_core_loc_buy(r) and r.status in ("submitted", "partial", "filled")
                       for r in self.registry.day_orders(trade_date, ticker))
        except Exception:
            return False

    async def _submit_ticker(self, ticker: str, trade_date: str, window: str, label: str,
                             aux_reserve: float = 0.0,
                             budget: float | None = None,
                             auto_retry: bool = False,
                             final_retry: bool = False) -> float:
        """한 종목 접수. 접수한 매수 주문이 묶는 달러를 돌려준다.

        aux_reserve  뒤에 접수할 다른 종목의 핵심 매수 몫. 이 종목의
                     폭락대비는 이 금액을 건드리지 못한다.
        budget       이번 접수에서 이 종목이 쓸 수 있는 최대 달러. 앞 종목이
                     쓴 만큼 줄어 있다. 증권사 주문가능금액이 아직 앞 종목
                     주문을 반영하지 못했어도 겹쳐 쓰지 않게 한다.
        """
        state = self.state_mgr.get_state(ticker)
        if state is None:
            return 0.0

        # 회로차단기 — 장부가 어긋난 상태로는 주문을 내지 않는다
        if CircuitBreaker.is_halted(state):
            logger.warning("[%s] 주문 정지 중 — %s 접수를 건너뜁니다 (%s)",
                           ticker, label, state.halt_reason)
            if window == SubmitWindow.PRE_MARKET:
                await self.notifier.send(
                    f"⛔ [{ticker}] 주문 정지 중이라 접수하지 않았습니다.\n"
                    f"  사유: {state.halt_reason}\n"
                    f"  확인 후 /unhalt {ticker} 로 해제하세요."
                )
            return 0.0

        # 사이클이 끝나고 새 원금을 아직 안 골랐으면 첫 매수를 대기한다
        if getattr(state, "awaiting_principal", False) and state.holdings <= 0:
            logger.info("[%s] 새 원금 대기 중 — %s 접수를 건너뜁니다", ticker, label)
            if window == SubmitWindow.REGULAR:
                await self._ask_principal(ticker, reminder=True)
            return 0.0

        market = await self._market_snapshot(ticker, state.mode)
        mode = ReverseMode(state) if state.mode == "reverse" else NormalMode(state)
        plan = mode.plan(market)

        orders = self._orders_for_window(plan, window)
        if not orders:
            return 0.0

        if plan.warnings and window == SubmitWindow.PRE_MARKET:
            await self.notifier.send(f"[{ticker}] " + "\n".join(plan.warnings))

        # 증권사 앱에서 직접 취소한 LOC 를 원장에 반영한다.
        # 반영하지 않으면 원장엔 "접수됨" 으로 남아, 다시 낼 때 아래 중복
        # 방지가 막는다.
        # 자동 재시도에서는 하지 않는다 — 사용자가 앱에서 취소한 주문을 되살리면 안 된다.
        if not self.config.dry_run and not auto_retry:
            await self._sync_cancelled_loc(ticker, trade_date)

        # 중복 접수 방지 — 같은 날 같은 주문을 두 번 내면 포지션이 두 배가 된다.
        #
        # 창구 단위가 아니라 주문 단위로 걸러낸다. 자금 부족으로 매수만
        # 건너뛴 날, 입금 후 /run loc 로 다시 실행하면 이미 낸 매도는 빼고
        # 매수만 접수되어야 한다.
        orders, dup_tags = self._drop_already_submitted(ticker, trade_date, orders,
                                                        include_cancelled=auto_retry)
        if auto_retry:
            # 재시도는 돈이 없어 빠진 매수만. 증권사가 거부한 주문은 정규장 복구
            # (recover_rejected) 가 맡는다 — 프리장에서 또 내면 또 거부된다.
            refused = {r.tag for r in self.registry.day_orders(trade_date, ticker)
                       if r.status == "rejected" and "증권사 거부" in (r.note or "")}
            orders = [o for o in orders if o.side == "buy" and o.tag not in refused]
            if not orders:
                return 0.0                                     # 다 들어가 있다 — 조용히
        if dup_tags:
            logger.info("[%s] %s 중 이미 접수된 주문 제외 (%s)",
                        ticker, label, ", ".join(sorted(dup_tags)))
            if not orders:
                await self.notifier.send(
                    f"ℹ️ [{ticker}] {label} 는 모두 이미 접수돼 있어 건너뛰었습니다.")
                return 0.0

        # 자금 점검 — 매수만 해당. 매도는 돈이 없어도 낸다.
        #
        # 가진 달러 안에서 우선순위대로 들어갈 수 있는 만큼만 낸다.
        # 원본은 폭락대비까지 합친 금액이 모자라면 매수를 통째로 건너뛰었다.
        # 폭락대비는 거의 체결되지 않는 주문인데 예수금을 가장 많이 묶어서,
        # 정작 별지점·평단 매수가 빠지고 방법론이 그날 멈췄다.
        buys = [o for o in orders if o.side == "buy"]
        if buys:
            from core.funding import allocate_buys
            try:
                ref_price = max(o.price or 0 for o in buys) or market.current_price
                available = await self.kiwoom.available_usd(
                    ticker, exchange_of(ticker), ref_price)
            except Exception as e:
                logger.warning("[%s] 매수 가능 금액 확인 실패: %s", ticker, e)
                available = None

            if budget is not None:
                # 증권사 조회가 실패해도 예산이 있으면 그 안에서 배분한다
                available = (max(0.0, budget) if available is None
                             else min(available, max(0.0, budget)))
            if available is not None:
                alloc = allocate_buys(buys, available, state.fee_rate,
                                      aux_reserve=aux_reserve)
                if not alloc.all_placed:
                    keep = {id(o) for o in alloc.placed}
                    orders = [o for o in orders if o.side != "buy" or id(o) in keep]
                    # 자동 재시도는 새로 들어가는 게 있거나 마지막 시도일 때만 알린다
                    if not auto_retry or alloc.placed or final_retry:
                        from core.fx import safe_rate
                        text = self._funding_notice(
                            ticker, alloc, orders, krw_rate=safe_rate(self.state_mgr))
                        diag = await self._krw_diag(ticker, exchange_of(ticker), ref_price)
                        if diag:
                            text += "\n" + diag
                        if auto_retry:
                            text = f"🔁 {label}\n" + text
                        await self.notifier.send(text)
                    if not orders:
                        return 0.0

        if self.config.dry_run:
            lines = "\n".join(f"  {o}" for o in orders)
            await self.notifier.send(
                f"[DRY RUN] {ticker} {label}\n{plan.summary().splitlines()[0]}\n{lines}")
            return 0.0

        exchange = exchange_of(ticker)
        submitted = 0
        placed: list = []
        for order in orders:
            record_id = self.registry.record_submission(trade_date, ticker, order)
            try:
                res = await self._place(ticker, exchange, order)
                # 실시간 주문은 주문번호, 예약주문은 예약번호를 남긴다
                if getattr(res, "rsrv_ord_no", ""):
                    self.registry.attach_rsrv_ord_no(record_id, res.rsrv_ord_no)
                elif getattr(res, "ord_no", ""):
                    self.registry.attach_ord_no(record_id, res.ord_no)
                placed.append(order)
                submitted += 1
            except KiwoomOrderUncertainError as e:
                # 접수됐는지 알 수 없다. 원장에 남겨두고(삭제하지 않는다)
                # 정지한 뒤 사람이 확인하게 한다. 임의로 재전송하면
                # 중복 주문이 된다.
                self.registry.set_status(record_id, "submitted", f"결과 불명: {e}")
                CircuitBreaker.halt(state, f"주문 결과 불명 — {order}")
                self.state_mgr.save_state(state)
                await self.notifier.send(
                    f"🚨 [{ticker}] 주문 결과를 확인할 수 없습니다.\n"
                    f"  {order}\n"
                    f"  접수됐을 수 있어 재전송하지 않았습니다.\n"
                    f"  증권사 앱에서 실제 주문을 확인하세요.\n"
                    f"  이후 주문은 정지했습니다. /unhalt {ticker} 로 해제합니다."
                )
                return self._buy_cost(placed, state.fee_rate)
            except KiwoomAPIError as e:
                self.registry.set_status(record_id, "rejected", str(e))
                await self.notifier.send(f"⛔ [{ticker}] {order} 접수 거부: {e.message}")
                if order.trade_type == TradeType.MOC:
                    fb = await self._moc_fallback(ticker, exchange, order, trade_date,
                                                  f"접수 거부: {e.message}")
                    if fb is not None:
                        placed.append(fb)
                        submitted += 1
            except Exception as e:
                # 전송 자체가 안 된 경우(값 검증 실패 등). 원장에 '접수됨' 으로
                # 남겨두면 나가지도 않은 주문이 다른 체결을 가로챈다.
                logger.exception("[%s] 주문 전송 실패", ticker)
                self.registry.set_status(record_id, "rejected", f"전송 실패: {e}")
                await self.notifier.send(f"⚠️ [{ticker}] {order} 전송 실패: {e}")

        logger.info("[%s] %s %d/%d건 접수", ticker, label, submitted, len(orders))
        if submitted == 0 and orders:
            await self.notifier.send(
                f"⚠️ [{ticker}] {label} {len(orders)}건이 모두 실패했습니다. "
                f"증권사 앱에서 주문 상태를 확인하세요.")
        elif placed:
            # 접수 성공 알림.
            #
            # 원본은 실패·거부일 때만 알렸다. 잘 들어간 날은 조용해서,
            # 주문이 나갔는지 확인하려면 매번 /orders 를 쳐야 했다.
            await self.notifier.send(self._submit_report(
                ticker, label, placed, star=self._star_line(plan, state)))
        return self._buy_cost(placed, state.fee_rate)

    @staticmethod
    def _buy_cost(orders: list, fee_rate: float) -> float:
        from core.funding import order_cost
        return round(sum(order_cost(o, fee_rate) for o in orders if o.side == "buy"), 2)

    @staticmethod
    def _funding_notice(ticker: str, alloc, orders: list, krw_rate: float = 0.0) -> str:
        """주문가능금액(원화 포함)이 모자라 일부 매수를 뺄 때의 알림"""
        from core.fx import krw_text
        def _line(o):
            return f"    {o.qty}주 @{o.price:.2f}  {o.tag}"

        placed_buys = [o for o in alloc.placed]
        sells_left = any(o.side == "sell" for o in orders)
        head = ("💸" if not alloc.core_complete else "⚪")
        L = [f"{head} [{ticker}] 주문가능금액이 부족해 매수 일부만 접수합니다"
             if placed_buys else
             f"💸 [{ticker}] 주문가능금액이 부족해 오늘 매수를 건너뜁니다",
             f"  가능 ${alloc.available:,.2f} · 핵심 ${alloc.core_need:,.2f} · "
             f"전체 ${alloc.full_need:,.2f}"]

        if placed_buys:
            L.append("")
            L.append("  🔴 접수")
            L += [_line(o) for o in placed_buys]
        L.append("")
        L.append("  ⚪ 제외")
        L += [_line(o) for o in alloc.skipped]

        L.append("")
        if not alloc.core_complete:
            short = max(0.0, alloc.core_need - alloc.available)
            L.append(f"  ⚠️ 별지점·평단 매수가 빠졌습니다. 최소 ${short:,.2f}"
                     f"{krw_text(short, krw_rate)} 입금 후")
            L.append("     /run loc 로 빠진 매수만 다시 접수할 수 있습니다 (장 시작 전까지).")
        else:
            L.append("  ✅ 별지점·평단 매수는 모두 들어갔습니다. 폭락대비만 일부 빠짐.")
            if getattr(alloc, "aux_reserve", 0) > 0:
                L.append(f"  🔒 다른 종목 별지점·평단 몫 ${alloc.aux_reserve:,.2f} 는 남겨뒀습니다.")
        if sells_left:
            L.append("  🔵 매도 주문은 그대로 접수합니다.")
        return "\n".join(L)

    async def _krw_diag(self, ticker: str, exchange: str, price: float) -> str:
        """부족 알림에 붙일 원화주문 상태 한 줄 — 원화가 언제부터 잡히는지 기록용"""
        try:
            bp = await self.kiwoom.get_buying_power(ticker, exchange, price)
        except Exception:
            return ""
        raw = bp.get("raw") or {}
        try:
            set_amt = int(str(raw.get("krw_ord_set_amt") or "0").strip() or 0)
        except ValueError:
            set_amt = 0
        if not bp.get("krw_order") and not set_amt:
            return ""
        now = datetime.now(KST).strftime("%H:%M")
        line = (f"  🔎 원화주문 ({now}): 설정 {set_amt:,}원 · 평가 ${bp.get('krw_eval_usd', 0.0):,.2f}"
                f" · 달러만 ${bp.get('cash_no_margin', 0.0):,.2f}")
        logger.info("[%s] %s", ticker, line.strip())
        if set_amt > 0 and float(bp.get("krw_eval_usd") or 0.0) <= 0:
            line += ("\n     원화는 설정돼 있는데 아직 평가가 0입니다 — 잠시 뒤 자동으로 다시 시도합니다.")
        return line

    async def _ask_principal(self, ticker: str, reminder: bool = False) -> None:
        """새 원금 선택 버튼을 보낸다"""
        from core.principal import prompt
        try:
            st = self.state_mgr.get_state(ticker)
            if st is None or not getattr(st, "awaiting_principal", False):
                return
            avail = await self._account_available()
            text, rows = prompt(ticker, st, avail)
            if reminder:
                text = "⏸ 오늘 첫 매수를 하지 않았습니다 — " + text.lstrip("💰 ")
            sender = getattr(self.notifier, "send_buttons", None)
            if sender is not None:
                await sender(text, rows)
            else:
                await self.notifier.send(text)
        except Exception:
            logger.exception("[%s] 원금 선택 안내 실패", ticker)

    async def _moc_fallback(self, ticker: str, exchange: str, order, trade_date: str,
                            reason: str):
        """거부된 MOC 매도를 낮은 가격 LOC 매도로 다시 낸다. 낸 주문을 돌려준다."""
        from core.reverse_guard import fallback_order
        if self.config.dry_run:
            return None
        try:
            q = await self.kiwoom.get_quote(ticker, exchange)
            ref = q.get("cur_price") or q.get("prev_close") or 0.0
            if ref <= 0:
                raise ValueError("기준가를 받지 못했습니다")
            fb = fallback_order(order, ref)
            rid = self.registry.record_submission(trade_date, ticker, fb)
            try:
                res = await self.kiwoom.sell(ticker, exchange, fb.qty, fb.trade_type, fb.price)
            except KiwoomOrderUncertainError as e:
                # 접수됐을 수도 있다. 직접 팔라고 하면 두 번 팔린다.
                self.registry.set_status(rid, "submitted", f"MOC 대체 결과 불명: {e}")
                with self.state_mgr.edit_state(ticker) as st:
                    CircuitBreaker.halt(st, f"MOC 대체 주문 결과 불명 — {fb}")
                await self.notifier.send(
                    f"🚨 [{ticker}] MOC 대체 LOC 매도의 결과를 확인할 수 없습니다.\n"
                    f"  {fb.qty}주 LOC @{fb.price:.2f} 가 접수됐을 수 있습니다.\n"
                    f"  직접 매도하지 말고, 먼저 앱 미체결 내역에서 이 주문이 있는지 확인하세요.\n"
                    f"  없을 때만 직접 매도하세요. 이후 주문은 정지했습니다 (/unhalt {ticker}).")
                return None
            except Exception as e:
                self.registry.set_status(rid, "rejected", f"MOC 대체 실패: {e}")
                raise
            if getattr(res, "ord_no", ""):
                self.registry.attach_ord_no(rid, res.ord_no)
            await self.notifier.send(
                f"🔁 [{ticker}] MOC 매도가 거부돼 LOC 매도로 대체했습니다\n"
                f"  사유: {reason}\n"
                f"  {fb.qty}주 LOC @{fb.price:.2f} (기준가 ${ref:.2f}의 75%)\n"
                f"  종가가 이 가격 이상이면 종가에 체결 — MOC 와 같은 효과입니다.")
            return fb
        except Exception as e:
            logger.exception("[%s] MOC 대체 주문 실패", ticker)
            await self.notifier.send(
                f"🚨 [{ticker}] MOC 매도 거부 + 대체 LOC 매도도 실패했습니다: {e}\n"
                f"  오늘 리버스 매도가 빠집니다. 앱에서 {order.qty}주를 직접 매도하세요\n"
                f"  (LOC 아주 낮은 가격 또는 장 마감 직전 시장가).")
            return None

    @staticmethod
    def _star_line(plan, state) -> str:
        """⭐ 별지점 $80.71 · 별% +14.25% (평단 $70.64 · T 2.00)"""
        star = getattr(plan, "star_point", None)
        avg = getattr(state, "avg_price", 0.0) or 0.0
        if not star:
            return ""
        name = "별지점(MA5)" if getattr(plan, "mode", "") == "reverse" else "별지점"
        if avg <= 0:
            return f"  ⭐ {name} ${star:.2f}"
        pct = (star / avg - 1) * 100
        if getattr(plan, "mode", "") != "reverse":
            # 일반모드는 방법론 공식값(가격 반올림 전)을 보여준다
            try:
                from core.star_point import star_pct
                pct = star_pct(plan.ticker, state.division, plan.T)
            except Exception:
                pass
        return (f"  ⭐ {name} ${star:.2f} · 별% {pct:+.2f}%"
                f" (평단 ${avg:.2f} · T {plan.T:.2f})")

    @staticmethod
    def _submit_report(ticker: str, label: str, placed: list, star: str = "") -> str:
        """접수 결과 요약"""
        buys = [o for o in placed if o.side == "buy"]
        sells = [o for o in placed if o.side == "sell"]
        L = [f"📤 [{ticker}] {label} · {len(placed)}건"]
        if star:
            L.append(star)

        for group, mark, name in ((sells, "🔵", "매도"), (buys, "🔴", "매수")):
            if not group:
                continue
            L.append("")
            for o in group:
                price = f"@{o.price:.2f}" if o.price is not None else "시장가"
                kind = {TradeType.LOC: "LOC", TradeType.MOC: "MOC",
                        TradeType.LIMIT: "지정가"}.get(o.trade_type, o.trade_type)
                L.append(f"  {mark} {name} {o.qty:>2}주 {price:>9}  {kind}  {o.tag}")

        if buys:
            L.append("")
            L.append(f"  💵 소요 ${sum(o.amount for o in buys):,.2f}")
        return "\n".join(L)

    @staticmethod
    def _orders_for_window(plan: OrderPlan, window: str) -> list[PlannedOrder]:
        """접수 창구별 주문 분배.

        PRE_MARKET 창구는 프리장 전용 주문만,
        REGULAR 창구는 나머지 전부(ANY 포함)를 가져간다.
        ANY 를 정확히 일치 비교하면 LOC 주문이 통째로 누락된다.
        """
        if window == SubmitWindow.PRE_MARKET:
            return [o for o in plan.orders if o.window == SubmitWindow.PRE_MARKET]
        return [o for o in plan.orders if o.window != SubmitWindow.PRE_MARKET]

    async def _place(self, ticker: str, exchange: str, order: PlannedOrder):
        """PlannedOrder → 키움 주문.

        지정가·LOC 는 실시간 주문으로 프리장에 바로 낸다. MOC 만 예약주문.

        예약주문은 정규장 개장(22:30) 때 실주문으로 넘어가고, 거부 여부도
        그때 결정된다. 실시간으로 내면
          - 지정가매도가 프리장부터 살아 있다 (방법론 6)
          - 증거금·가격 거부가 접수 즉시 드러나, 입금·재접수할 시간이 생긴다
          - 예약주문의 날짜 규칙·취소 가능 시간(08:00~22:25) 제약이 없다

        실측(2026-09-22 17:25, 17:29): 실시간 지정가·LOC 모두 프리장에 바로
        접수되어 미체결에 표시됐다. 현재가 대비 −29% LOC 도 받아졌다.

        MOC 는 리버스모드 첫날에만 쓰는데 프리장 실시간 접수가 아직
        확인되지 않아 예약주문으로 둔다.
        """
        if order.trade_type == TradeType.MOC:
            return await self.kiwoom.reserve_moc_sell(ticker, exchange, order.qty)
        if order.side == "buy":
            return await self.kiwoom.buy(
                ticker, exchange, order.qty, order.trade_type, order.price)
        return await self.kiwoom.sell(
            ticker, exchange, order.qty, order.trade_type, order.price)

    # ------------------------------------------------------------
    # 예약주문 검증
    # ------------------------------------------------------------

    async def verify_reserved(self, session: date) -> None:
        """접수한 예약주문이 실제로 살아있는지 확인한다.

        ust21200/ust21201 은 증거금·잔고·가격제한폭을 검증하지 않고
        접수되므로, 접수 성공이 체결 대기를 보장하지 않는다.
        거부된 건은 원장에서 빼야 다음날 태그 매칭이 어긋나지 않는다.
        """
        if self.config.dry_run:
            return

        for ticker in self.state_mgr.list_tickers():
            try:
                bad = await self.kiwoom.verify_reserved_orders(ticker, exchange_of(ticker))
            except Exception as e:
                logger.exception("[%s] 예약 검증 실패", ticker)
                await self.notifier.send(f"⚠ [{ticker}] 예약주문 검증 실패: {e}")
                continue

            # 봇 원장에서 아직 살아 있다고 보는 예약만 알린다.
            # 이미 취소·만료로 기록한 예약이나 사용자가 직접 건 예약은 뺀다.
            ours = self.registry.bot_reserved_orders(ticker)
            bad = [b for b in bad if str(b.get("rsrv_ord_no")) in ours]

            if bad:
                lines = [f"⛔ [{ticker}] 예약주문 {len(bad)}건 거부"]
                moc_rejected = []
                for b in bad:
                    rec = ours.get(str(b.get("rsrv_ord_no")))
                    self.registry.mark_rejected_by_rsrv_no(b["rsrv_ord_no"], b["reason"])
                    lines.append(f"  {b['ord_qty']}주 @{b['ord_uv']:.2f} — "
                                 f"{b['reason'] or b.get('proc_tp') or '사유 미상'}")
                    # 오늘 낸 MOC 만 대체한다. 지난 날 기록이 남아 있다가
                    # 거부로 보이는 경우까지 오늘 매도를 내면 안 된다.
                    if (rec is not None and rec.trade_type == TradeType.MOC
                            and rec.trade_date == session.strftime("%Y%m%d")):
                        moc_rejected.append((rec, b))
                await self.notifier.send("\n".join(lines))

                # 리버스 MOC 매도가 거부되면 그날 매도가 빠진다 — 바로 LOC 로 대체
                for rec, b in moc_rejected:
                    order = PlannedOrder(tag=rec.tag, side="sell", trade_type=TradeType.MOC,
                                         qty=rec.qty, price=None)
                    await self._moc_fallback(ticker, exchange_of(ticker), order,
                                             rec.trade_date,
                                             f"예약 거부: {b['reason'] or b.get('proc_tp') or '사유 미상'}")

            await self._verify_live(ticker)

    async def _sync_cancelled_loc(self, ticker: str, trade_date: str) -> int:
        """원장엔 접수됨인데 증권사 미체결에 없는 LOC 를 취소로 표시한다.

        LOC 는 종가에만 체결되므로 장중에 미체결에서 사라졌다면 취소된 것이
        확실하다. 지정가는 장중 체결일 수 있어 건드리지 않는다.

        미체결 조회가 실패하면 아무것도 바꾸지 않는다. 확인 없이 취소로
        표시하면 살아 있는 주문을 한 번 더 내게 된다.
        """
        recs = [r for r in self.registry.day_orders(trade_date, ticker)
                if r.status in ("submitted", "partial") and r.ord_no
                and not r.rsrv_ord_no and r.trade_type == TradeType.LOC]
        if not recs:
            return 0
        try:
            live = {str(o["ord_no"]) for o in
                    await self.kiwoom.get_open_orders(ticker, exchange_of(ticker))}
        except Exception as e:
            logger.warning("[%s] 미체결 조회 실패 — 취소 반영 생략: %s", ticker, e)
            return 0

        from core.order_registry import normalize_ord_no
        live_n = {normalize_ord_no(n) for n in live}
        gone = [r for r in recs if normalize_ord_no(r.ord_no) not in live_n]
        if not gone:
            return 0
        stats = await self._broker_stats(ticker)
        rej = can = unk = 0
        for r in gone:
            kind = self._mark_gone(r, stats)
            rej += kind == "rejected"
            can += kind == "cancelled"
            unk += kind == "unknown"
        logger.info("[%s] 미체결에서 사라진 LOC — 거부 %d · 취소 %d · 확인불가 %d", ticker, rej, can, unk)
        if rej or can:
            parts = ([f"거부 {rej}건"] if rej else []) + ([f"취소 {can}건"] if can else [])
            await self.notifier.send(
                f"🗑 [{ticker}] 미체결에서 사라진 LOC 를 확인했습니다 ({' · '.join(parts)}). 다시 접수합니다.")
        if unk:
            await self.notifier.send(
                f"⚠️ [{ticker}] 미체결에 안 보이는 LOC {unk}건의 상태를 증권사에서 확인하지 못했습니다.\n"
                f"  중복을 막으려고 다시 내지 않습니다. 앱 미체결을 확인해 주세요 (/today {ticker}).")
        return rej + can

    async def _broker_stats(self, ticker: str) -> dict:
        """증권사 당일 주문의 상태 {정규화 주문번호: '접수'/'거부'/'취소'...}. 실패하면 {}"""
        from core.order_registry import normalize_ord_no
        try:
            rows = await self.kiwoom.get_today_orders(ticker, exchange_of(ticker))
        except Exception as e:
            logger.warning("[%s] 당일 주문 조회 실패: %s", ticker, e)
            return {}
        return {normalize_ord_no(r.get("ord_no")): str(r.get("ord_stat") or "") for r in rows}

    def _mark_gone(self, rec, stats: dict) -> str:
        """미체결에서 사라진 LOC 를 증권사 상태대로 원장에 반영한다.

        'rejected' / 'cancelled' 는 증권사가 그렇게 말할 때만 쓴다. 상태를 모르거나
        (조회 실패·목록 지연) 아직 '접수' 라고 하면 원장을 바꾸지 않고 'unknown'.
        살아 있는 주문을 취소로 적으면 /run loc 가 같은 주문을 한 번 더 낸다.
        """
        from core.order_registry import normalize_ord_no
        st = stats.get(normalize_ord_no(rec.ord_no), "")
        base = (rec.note or "").strip()
        if "거부" in st:
            self.registry.set_status(rec.id, "rejected", (base + " · 증권사 거부").strip(" ·"))
            return "rejected"
        if "취소" in st:
            self.registry.set_status(rec.id, "cancelled", (base + " · 직접 취소").strip(" ·"))
            return "cancelled"
        return "unknown"

    async def _verify_live(self, ticker: str) -> None:
        """봇이 낸 실시간 주문이 미체결에 살아 있는지 확인한다.

        LOC 는 종가에만 체결되므로 장중에 미체결에서 사라졌다면 거부됐거나
        누가 취소한 것이다. 그대로 두면 그날 매수가 빠진 줄 모른다.

        지정가매도는 프리장·장중에 체결될 수 있어서, 사라졌다고 이상으로
        보지 않는다 (체결은 EOD 에서 반영된다).

        세션이 바뀔 때(프리장 → 정규장) 실시간 주문이 유지되는지도 이
        검사로 확인된다.
        """
        ours = self.registry.bot_live_orders(ticker)
        if not ours:
            return
        try:
            live = {str(o["ord_no"]) for o in
                    await self.kiwoom.get_open_orders(ticker, exchange_of(ticker))}
        except Exception as e:
            logger.warning("[%s] 미체결 조회 실패: %s", ticker, e)
            return

        from core.order_registry import normalize_ord_no
        live_n = {normalize_ord_no(n) for n in live}
        missing = [r for no, r in ours.items()
                   if normalize_ord_no(no) not in live_n and r.trade_type == TradeType.LOC]
        if not missing:
            return
        stats = await self._broker_stats(ticker)
        lines = [f"⚠️ [{ticker}] LOC 주문 {len(missing)}건이 미체결에서 사라졌습니다"]
        any_core_buy = False
        for r in missing:
            kind = self._mark_gone(r, stats)
            side = "🔴 매수" if r.side == "buy" else "🔵 매도"
            word = {"rejected": "증권사 거부", "cancelled": "취소됨"}.get(
                kind, "상태 확인 불가 — 그대로 둡니다")
            lines.append(f"  {side} {r.qty}주 @{r.price:.2f} [{r.tag}] — {word}")
            if kind == "rejected" and self._is_core_loc_buy(r):
                any_core_buy = True
        if any_core_buy:
            lines.append("  거부된 별지점·평단 매수는 정규장에서 자동으로 다시 냅니다"
                         " (같은 가격 → 현재가×1.15 대체).")
        else:
            lines.append("  키움 앱 주문내역에서 확인하세요.")
        await self.notifier.send("\n".join(lines))

    # ------------------------------------------------------------
    # 거부된 핵심 매수 되살리기
    # ------------------------------------------------------------

    #: 원장 메모 표시 — 몇 단계까지 했는지
    R1, R2, R3 = "정규장 재접수", "가격제한 대체", "자동 복구 실패 알림"

    @staticmethod
    def _is_core_loc_buy(rec) -> bool:
        from core.t_calculator import MAIN_BUY_KINDS
        return (rec.side == "buy" and rec.trade_type == TradeType.LOC
                and (rec.tag in MAIN_BUY_KINDS or rec.tag == FillKind.REVERSE_QUARTER_BUY))

    async def _recover_first(self, session: date) -> None:
        await self.recover_rejected(session)

    async def _recover_second(self, session: date) -> None:
        await self.recover_rejected(session)

    async def _recover_final(self, session: date) -> None:
        await self.recover_rejected(session, final=True)

    async def recover_rejected(self, session: date, final: bool = False) -> None:
        """증권사가 거부한 핵심 LOC 매수를 정규장에서 되살린다.

        프리장에서 별지점 매수가 거부되고 정규장에서는 같은 주문이 받아진
        사례가 있었다 (2026-11-03 TQQQ 3주 @72.55: 18:02·19:43 거부, 00:40 접수).
        현재가보다 위에 거는 매수가 프리장 가격 범위에 걸린 것으로 보인다.

          1차  같은 태그·가격·수량으로 다시 낸다
          2차  그래도 거부되면 현재가×1.15 에 같은 금액만큼 (방법론 8)
          3차  그래도 안 되면 직접 확인하라고 알린다

        태그는 원래 것을 그대로 쓴다. 전반전 별지점 매수(+0.5)를 대체 매수
        (+1)로 바꾸면 T 가 틀어진다.
        """
        if self.config.dry_run:
            return
        trade_date = session.strftime("%Y%m%d")
        for ticker in self.state_mgr.list_tickers():
            try:
                st = self.state_mgr.get_state(ticker)
                if st is None or CircuitBreaker.is_halted(st):
                    continue
                if getattr(st, "awaiting_principal", False):
                    continue
                await self._verify_live(ticker)          # 새로 거부된 것을 원장에 반영
                await self._recover_ticker(ticker, trade_date, final)
            except Exception:
                logger.exception("[%s] 거부 주문 복구 실패", ticker)

    async def _recover_ticker(self, ticker: str, trade_date: str, final: bool = False) -> None:
        recs = [r for r in self.registry.day_orders(trade_date, ticker) if self._is_core_loc_buy(r)]
        by_tag: dict[str, list] = {}
        for r in recs:
            by_tag.setdefault(r.tag, []).append(r)
        exchange = exchange_of(ticker)
        for tag, rows in by_tag.items():
            if any(r.status in ("submitted", "partial", "filled") for r in rows):
                continue                                  # 살아 있거나 체결됨 (결과 불명 포함)
            if any(self.R3 in (r.note or "") for r in rows):
                continue                                  # 이미 포기 알림을 보냈다 — 더 내지 않는다
            latest = max(rows, key=lambda r: r.id)
            note = latest.note or ""
            if latest.status != "rejected" or "증권사 거부" not in note:
                continue                                  # 직접 취소 등은 건드리지 않는다
            if self.R2 in note:
                await self._recover_give_up(ticker, rows)
                continue
            if self.R1 not in note:
                res = await self._recover_place(ticker, exchange, trade_date, latest,
                                                latest.price, latest.qty, self.R1)
                if res != "rejected":
                    continue                              # 접수됨 또는 결과 불명 → 다음 확인에서
            # 2단계: 현재가 × 1.15
            try:
                q = await self.kiwoom.get_quote(ticker, exchange)
                cur = float(q.get("cur_price") or q.get("prev_close") or 0.0)
            except Exception:
                cur = 0.0
            if cur <= 0:
                if final:
                    await self._recover_give_up(ticker, rows)
                continue                                  # 시세를 못 받았다 — 다음 확인에서 다시
            from core.star_point import round_half_up
            guard = round_half_up(cur * 1.15, 2)
            amount = (latest.price or 0.0) * latest.qty
            qty = int(amount // guard) if guard > 0 else 0
            if guard >= (latest.price or 0.0) or qty < 1:
                await self._recover_give_up(ticker, rows)        # 가격 문제가 아니다
                continue
            res = await self._recover_place(ticker, exchange, trade_date, latest,
                                            guard, qty, self.R2)
            if res == "rejected":
                await self._recover_give_up(
                    ticker, [r for r in self.registry.day_orders(trade_date, ticker) if r.tag == tag])

    async def _recover_place(self, ticker, exchange, trade_date, src, price, qty, mark) -> str:
        """복구 주문 한 건. 'placed' / 'rejected' / 'unknown'

        증권사가 명시적으로 거부한 경우(KiwoomAPIError)만 'rejected'. 그 밖의
        오류는 주문이 나갔을 수 있으므로 'unknown' 으로 멈춘다 (다음 단계로 가면
        주문이 두 건 살아 있을 수 있다).
        """
        order = PlannedOrder(tag=src.tag, side="buy", trade_type=TradeType.LOC,
                             qty=int(qty), price=float(price), note=mark)
        rid = self.registry.record_submission(trade_date, ticker, order)
        try:
            res = await self.kiwoom.buy(ticker, exchange, order.qty, order.trade_type, order.price)
        except KiwoomAPIError as e:
            self.registry.set_status(rid, "rejected", f"{mark} · 증권사 거부: {e}")
            return "rejected"
        except Exception as e:
            self.registry.set_status(rid, "submitted", f"{mark} · 결과 불명: {e}")
            await self.notifier.send(
                f"🚨 [{ticker}] {mark} 주문 결과를 확인할 수 없습니다 ({order.qty}주 @{order.price:.2f}).\n"
                f"  앱 미체결에서 확인하세요. 중복을 막으려고 더 내지 않습니다.")
            return "unknown"
        if getattr(res, "ord_no", ""):
            self.registry.attach_ord_no(rid, res.ord_no)
        head = "🔁" if mark == self.R1 else "🛟"
        how = ("같은 가격으로 정규장에서 다시 냈습니다" if mark == self.R1
               else "현재가×1.15 로 대체해 냈습니다 (방법론 8)")
        await self.notifier.send(
            f"{head} [{ticker}] 거부된 {src.tag} 를 {how}\n"
            f"  {order.qty}주 LOC @{order.price:.2f}  (원래 {src.qty}주 @{src.price:.2f})")
        return "placed"

    async def _recover_give_up(self, ticker: str, rows) -> None:
        """포기 알림. 같은 태그의 기록 모두에 표시해 다음 확인에서 다시 내지 않는다."""
        rows = [r for r in rows if self._is_core_loc_buy(r)]
        if not rows:
            return
        first = min(rows, key=lambda r: r.id)
        for r in rows:
            if r.tag == first.tag and self.R3 not in (r.note or ""):
                self.registry.set_status(r.id, r.status, ((r.note or "") + f" · {self.R3}").strip(" ·"))
        await self.notifier.send(
            f"🚨 [{ticker}] {first.tag} 매수가 다시 내도 거부됐습니다 ({first.qty}주 @{first.price:.2f}).\n"
            f"  봇은 오늘 이 매수를 더 내지 않습니다. 꼭 넣어야 하면 앱에서 같은 가격 LOC 로 직접 넣으세요\n"
            f"  (같은 가격·수량이면 정산 때 봇 주문으로 인식됩니다). /today {ticker} {first.price:.2f}")

    # ------------------------------------------------------------
    # EOD 정산
    # ------------------------------------------------------------

    async def run_eod(self, session: date) -> None:
        trade_date = session.strftime("%Y%m%d")
        for ticker in self.state_mgr.list_tickers():
            try:
                await self._eod_ticker(ticker, trade_date, session)
            except Exception as e:
                logger.exception("[%s] EOD 실패", ticker)
                await self.notifier.send(f"⚠️ [{ticker}] EOD 정산 실패: {e}")
                # EOD 가 실패하면 T값·평단이 갱신되지 않은 채 다음날 주문이 나간다.
                # 연속 실패하면 멈춘다.
                try:
                    with self.state_mgr.edit_state(ticker) as state:
                        halted = CircuitBreaker.record_eod_failure(state, str(e))
                    if halted:
                        await self.notifier.send(
                            f"⛔ [{ticker}] EOD 연속 실패로 주문을 정지했습니다.\n"
                            f"확인 후 /unhalt {ticker} 로 해제하세요.")
                except Exception:
                    logger.exception("[%s] EOD 실패 집계 실패", ticker)

    async def _eod_ticker(self, ticker: str, trade_date: str, session: date) -> None:
        exchange = exchange_of(ticker)

        # 체결 수집 — WebSocket 수신분과 API 조회분을 대조한다
        api_rows = await self.kiwoom.get_today_orders(ticker, exchange)
        api_fills = fills_from_api_rows(api_rows)
        ws_fills = await self.fill_source(trade_date, ticker) if self.fill_source else []
        fills, missed = merge_fill_sources(ws_fills, api_fills)
        if missed:
            await self.notifier.send(
                f"🛟 [{ticker}] WebSocket 이 놓친 체결 {len(missed)}건을 API 조회로 보완했습니다")

        quote = await self.kiwoom.get_quote(ticker, exchange)
        close_price = quote["cur_price"] or quote["prev_close"]

        # 시세 조회(await)는 잠금 밖에서 한다. 잠금을 쥔 채 기다리면 그 사이
        # 텔레그램 버튼이 같은 종목 잠금을 요청할 때 이벤트 루프가 멈춘다.
        pre = self.state_mgr.get_state(ticker)
        next_market = await self._market_snapshot(ticker, pre.mode, quote=quote)

        with self.state_mgr.edit_state(ticker) as state:
            t_before, mode_before = state.T, state.mode
            if state.mode != pre.mode:          # 그 사이 바뀌었으면 다시 조회하지 않고 그대로 쓴다
                logger.warning("[%s] 정산 중 모드가 바뀌었습니다 (%s → %s)", ticker, pre.mode, state.mode)
            result = self.eod.run(state, trade_date, fills, close_price, next_market)
            # 사이클이 끝나면 새 원금을 고를 때까지 첫 매수를 대기한다
            if result.cycle_closed and state.holdings <= 0:
                state.awaiting_principal = True
            if getattr(state, "awaiting_principal", False):
                state.next_buy_need = 0.0
                state.next_core_need = 0.0
            self.state_mgr.archive_eod(ticker, trade_date, {
                "T": state.T, "holdings": state.holdings,
                "avg_price": state.avg_price, "cash": state.cash,
                "mode": state.mode, "close": close_price,
                "fills": result.fills_applied, "realized_pl": result.realized_pl,
                # 빠져 있어 /stats 가 사이클 종료를 몰랐다 (모든 사이클이 하나로 합쳐짐)
                "cycle_closed": bool(result.cycle_closed),
                "division": state.division, "principal": state.principal,
                # 세금 계산(원화 환산)용 — 그날 기준환율
                "usd_krw": quote.get("usd_krw") or 0.0,
            })
            CircuitBreaker.record_eod_success(state)
            try:
                stats = collect_stats(self.state_mgr, ticker)
            except Exception:
                logger.exception("[%s] 통계 집계 실패", ticker)
                stats = None
            report = daily_report(state, self.registry, result, stats=stats)

        await self.notifier.send(report)

        # 리버스 접근선(분할 − 5)을 처음 넘은 날 따로 알린다
        try:
            from core.reverse_guard import crossed_approach, crossing_alert
            if crossed_approach(t_before, state.T, state.division, mode_before) \
                    and state.mode == "normal":
                await self.notifier.send(crossing_alert(ticker, state))
        except Exception:
            logger.exception("[%s] 리버스 접근 알림 실패", ticker)

        # 사이클이 끝난 날은 결산을 따로 보내고, 새 원금을 묻는다
        if result.cycle_closed and stats is not None and stats.cycles:
            try:
                await self.notifier.send(self._cycle_end_text(ticker, stats, state))
            except Exception:
                logger.exception("[%s] 사이클 결산 전송 실패", ticker)
        if result.cycle_closed and state.holdings <= 0:
            await self._ask_principal(ticker)

        # 장부 대조 — 어긋나면 다음 거래일 주문을 멈춘다
        await self._reconcile_ticker(ticker, exchange, result)

        if result.anomalies or result.unmatched_fills:
            # 그날 주문 전부(취소·거부 포함)를 나열하면 화면이 길어져
            # 정작 확인할 것이 묻힌다. 살아 있던 주문만 보여준다.
            rows = [r for r in self.registry.day_orders(trade_date, ticker)
                    if r.status in ("filled", "partial", "submitted", "expired")]
            lines = [f"⚠️ [{ticker}] 수동 확인이 필요합니다", f"  {trade_date}"]
            for r in rows:
                side = "🔴 매수" if r.side == "buy" else "🔵 매도"
                lines.append(f"  {side} {r.qty}주 @{r.price:.2f} [{r.tag}] "
                             f"{ '체결' if r.status == 'filled' else r.status}")
            await self.notifier.send("\n".join(lines))

    # ------------------------------------------------------------
    # 장부 대조
    # ------------------------------------------------------------

    async def _reconcile_ticker(self, ticker: str, exchange: str, eod_result) -> None:
        """전략 장부와 증권사 잔고를 대조하고, 어긋나면 주문을 멈춘다.

        이 단계가 없으면 봇은 어긋난 상태를 스스로 알아차리지 못하고
        잘못된 평단 기준으로 매일 주문을 낸다.
        """
        # get_position 은 "조회 실패" 와 "보유 0주" 를 모두 None 으로 돌려준다.
        # 그대로 넘기면 대조가 건너뛰어져, 수량 검증이라는 회로차단기의
        # 핵심이 무력화된다. 예외 여부로 둘을 가른다.
        try:
            position = await self.kiwoom.get_position(ticker, exchange)
            if position is None:
                position = {"poss_qty": 0, "qty": 0, "avg_price": 0.0}
        except Exception as e:
            logger.warning("[%s] 증권사 잔고 조회 실패: %s", ticker, e)
            position = None

        try:
            deposit = (await self.kiwoom.get_deposit_usd())["d0_usd"]
        except Exception:
            deposit = None

        required = 0.0
        if eod_result.next_plan is not None:
            required = getattr(eod_result.next_plan, "max_buy_amount", 0.0)

        with self.state_mgr.edit_state(ticker) as state:
            result = reconcile(state, position, deposit, required)
            newly_halted = apply_reconcile(state, result, position)
            for c in result.corrections:
                self.state_mgr.record_correction(ticker, {
                    "source": "auto_reconcile", "field": c.field,
                    "before": c.before, "after": c.after, "reason": c.reason,
                })

        if result.severity != "ok":
            await self.notifier.send(result.report())

        # 액면분할·병합이면 옛 가격으로 걸린 주문을 거둔다.
        #
        # 분할 후에도 그대로 두면 값이 엉뚱해진다. 병합(1:10)이면 매도
        # 주문이 새 가격보다 한참 아래라 즉시 체결되고, 분할(1:2)이면
        # 매수 주문이 새 가격보다 위라 계획에 없던 매수가 들어간다.
        if any(f.code == "split_detected" for f in result.findings):
            await self._cancel_stale_orders(ticker, exchange)

        if newly_halted:
            await self.notifier.send(
                f"⛔ [{ticker}] 신규 주문을 정지했습니다.\n"
                f"증권사 앱에서 실제 잔고를 확인한 뒤,\n"
                f"필요하면 /fix 로 보정하고 /unhalt {ticker} 로 해제하세요."
            )

    async def _cancel_stale_orders(self, ticker: str, exchange: str) -> None:
        """봇이 낸 미체결 주문을 모두 취소한다 (분할·병합 직후)."""
        ours = self.registry.bot_live_orders(ticker)
        if not ours:
            return
        try:
            live = await self.kiwoom.get_open_orders(ticker, exchange)
        except Exception as e:
            logger.warning("[%s] 미체결 조회 실패 — 수동 취소가 필요합니다: %s", ticker, e)
            await self.notifier.send(
                f"⚠️ [{ticker}] 분할·병합 후 미체결을 조회하지 못했습니다. "
                f"증권사 앱에서 남은 주문을 직접 취소하세요.")
            return

        done, failed = 0, []
        for row in live:
            no = str(row.get("ord_no", ""))
            rec = ours.get(no)
            if rec is None:
                continue
            try:
                await self.kiwoom.cancel(no, ticker, exchange)
                self.registry.set_status(rec.id, "cancelled", "분할·병합으로 취소")
                done += 1
            except Exception as e:
                failed.append(f"{rec.qty}주 @{rec.price:.2f} — {str(e)[:40]}")

        lines = [f"🗑 [{ticker}] 분할·병합으로 옛 주문 {done}건을 취소했습니다."]
        if failed:
            lines.append(f"  ⚠️ 취소 실패 {len(failed)}건 — 앱에서 직접 취소하세요")
            lines += [f"    {f}" for f in failed]
        await self.notifier.send("\n".join(lines))

    # ------------------------------------------------------------
    # 시세
    # ------------------------------------------------------------

    async def _market_snapshot(self, ticker: str, mode: str, quote: dict = None) -> MarketSnapshot:
        exchange = exchange_of(ticker)
        quote = quote or await self.kiwoom.get_quote(ticker, exchange)
        closes: list[float] = []
        if mode == "reverse":
            # 리버스 별지점 = 직전 5거래일 종가 평균
            closes = await self.kiwoom.get_recent_closes(ticker, exchange, 5)
        return MarketSnapshot(
            prev_close=quote["prev_close"],
            current_price=quote["cur_price"],
            recent_closes=closes,
        )

    # ------------------------------------------------------------
    # 유지보수
    # ------------------------------------------------------------

    async def _refresh_token(self) -> None:
        try:
            await self.kiwoom._ensure_token()
        except Exception as e:
            logger.exception("토큰 갱신 실패")
            await self.notifier.send(f"⚠ 키움 토큰 갱신 실패: {e}")

    async def _account_available(self):
        """계좌의 매수 가능 금액(미수불가). 조회 실패하면 None.

        주문가능금액은 계좌 단위라 종목과 무관하다. 조회 API 가 종목과
        가격을 요구해서 운용 중인 첫 종목으로 조회한다.
        """
        tickers = self.state_mgr.list_tickers()
        if not tickers:
            return None
        t = tickers[0]
        try:
            q = await self.kiwoom.get_quote(t, exchange_of(t))
            ref = (q["prev_close"] or q["cur_price"]) * 1.15
            return await self.kiwoom.available_usd(t, exchange_of(t), ref)
        except Exception as e:
            logger.warning("매수 가능 금액 조회 실패: %s", e)
            return None

    async def _funding_reminder(self, session=None) -> None:
        """주문 1시간 전, 달러가 모자라면 알린다. 종목 합계로 비교한다."""
        from core.funding import FundingCheck, account_summary
        available = await self._account_available()
        if available is None:
            return
        from core.fx import safe_rate
        rate = safe_rate(self.state_mgr)
        needs, cores = {}, {}
        for t in self.state_mgr.list_tickers():
            st = self.state_mgr.get_state(t)
            if (st is not None and not getattr(st, "halted", False)
                    and not getattr(st, "awaiting_principal", False)):
                needs[t] = st.next_buy_need
                cores[t] = _core_or_full(st)
        chk = FundingCheck(need=sum(needs.values()), available=available,
                           core_need=sum(cores.values()), krw_rate=rate)
        if chk.ok:
            return
        tail = ("입금하지 않으면 별지점·평단 매수는 들어가고 폭락대비만 일부 빠집니다."
                if chk.core_ok else
                "입금하지 않으면 우선순위대로 들어갈 수 있는 매수만 접수합니다.\n"
                "(별지점·평단 먼저, 폭락대비는 그다음)")
        await self.notifier.send(
            "⏰ 1시간 뒤 주문 접수 — 주문가능금액(원화 포함)이 부족합니다.\n\n"
            + account_summary(needs, available, cores, krw_rate=rate)
            + "\n\n" + tail)

    async def send_weekly(self, today: Optional[date] = None) -> None:
        """주간 성과 요약"""
        from core.weekly import weekly_report
        try:
            await self.notifier.send(weekly_report(
                self.state_mgr, self.registry, today or datetime.now(KST).date()))
        except Exception as e:
            logger.exception("주간 요약 생성 실패")
            await self.notifier.send(f"⚠️ 주간 요약 생성 실패: {e}")

    async def send_backup(self, reason: str = "주간") -> bool:
        """장부(data/)를 묶어 텔레그램 파일로 보낸다. 성공하면 True."""
        from core.backup import make_backup
        sender = getattr(self.notifier, "send_document", None)
        if sender is None:
            logger.warning("파일을 보낼 수 없는 알림 채널이라 백업을 건너뜁니다")
            return False
        try:
            res = make_backup(self.state_mgr.data_dir)
        except Exception as e:
            logger.exception("장부 백업 생성 실패")
            await self.notifier.send(f"⚠️ 장부 백업을 만들지 못했습니다: {e}")
            return False
        try:
            ok = await sender(res.path, f"[{reason}] " + res.caption())
            if ok:
                logger.info("장부 백업 전송 (%s, %d파일, %dB)", reason, res.files, res.size)
            return ok
        finally:
            res.path.unlink(missing_ok=True)

    async def _morning_brief(self) -> None:
        """아침 요약. 어제 결과와 오늘 예정을 한 번 더 알린다."""
        from core.ops import morning_brief
        try:
            avail = await self._account_available()
            await self.notifier.send(
                morning_brief(self.state_mgr, self.registry, self, avail))
        except Exception as e:
            logger.exception("아침 요약 생성 실패")
            await self.notifier.send(f"⚠️ 아침 요약 생성 실패: {e}")

    def _cycle_end_text(self, ticker: str, stats, state) -> str:
        from core.tchart import t_values_between
        from eod.stats import format_cycle_end
        c = stats.cycles[-1]
        try:
            tv = t_values_between(self.state_mgr, ticker, c.start_date, c.end_date)
            tv = tv[:-1] if len(tv) > 1 and tv[-1] == 0 else tv    # 마지막 날은 T 0
        except Exception:
            tv = []
        return format_cycle_end(stats, principal=state.principal,
                                division=state.division, t_values=tv)

    async def _heartbeat(self) -> None:
        # async 로 둔다. 동기 함수면 APScheduler 가 별도 스레드에서 돌려,
        # 이벤트 루프가 멈춰도 신호가 계속 나간다.
        self._write_heartbeat()

    def _write_heartbeat(self) -> None:
        from core.heartbeat import write_heartbeat
        try:
            write_heartbeat(self.state_mgr.data_dir)
        except Exception as e:
            logger.warning("생존 신호 기록 실패: %s", e)

    async def _health_check(self) -> None:
        try:
            await self.kiwoom.get_deposit_usd()
        except Exception as e:
            logger.warning("헬스체크 실패: %s", e)
            await self.notifier.send(f"⚠ 키움 API 응답 없음: {e}")

    # ------------------------------------------------------------
    # 수동 실행 (텔레그램 명령용)
    # ------------------------------------------------------------

    async def run_now(self, what: str) -> str:
        """텔레그램에서 즉시 실행. what: plan / submit / verify / eod"""
        session = self._current.session if self._current else upcoming_session()
        async def _submit_all():
            # /run submit 이 LOC 만 내면 목표 지정가매도가 빠진다.
            # 프리장 창구와 본장 창구를 모두 실행한다.
            await self.submit_target_sells(session)
            await self.submit_loc_orders(session)

        actions = {
            "plan": self.plan_day,
            "submit": _submit_all,
            "target": lambda: self.submit_target_sells(session),
            "loc": lambda: self.submit_loc_orders(session),
            "verify": lambda: self.verify_reserved(session),
            "eod": lambda: self.run_eod(session),
        }
        fn = actions.get(what)
        if fn is None:
            return (f"알 수 없는 작업: {what}\n"
                    f"  plan / submit / target / loc / verify / eod")
        await fn()
        return f"{what} 실행 완료"

    def preview(self) -> str:
        """다음 세션 일정 미리보기"""
        session = upcoming_session()
        return describe(session) + "\n\n" + DaySchedule.build(session).describe()
