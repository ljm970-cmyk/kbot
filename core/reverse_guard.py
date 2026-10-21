"""
================================================================
리버스 대비

리버스모드는 지금까지 실전에서 한 번도 돌지 않았다. 처음 겪는 날 문제가
나면 가장 비싼 구간이라 세 가지를 둔다.

1. 접근 알림
   일반모드 T 가 (분할 − 5) 이상이면 아침 요약에 "리버스까지 N" 을 띄우고,
   정산에서 그 선을 처음 넘는 날 따로 알린다. 40분할이면 T 35.

2. MOC 대체 주문
   리버스 첫날은 MOC 매도(예약주문)를 쓴다. 증권사가 이걸 거부하면
   (접수 즉시든, 22:30 실주문 전환 때든) 그날 매도가 통째로 빠진다.
   거부를 확인하면 바로 같은 수량을 아주 낮은 가격의 LOC 매도로 다시 낸다.
   LOC 는 종가가 주문가 이상이면 종가에 체결되므로, 주문가를 충분히 낮게
   잡으면 MOC 와 같은 효과다. (실측 2026-09-22: 현재가 −29% LOC 도 접수됨)

3. MOC 경로 점검 (/check moc go)
   1주짜리 MOC 예약매도를 실제로 넣고, 조회로 확인한 뒤 바로 취소한다.
   예약 취소 가능 시간(08:00~22:25 KST) 안에서만, 22:10 이전에만 돈다.
   증권사가 MOC 예약 접수 자체를 받아주는지(코드·형식)를 확인한다.
   22:30 실주문 전환 때의 거부는 이 점검으로 알 수 없다 — 그건 2번이 막는다.
================================================================
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

#: 리버스 접근 경고 시작 — 분할 − 이 값
APPROACH_GAP = 5
#: MOC 대체 LOC 매도가 = 기준가 × 이 값 (25% 아래)
FALLBACK_FLOOR = 0.75
#: MOC 점검은 이 시각(KST) 이전에만 — 예약 취소 마감 22:25 전에 여유를 둔다
CHECK_DEADLINE = (22, 10)


# ================================================================
# 1. 접근 알림
# ================================================================

def approach_threshold(division: int) -> float:
    return float(division - APPROACH_GAP)


def exhaust_line(division: int) -> float:
    """일반모드 소진 기준 — T 가 이 값을 넘으면 리버스 (방법론: T > 분할 − 1)"""
    return float(division - 1)


def approach_text(state) -> str:
    """아침 요약용 한 줄. 해당 없으면 빈 문자열."""
    if getattr(state, "mode", "normal") != "normal":
        return ""
    d = int(getattr(state, "division", 0) or 0)
    if d <= 0 or state.T < approach_threshold(d):
        return ""
    gap = max(exhaust_line(d) - state.T, 0.0)
    days = max(math.ceil(gap), 1)
    return (f"⚠️ 리버스 접근 — 소진 기준(T>{exhaust_line(d):.0f})까지 {gap:.1f} · "
            f"빠르면 {days}거래일\n"
            f"     첫날은 MOC 매도입니다. 미리 /check moc 로 경로를 점검해 두세요.")


def crossed_approach(t_before: float, t_after: float, division: int, mode: str) -> bool:
    if mode != "normal" or division <= 0:
        return False
    th = approach_threshold(division)
    return t_before < th <= t_after


def crossing_alert(ticker: str, state) -> str:
    d = int(state.division)
    gap = max(exhaust_line(d) - state.T, 0.0)
    return (f"🟠 [{ticker}] 리버스 접근 — T {state.T:.2f} / {d}\n"
            f"  소진 기준(T>{exhaust_line(d):.0f})까지 {gap:.1f} 남았습니다.\n"
            f"  리버스 첫날은 MOC 매도(예약주문)로 나갑니다. 거부되면 봇이\n"
            f"  낮은 가격 LOC 매도로 바로 대체합니다.\n"
            f"  미리 확인하려면 /check moc (08:00~22:10 사이).")


# ================================================================
# 2. MOC 대체 주문
# ================================================================

def fallback_price(ref_price: float) -> float:
    return math.floor(ref_price * FALLBACK_FLOOR * 100) / 100


def fallback_order(order, ref_price: float):
    """MOC 매도 → 같은 태그·수량의 낮은 가격 LOC 매도"""
    from dataclasses import replace
    from kiwoom.constants import TradeType
    return replace(order, trade_type=TradeType.LOC, price=fallback_price(ref_price),
                   note=(order.note + " · MOC 거부 대체").strip(" ·"))


# ================================================================
# 3. MOC 경로 점검
# ================================================================

def check_window_open(now: datetime | None = None) -> tuple[bool, str]:
    now = now or datetime.now(KST)
    if now.hour < 8:
        return False, "예약주문 취소는 08:00 부터 됩니다. 08:00~22:10 사이에 다시 실행하세요."
    if (now.hour, now.minute) >= CHECK_DEADLINE:
        return False, ("22:10 이후에는 하지 않습니다 (예약 취소 마감 22:25 전에 여유를 둡니다). "
                       "내일 08:00 이후에 실행하세요.")
    return True, ""


def check_intro(ticker: str) -> str:
    return (f"🧪 MOC 경로 점검 — {ticker}\n\n"
            f"  1. {ticker} 1주 MOC 예약매도를 실제로 넣습니다\n"
            f"  2. 증권사 예약 목록에 정상으로 올라왔는지 확인합니다\n"
            f"  3. 바로 취소하고, 취소됐는지 다시 확인합니다\n\n"
            f"  취소에 실패하면 22:30 에 1주가 종가에 팔릴 수 있습니다.\n"
            f"  그 경우 바로 알려 드리니 22:25 전에 앱에서 취소하세요.\n"
            f"  (봇 장부에는 기록하지 않습니다)\n\n"
            f"  실행: /check moc go {ticker}")


async def run_moc_check(kiwoom, ticker: str, exchange: str, now: datetime | None = None) -> str:
    ok, why = check_window_open(now)
    if not ok:
        return f"⏸ MOC 점검을 하지 않았습니다.\n  {why}"

    L = [f"🧪 MOC 경로 점검 — {ticker}"]

    # 1) 접수
    try:
        res = await kiwoom.reserve_moc_sell(ticker, exchange, 1)
    except Exception as e:
        L += [f"  ❌ 접수 거부: {e}",
              "",
              "  증권사가 MOC 예약매도를 받지 않습니다.",
              "  리버스 첫날 MOC 가 거부되면 봇이 낮은 가격 LOC 매도로 자동 대체하므로",
              "  매도가 빠지지는 않습니다. 이 결과를 개발 쪽에 알려 주세요."]
        return "\n".join(L)
    no = str(getattr(res, "rsrv_ord_no", "") or "")
    if not no:
        L += ["  ⚠️ 접수 응답에 예약번호가 없습니다 — 취소할 대상을 특정할 수 없습니다.", "",
              "  🚨 점검용 1주 MOC 예약이 들어갔을 수 있습니다!",
              f"  22:25 전에 영웅문 앱 → 예약주문 내역에서 {ticker} MOC 1주를 확인하고 취소하세요."]
        return "\n".join(L)
    L.append(f"  ✅ 접수됨 — 예약번호 {no}")

    # 2) 조회
    row = None
    try:
        rows = await kiwoom.get_reserved_orders(ticker=ticker, exchange=exchange,
                                                include_cancelled=True)
        row = next((r for r in rows if str(r.get("rsrv_ord_no", "")) == no), None)
    except Exception as e:
        L.append(f"  ⚠️ 예약 목록 조회 실패: {e}")
    if row is not None:
        err = str(row.get("err_cntn") or "").strip()
        proc = str(row.get("proc_tp") or "")
        if err or "에러" in proc:
            L.append(f"  ❌ 예약 목록에 에러로 표시: {err or proc}")
        else:
            L.append(f"  ✅ 예약 목록 확인 — {proc or '미처리'} "
                     f"({row.get('ord_qty', 1)}주, 유형 {row.get('trde_tp', 'MOC')})")
    elif no:
        L.append("  ⚠️ 예약 목록에서 찾지 못했습니다")

    # 3) 취소
    rsrv_dt = str((row or {}).get("rsrv_dt") or getattr(res, "frcs_dt", "")
                  or (now or datetime.now(KST)).strftime("%Y%m%d"))
    cancelled = False
    try:
        await kiwoom.cancel_reserved(rsrv_dt, no, ticker, exchange)
        rows = await kiwoom.get_reserved_orders(ticker=ticker, exchange=exchange,
                                                include_cancelled=True)
        again = next((r for r in rows if str(r.get("rsrv_ord_no", "")) == no), None)
        flag = str((again or {}).get("rsrv_cncl_yn", ""))
        # 목록에서 사라진 것을 취소로 보는 건, 취소 전에 목록에서 찾았던 경우만
        cancelled = bool((again is None and row is not None)
                         or (flag and not flag.startswith("미")))
    except Exception as e:
        L.append(f"  ❌ 취소 실패: {e}")

    if cancelled:
        L += ["  ✅ 취소 확인", "",
              "  MOC 예약 접수 경로는 정상입니다.",
              "  (22:30 실주문 전환 때 거부되면 봇이 LOC 매도로 자동 대체합니다)"]
    else:
        L += ["", "  🚨 점검용 1주 MOC 예약이 남아 있을 수 있습니다!",
              f"  22:25 전에 영웅문 앱 → 예약주문 내역에서 {ticker} MOC 1주를 취소하세요.",
              "  취소하지 못해 팔리면 다음 정산에서 '장부 불일치' 로 알려 드립니다."]
    return "\n".join(L)
