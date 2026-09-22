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
        self.scheduler.start()
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
        await self._submit(session, SubmitWindow.REGULAR, "LOC 예약")

    async def _submit(self, session: date, window: str, label: str) -> None:
        trade_date = session.strftime("%Y%m%d")
        for ticker in self.state_mgr.list_tickers():
            try:
                await self._submit_ticker(ticker, trade_date, window, label)
            except Exception as e:
                logger.exception("%s %s 접수 실패", ticker, label)
                await self.notifier.send(f"⚠ [{ticker}] {label} 접수 실패: {e}")

    async def _submit_ticker(self, ticker: str, trade_date: str, window: str, label: str) -> None:
        state = self.state_mgr.get_state(ticker)
        if state is None:
            return

        # 회로차단기 — 장부가 어긋난 상태로는 주문을 내지 않는다
        if CircuitBreaker.is_halted(state):
            logger.warning("[%s] 주문 정지 중 — %s 접수를 건너뜁니다 (%s)",
                           ticker, label, state.halt_reason)
            if window == SubmitWindow.PRE_MARKET:
                await self.notifier.send(
                    f"[{ticker}] 주문 정지 중이라 접수하지 않았습니다.\n"
                    f"  사유: {state.halt_reason}\n"
                    f"  확인 후 /unhalt {ticker} 로 해제하세요."
                )
            return

        market = await self._market_snapshot(ticker, state.mode)
        mode = ReverseMode(state) if state.mode == "reverse" else NormalMode(state)
        plan = mode.plan(market)

        orders = self._orders_for_window(plan, window)
        if not orders:
            return

        if plan.warnings and window == SubmitWindow.PRE_MARKET:
            await self.notifier.send(f"[{ticker}] " + "\n".join(plan.warnings))

        # 증권사 앱에서 직접 취소한 LOC 를 원장에 반영한다.
        # 반영하지 않으면 원장엔 "접수됨" 으로 남아, 다시 낼 때 아래 중복
        # 방지가 막는다.
        if not self.config.dry_run:
            await self._sync_cancelled_loc(ticker, trade_date)

        # 중복 접수 방지 — 같은 날 같은 주문을 두 번 내면 포지션이 두 배가 된다.
        #
        # 창구 단위가 아니라 주문 단위로 걸러낸다. 자금 부족으로 매수만
        # 건너뛴 날, 입금 후 /run loc 로 다시 실행하면 이미 낸 매도는 빼고
        # 매수만 접수되어야 한다.
        dup = set(self.registry.already_submitted(
            trade_date, ticker, [o.tag for o in orders]))
        if dup:
            skipped = [o for o in orders if o.tag in dup]
            orders = [o for o in orders if o.tag not in dup]
            logger.info("[%s] %s 중 이미 접수된 %d건 제외 (%s)",
                        ticker, label, len(skipped), ", ".join(sorted(dup)))
            if not orders:
                await self.notifier.send(
                    f"[{ticker}] {label} 는 모두 이미 접수돼 있어 건너뛰었습니다.")
                return

        # 자금 점검 — 매수만 해당. 매도는 돈이 없어도 낸다.
        buys = [o for o in orders if o.side == "buy"]
        if buys:
            from core.funding import FundingCheck
            need = round(sum(o.amount for o in buys)
                         * (1 + state.fee_rate + 0.005), 2)
            try:
                ref_price = max(o.price or 0 for o in buys) or market.current_price
                available = await self.kiwoom.available_usd(
                    ticker, exchange_of(ticker), ref_price)
            except Exception as e:
                logger.warning("[%s] 매수 가능 금액 확인 실패: %s", ticker, e)
                available = None

            if available is not None:
                chk = FundingCheck(need=need, available=available)
                if not chk.ok:
                    orders = [o for o in orders if o.side != "buy"]
                    await self.notifier.send(
                        f"[{ticker}] 달러가 부족해 오늘 매수를 건너뜁니다.\n"
                        f"{chk.topup_text()}\n\n"
                        + ("  매도 주문은 그대로 접수합니다.\n" if orders else "")
                        + f"  입금한 뒤 /run loc 로 매수만 다시 접수할 수 있습니다\n"
                        f"  (장 시작 전까지).")
                    if not orders:
                        return

        if self.config.dry_run:
            lines = "\n".join(f"  {o}" for o in orders)
            await self.notifier.send(
                f"[DRY RUN] {ticker} {label}\n{plan.summary().splitlines()[0]}\n{lines}")
            return

        exchange = exchange_of(ticker)
        submitted = 0
        for order in orders:
            record_id = self.registry.record_submission(trade_date, ticker, order)
            try:
                res = await self._place(ticker, exchange, order)
                # 실시간 주문은 주문번호, 예약주문은 예약번호를 남긴다
                if getattr(res, "rsrv_ord_no", ""):
                    self.registry.attach_rsrv_ord_no(record_id, res.rsrv_ord_no)
                elif getattr(res, "ord_no", ""):
                    self.registry.attach_ord_no(record_id, res.ord_no)
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
                return
            except KiwoomAPIError as e:
                self.registry.set_status(record_id, "rejected", str(e))
                await self.notifier.send(f"⚠ [{ticker}] {order} 접수 거부: {e.message}")
            except Exception as e:
                # 전송 자체가 안 된 경우(값 검증 실패 등). 원장에 '접수됨' 으로
                # 남겨두면 나가지도 않은 주문이 다른 체결을 가로챈다.
                logger.exception("[%s] 주문 전송 실패", ticker)
                self.registry.set_status(record_id, "rejected", f"전송 실패: {e}")
                await self.notifier.send(f"⚠ [{ticker}] {order} 전송 실패: {e}")

        logger.info("[%s] %s %d/%d건 접수", ticker, label, submitted, len(orders))
        if submitted == 0 and orders:
            await self.notifier.send(
                f"⚠ [{ticker}] {label} {len(orders)}건이 모두 실패했습니다. "
                f"증권사 앱에서 주문 상태를 확인하세요.")

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
                lines = [f"⚠ [{ticker}] 예약주문 {len(bad)}건 거부"]
                for b in bad:
                    self.registry.mark_rejected_by_rsrv_no(b["rsrv_ord_no"], b["reason"])
                    lines.append(f"  {b['ord_qty']}주 @{b['ord_uv']:.2f} — "
                                 f"{b['reason'] or b.get('proc_tp') or '사유 미상'}")
                await self.notifier.send("\n".join(lines))

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

        gone = [r for r in recs if r.ord_no not in live]
        for r in gone:
            self.registry.set_status(r.id, "cancelled", "증권사 미체결에서 사라짐 (직접 취소)")
        if gone:
            logger.info("[%s] 직접 취소된 LOC %d건 원장 반영", ticker, len(gone))
            await self.notifier.send(
                f"[{ticker}] 앱에서 취소된 LOC {len(gone)}건을 확인했습니다. "
                f"다시 접수합니다.")
        return len(gone)

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

        missing = [r for no, r in ours.items()
                   if no not in live and r.trade_type == TradeType.LOC]
        if not missing:
            return
        lines = [f"⚠ [{ticker}] LOC 주문 {len(missing)}건이 미체결에서 사라졌습니다",
                 "  종가 전에는 체결될 수 없으니 거부·취소됐을 가능성이 큽니다."]
        for r in missing:
            side = "매수" if r.side == "buy" else "매도"
            lines.append(f"  {side} {r.qty}주 @{r.price:.2f} [{r.tag}]")
        lines.append("  키움 앱 주문내역에서 사유를 확인하세요.")
        await self.notifier.send("\n".join(lines))

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
                await self.notifier.send(f"⚠ [{ticker}] EOD 정산 실패: {e}")
                # EOD 가 실패하면 T값·평단이 갱신되지 않은 채 다음날 주문이 나간다.
                # 연속 실패하면 멈춘다.
                try:
                    with self.state_mgr.edit_state(ticker) as state:
                        halted = CircuitBreaker.record_eod_failure(state, str(e))
                    if halted:
                        await self.notifier.send(
                            f"[{ticker}] EOD 연속 실패로 주문을 정지했습니다.\n"
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
                f"[{ticker}] WebSocket 이 놓친 체결 {len(missed)}건을 API 조회로 보완했습니다")

        quote = await self.kiwoom.get_quote(ticker, exchange)
        close_price = quote["cur_price"] or quote["prev_close"]

        with self.state_mgr.edit_state(ticker) as state:
            next_market = await self._market_snapshot(ticker, state.mode, quote=quote)
            result = self.eod.run(state, trade_date, fills, close_price, next_market)
            self.state_mgr.archive_eod(ticker, trade_date, {
                "T": state.T, "holdings": state.holdings,
                "avg_price": state.avg_price, "cash": state.cash,
                "mode": state.mode, "close": close_price,
                "fills": result.fills_applied, "realized_pl": result.realized_pl,
            })
            CircuitBreaker.record_eod_success(state)
            try:
                stats = collect_stats(self.state_mgr, ticker)
            except Exception:
                logger.exception("[%s] 통계 집계 실패", ticker)
                stats = None
            report = daily_report(state, self.registry, result, stats=stats)

        await self.notifier.send(report)

        # 장부 대조 — 어긋나면 다음 거래일 주문을 멈춘다
        await self._reconcile_ticker(ticker, exchange, result)

        if result.anomalies or result.unmatched_fills:
            await self.notifier.send(
                f"⚠ [{ticker}] 수동 확인이 필요합니다\n" + self.registry.day_summary(trade_date, ticker))

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
        if newly_halted:
            await self.notifier.send(
                f"[{ticker}] 신규 주문을 정지했습니다.\n"
                f"증권사 앱에서 실제 잔고를 확인한 뒤,\n"
                f"필요하면 /fix 로 보정하고 /unhalt {ticker} 로 해제하세요."
            )

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
        needs = {}
        for t in self.state_mgr.list_tickers():
            st = self.state_mgr.get_state(t)
            if st is not None and not getattr(st, "halted", False):
                needs[t] = st.next_buy_need
        if FundingCheck(need=sum(needs.values()), available=available).ok:
            return
        await self.notifier.send(
            "1시간 뒤 주문 접수 — 달러가 부족합니다.\n\n"
            + account_summary(needs, available)
            + "\n\n입금하지 않으면 모자란 만큼 매수를 건너뛰고 매도만 접수합니다.")

    async def _morning_brief(self) -> None:
        """아침 요약. 어제 결과와 오늘 예정을 한 번 더 알린다."""
        from core.ops import morning_brief
        try:
            avail = await self._account_available()
            await self.notifier.send(
                morning_brief(self.state_mgr, self.registry, self, avail))
        except Exception as e:
            logger.exception("아침 요약 생성 실패")
            await self.notifier.send(f"⚠ 아침 요약 생성 실패: {e}")

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
