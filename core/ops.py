"""
================================================================
운영 상태 점검 · 긴급 정지

/health  봇이 살아 있고 제대로 돌고 있는지 한 화면에
/panic   전 종목 정지 + 봇이 낸 예약주문 전부 취소

/halt 과의 차이
    /halt   신규 주문만 막는다. 접수된 예약주문은 그대로 살아 있다.
    /panic  거기에 더해 봇이 낸 예약주문과 실시간 주문을 취소한다.

계좌의 모든 예약주문을 취소하지 않는다. 사용자가 직접 건 주문은
원장에 없으므로 대상에서 빠진다.
================================================================
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("kbot.ops")
KST = ZoneInfo("Asia/Seoul")

#: 프로세스 시작 시각. main 에서 덮어쓴다.
_STARTED_AT: Optional[datetime] = None


def mark_started() -> None:
    global _STARTED_AT
    _STARTED_AT = datetime.now(KST)


def uptime_text() -> str:
    if _STARTED_AT is None:
        return "알 수 없음"
    sec = int((datetime.now(KST) - _STARTED_AT).total_seconds())
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}일 {h}시간 {m}분"
    if h:
        return f"{h}시간 {m}분"
    return f"{m}분"


def _ago(when: Optional[datetime]) -> str:
    if when is None:
        return "없음"
    sec = int((datetime.now(KST) - when).total_seconds())
    if sec < 60:
        return f"{sec}초 전"
    if sec < 3600:
        return f"{sec // 60}분 전"
    return f"{sec // 3600}시간 {(sec % 3600) // 60}분 전"


# ================================================================
# /health
# ================================================================

def health_report(config, kiwoom, state_mgr, scheduler=None, ws=None) -> str:
    """운영 상태 요약.

    SSH 로 들어가지 않고도 봇이 살아 있는지, 토큰이 유효한지,
    체결 수신이 끊기지 않았는지 확인할 수 있어야 한다.
    """
    mode = "모의투자" if config.kiwoom.mock else "실계좌"
    if config.dry_run:
        mode += " · DRY RUN(주문 미전송)"

    L = [f"kbot 상태 — {mode}", "", f"  가동    {uptime_text()}"]

    # 토큰
    exp = getattr(kiwoom, "token_expiry", None)
    if exp:
        left = int((exp - datetime.now()).total_seconds())
        if left > 0:
            L.append(f"  토큰    {exp:%m/%d %H:%M} 만료 "
                     f"(남은 {left // 3600}시간 {(left % 3600) // 60}분)")
        else:
            L.append(f"  토큰    만료됨 — 다음 요청 시 갱신")
    else:
        L.append("  토큰    미발급")

    # WebSocket
    if ws is not None:
        try:
            L.append(f"  체결수신 {ws.health()}")
        except Exception as e:
            L.append(f"  체결수신 확인 실패 ({e})")
    else:
        L.append("  체결수신 미연결")

    # 다음 작업
    if scheduler is not None:
        nxt = []
        for job in getattr(scheduler.scheduler, "get_jobs", lambda: [])():
            t = getattr(job, "next_run_time", None)
            if t:
                nxt.append((t, job.id))
        if nxt:
            t, jid = min(nxt)
            L.append(f"  다음작업 {jid} {t:%m/%d %H:%M}")
        else:
            L.append("  다음작업 없음")

    # 종목별
    L.append("")
    tickers = state_mgr.list_tickers()
    if not tickers:
        L.append("  설정된 종목 없음")
    for t in tickers:
        st = state_mgr.get_state(t)
        if st is None:
            continue
        flag = " [정지]" if getattr(st, "halted", False) else ""
        L.append(f"  [{t}]{flag} {st.mode} T={st.T:.4f} "
                 f"{st.holdings}주 잔금 ${st.cash:,.2f}")
        last = getattr(st, "last_eod_date", "")
        L.append(f"     마지막 정산 {last or '없음'}")
        if getattr(st, "halted", False):
            L.append(f"     사유: {st.halt_reason}")

    return "\n".join(L)


# ================================================================
# /panic
# ================================================================

@dataclass
class PanicResult:
    halted: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped_not_ours: int = 0
    cancel_window_closed: bool = False

    def report(self) -> str:
        L = ["긴급 정지 실행"]
        L.append(f"  주문 정지  {', '.join(self.halted) or '없음'}")

        if self.cancelled:
            L.append(f"  취소 완료  {len(self.cancelled)}건")
            for c in self.cancelled[:10]:
                L.append(f"    {c}")
        else:
            L.append("  취소 완료  없음")

        if self.failed:
            L.append(f"  취소 실패  {len(self.failed)}건")
            for no, why in self.failed[:10]:
                L.append(f"    {no} — {why}")

        if self.skipped_not_ours:
            L.append(f"  건드리지 않음 {self.skipped_not_ours}건 "
                     f"(봇이 낸 주문이 아님)")

        if self.cancel_window_closed:
            L.append("")
            L.append("  ⚠ 지금은 예약주문 취소 가능 시간(08:00~22:25 KST)이")
            L.append("    아닙니다. 급하면 증권사 앱에서 직접 취소하세요.")

        L.append("")
        L.append("  신규 주문은 멈췄습니다. /unhalt <종목> 으로 해제합니다.")
        return "\n".join(L)


async def panic_stop(kiwoom, state_mgr, registry, exchange_of) -> PanicResult:
    """전 종목 정지 + 봇이 낸 예약주문 취소.

    계좌의 모든 예약주문을 취소하지 않는다. 원장에 기록된 예약번호와
    대조해, 봇이 낸 것만 취소한다. 사용자가 직접 건 주문은 그대로 둔다.
    """
    from core.reconciler import CircuitBreaker

    result = PanicResult()

    # 1) 먼저 멈춘다. 취소가 실패해도 신규 주문은 나가지 않아야 한다.
    for ticker in state_mgr.list_tickers():
        try:
            with state_mgr.edit_state(ticker) as st:
                CircuitBreaker.halt(st, "사용자가 /panic 으로 긴급 정지")
            result.halted.append(ticker)
        except Exception as e:
            logger.exception("[%s] 정지 실패", ticker)
            result.failed.append((ticker, f"정지 실패: {e}"))

    if not kiwoom.can_cancel_now():
        result.cancel_window_closed = True

    # 2) 봇이 낸 예약주문만 취소
    for ticker in state_mgr.list_tickers():
        try:
            ex = exchange_of(ticker)
            live = await kiwoom.get_reserved_orders(ticker=ticker, exchange=ex)
        except Exception as e:
            logger.exception("[%s] 예약주문 조회 실패", ticker)
            result.failed.append((ticker, f"조회 실패: {e}"))
            continue

        ours = registry.bot_reserved_orders(ticker)
        for row in live:
            no = str(row.get("rsrv_ord_no", ""))
            if no not in ours:
                result.skipped_not_ours += 1
                continue
            rsrv_dt = str(row.get("rsrv_dt", ""))
            label = (f"{ticker} {row.get('slby_tp', '')} "
                     f"{int(row.get('ord_qty') or 0)}주 @{row.get('ord_uv', '')}")
            try:
                await kiwoom.cancel_reserved(rsrv_dt, no, ticker, ex)
                registry.set_status(ours[no].id, "cancelled", "/panic 으로 취소")
                result.cancelled.append(label)
            except Exception as e:
                msg = str(e).split(":")[-1].strip()
                result.failed.append((label, msg[:60]))

    # 3) 봇이 낸 실시간 주문 취소 (프리장부터 거는 지정가매도).
    #    예약주문과 달리 취소 가능 시간 제약이 없다.
    for ticker in state_mgr.list_tickers():
        ours = registry.bot_live_orders(ticker)
        if not ours:
            continue
        try:
            ex = exchange_of(ticker)
            live = await kiwoom.get_open_orders(ticker, ex)
        except Exception as e:
            logger.exception("[%s] 미체결 조회 실패", ticker)
            result.failed.append((ticker, f"미체결 조회 실패: {e}"))
            continue
        for row in live:
            no = str(row.get("ord_no", ""))
            if no not in ours:
                continue
            label = (f"{ticker} {'매수' if row.get('side') == 'buy' else '매도'} "
                     f"{int(row.get('remain_qty') or row.get('ord_qty') or 0)}주 "
                     f"@{row.get('ord_uv', '')} (실시간)")
            try:
                await kiwoom.cancel(no, ticker, ex)
                registry.set_status(ours[no].id, "cancelled", "/panic 으로 취소")
                result.cancelled.append(label)
            except Exception as e:
                msg = str(e).split(":")[-1].strip()
                result.failed.append((label, msg[:60]))

    return result


# ================================================================
# 아침 요약
# ================================================================

def morning_brief(state_mgr, registry, scheduler=None, available=None) -> str:
    """08시에 보내는 요약.

    EOD 리포트는 새벽 05:30 에 오는데 그때는 대개 자고 있다.
    일어나서 읽을 수 있게 어제 결과와 오늘 예정을 다시 정리한다.
    """
    from core.market_calendar import DaySchedule, upcoming_session
    from eod.stats import collect

    L = [f"아침 요약 — {datetime.now(KST):%m/%d (%a)}", ""]

    tickers = state_mgr.list_tickers()
    if not tickers:
        return "\n".join(L + ["  설정된 종목이 없습니다."])

    for t in tickers:
        st = state_mgr.get_state(t)
        if st is None:
            continue
        flag = " [정지]" if getattr(st, "halted", False) else ""
        L.append(f"[{t}]{flag}")
        L.append(f"  T {st.T:.4f} · {st.holdings}주 @${st.avg_price:.2f} "
                 f"· 잔금 ${st.cash:,.2f}")

        last = getattr(st, "last_eod_date", "")
        if last:
            rows = registry.trade_history(t, days=400)
            same = [r for r in rows if r["date"] == last]
            if same:
                for r in same:
                    side = "매수" if r["side"] == "buy" else "매도"
                    L.append(f"  어제 {side} {r['qty']}주 @${r['avg_price']:.2f}")
            else:
                L.append("  어제 체결 없음")

        try:
            s = collect(state_mgr, t)
            if s.closed:
                L.append(f"  누적 {s.closed}사이클 · 승률 {s.win_rate:.0f}% "
                         f"· ${s.total_pnl:,.2f}")
        except Exception:
            pass

        # 오늘 입금 안내 — 매일 부족분을 채우는 운용용
        if available is not None and t in available:
            from core.funding import FundingCheck
            chk = FundingCheck(need=getattr(st, "next_buy_need", 0.0),
                               available=available[t])
            L.append("  오늘 매수 자금")
            L.append(chk.topup_text())

        if getattr(st, "halted", False):
            L.append(f"  ⚠ {st.halt_reason}")
        L.append("")

    try:
        sched = DaySchedule.build(upcoming_session())
        L.append(sched.describe())
    except Exception as e:
        L.append(f"일정 계산 실패: {e}")

    return "\n".join(L).rstrip()
