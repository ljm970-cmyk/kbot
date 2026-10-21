"""
================================================================
실시간 체결 알림

봇은 WebSocket(F5)으로 체결을 실시간으로 받지만 정산에만 쓰고 알리지
않았다. 체결 여부는 다음 날 정산 리포트에서야 알 수 있었다.

체결이 들어오면 잠깐(20초) 모았다가 종목별로 한 번에 보낸다. LOC 는
종가에 한꺼번에 체결되므로 하나씩 보내면 알림이 여러 개 울린다.

  ⚡ [SOXL] 체결 2건
    🔴 별지점 매수 1주 @146.25 (주문 @175.93)
    🔵 쿼터매도 2주 @175.94

장부(T값·평단)는 지금처럼 정산 때 반영한다. 이 알림은 보기만 한다.
================================================================
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from core.t_calculator import FillKind

logger = logging.getLogger(__name__)

LABELS = {
    FillKind.STAR_BUY: "별지점 매수",
    FillKind.AVG_BUY: "평단 매수",
    FillKind.HALF_BUY: "후반전 매수",
    FillKind.CRASH_BUY: "폭락대비 매수",
    FillKind.ENTRY_BUY: "처음매수",
    FillKind.GUARD_BUY: "대체 매수",
    FillKind.MERGED_BUY: "별지점·평단 매수",
    FillKind.QUARTER_SELL: "쿼터매도",
    FillKind.TARGET_SELL: "목표매도",
    FillKind.REVERSE_MOC_SELL: "리버스 MOC 매도",
    FillKind.REVERSE_SELL: "리버스 매도",
    FillKind.REVERSE_QUARTER_BUY: "리버스 쿼터매수",
}


def describe(fill, rec) -> str:
    side_mark = "🔴" if fill.side == "buy" else "🔵"
    side_word = "매수" if fill.side == "buy" else "매도"
    if rec is None:
        name = f"👤 직접 주문 {side_word}"
        side_mark = ""
    else:
        name = LABELS.get(rec.tag, rec.tag)
    line = f"  {side_mark + ' ' if side_mark else ''}{name} {fill.fill_qty}주 @{fill.fill_price:.2f}"
    op = getattr(fill, "order_price", 0.0) or 0.0
    if op and abs(op - fill.fill_price) >= 0.005:
        line += f" (주문 @{op:.2f})"
    if "부분" in str(getattr(fill, "status", "")):
        line += " · 부분체결"
    return line


def build_message(ticker: str, items: list) -> str:
    """items: [(fill, rec)]"""
    L = [f"⚡ [{ticker}] 체결 {len(items)}건"]
    L += [describe(f, r) for f, r in items]
    tags = {r.tag for _, r in items if r is not None}
    if FillKind.TARGET_SELL in tags:
        L.append("  🎯 목표매도 체결 — 전량 팔리면 사이클 종료 (정산에서 결산을 보내 드립니다)")
    if FillKind.REVERSE_MOC_SELL in tags:
        L.append("  🔄 리버스 첫날 매도 체결")
    L.append("  장부(T·평단)는 정산 때 반영합니다.")
    return "\n".join(L)


class FillAlerter:
    """WebSocketFillReceiver.add_callback() 에 on_fill 을 연결한다"""

    def __init__(self, registry, notifier_getter, delay: float = 20.0):
        self.registry = registry
        self.notifier_getter = notifier_getter
        self.delay = delay
        self._buf: list = []
        self._task: asyncio.Task | None = None

    def on_fill(self, fill) -> None:
        self._buf.append(fill)
        if self._task is None or self._task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._task = loop.create_task(self._later())

    async def _later(self) -> None:
        # 보내는 동안 새로 들어온 체결도 놓치지 않게, 버퍼가 빌 때까지 돈다
        while True:
            await asyncio.sleep(self.delay)
            await self.flush()
            if not self._buf:
                return

    async def flush(self) -> None:
        buf, self._buf = self._buf, []
        if not buf:
            return
        by_ticker: dict[str, list] = defaultdict(list)
        for f in buf:
            try:
                rec = self.registry.by_ord_no(f.ord_no) if f.ord_no else None
            except Exception:
                logger.exception("체결 주문 조회 실패 %s", f.ord_no)
                rec = None
            by_ticker[f.ticker].append((f, rec))
        notifier = self.notifier_getter()
        if notifier is None:
            return
        for ticker, items in by_ticker.items():
            try:
                await notifier.send(build_message(ticker, items))
            except Exception:
                logger.exception("체결 알림 전송 실패")
