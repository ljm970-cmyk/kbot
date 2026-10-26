"""
================================================================
휴장·조기폐장 사전 안내

봇은 휴장일에 주문을 내지 않고, 조기폐장일에는 일정을 당겨서 처리한다.
사람은 그걸 모르면 "왜 주문 알림이 안 오지?" 하게 된다. 아침 요약 맨
위에 오늘 밤과 다음 거래일 밤의 특이사항을 미리 알린다.

  📅 오늘 밤 미국 휴장 (추수감사절) — 주문·정산 없음
  ⏰ 다음 거래일(11/27 금) 조기폐장 (추수감사절 다음날) — 마감 03:00 KST

날짜는 미국 거래일 기준이다. 한국 날짜 D 의 밤에 열리는 장이 미국 D 일 장.
================================================================
"""

from __future__ import annotations

from datetime import date, timedelta

from core.market_calendar import (
    _easter, _last_weekday, _nth_weekday, _observed,
    is_early_close, is_trading_day, market_close_kst,
)

WEEKDAY = "월화수목금토일"


def holiday_name(d: date) -> str:
    y = d.year
    names = {
        _observed(date(y, 1, 1)): "신정",
        _nth_weekday(y, 1, 0, 3): "마틴 루서 킹 데이",
        _nth_weekday(y, 2, 0, 3): "대통령의 날",
        _easter(y) - timedelta(days=2): "성금요일",
        _last_weekday(y, 5, 0): "메모리얼 데이",
        _observed(date(y, 6, 19)): "준틴스",
        _observed(date(y, 7, 4)): "독립기념일",
        _nth_weekday(y, 9, 0, 1): "노동절",
        _nth_weekday(y, 11, 3, 4): "추수감사절",
        _observed(date(y, 12, 25)): "성탄절",
    }
    # 다음 해 신정 대체휴일이 올해 12/31 로 오는 경우
    names.setdefault(_observed(date(y + 1, 1, 1)), "신정")
    return names.get(d, "휴장일")


def early_close_name(d: date) -> str:
    if d.month == 7:
        return "독립기념일 전날"
    if d.month == 11:
        return "추수감사절 다음날"
    if d.month == 12:
        return "성탄 전야"
    return "조기폐장"


def _next_weekday(d: date) -> date:
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return n


def _line(label: str, d: date) -> str:
    if d.weekday() >= 5:
        return ""
    if not is_trading_day(d):
        return f"📅 {label} 미국 휴장 ({holiday_name(d)}) — 주문·정산 없음"
    if is_early_close(d):
        close = market_close_kst(d)
        return (f"⏰ {label} 조기폐장 ({early_close_name(d)}) — "
                f"마감 {close:%H:%M} KST, LOC 접수·정산도 그에 맞춰 당겨집니다")
    return ""


def notice_lines(today: date) -> list[str]:
    """아침 요약 맨 위에 넣을 줄. 특이사항 없으면 빈 목록."""
    out = []
    first = _line("오늘 밤", today)
    if first:
        out.append(first)
    nxt = _next_weekday(today)
    label = ("내일 밤" if nxt == today + timedelta(days=1)
             else f"다음 평일({nxt.month}/{nxt.day} {WEEKDAY[nxt.weekday()]}) 밤")
    second = _line(label, nxt)
    if second:
        out.append(second)
    return out
