"""
================================================================
미국 증시 캘린더 · 스케줄 시각 테스트

실행:  python tests/test_calendar.py
================================================================
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.market_calendar import (
    ET,
    KST,
    dst_range,
    early_close_days,
    is_dst,
    is_early_close,
    is_trading_day,
    market_close_kst,
    market_holidays,
    next_trading_day,
    premarket_open_kst,
    prev_trading_day,
    recent_trading_days,
    upcoming_session,
)


def _kst(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=KST)


# ================================================================
# 서머타임
# ================================================================

def test_dst_range_2026():
    """3월 둘째 일요일 ~ 11월 첫째 일요일"""
    start, end = dst_range(2026)
    assert start == date(2026, 3, 8)
    assert end == date(2026, 11, 1)


def test_is_dst_boundaries():
    assert is_dst(datetime(2026, 3, 7, 12, tzinfo=ET)) is False
    assert is_dst(datetime(2026, 3, 9, 12, tzinfo=ET)) is True
    assert is_dst(datetime(2026, 10, 31, 12, tzinfo=ET)) is True
    assert is_dst(datetime(2026, 11, 2, 12, tzinfo=ET)) is False


def test_is_dst_from_kst_is_timezone_safe():
    """한국시각을 넣어도 ET 로 변환해 판정한다 (서버 타임존과 무관)"""
    assert is_dst(_kst("2026-09-15 07:00")) is True
    assert is_dst(_kst("2026-12-15 07:00")) is False


# ================================================================
# 휴장일
# ================================================================

def test_holidays_2026():
    h = market_holidays(2026)
    assert date(2026, 1, 1) in h          # 신정 (목)
    assert date(2026, 1, 19) in h         # MLK
    assert date(2026, 2, 16) in h         # Presidents'
    assert date(2026, 4, 3) in h          # Good Friday
    assert date(2026, 5, 25) in h         # Memorial
    assert date(2026, 6, 19) in h         # Juneteenth (금)
    assert date(2026, 9, 7) in h          # Labor Day
    assert date(2026, 11, 26) in h        # Thanksgiving
    assert date(2026, 12, 25) in h        # Christmas (금)


def test_weekend_holiday_observed_on_friday():
    """2026년 독립기념일은 토요일 → 7/3 금요일 휴장"""
    assert date(2026, 7, 4).weekday() == 5
    assert date(2026, 7, 3) in market_holidays(2026)
    assert is_trading_day(date(2026, 7, 3)) is False


def test_good_friday_moves_with_easter():
    assert date(2026, 4, 3) in market_holidays(2026)   # 부활절 4/5
    assert date(2027, 3, 26) in market_holidays(2027)  # 부활절 3/28


def test_weekend_is_not_trading_day():
    assert is_trading_day(date(2026, 9, 19)) is False   # 토
    assert is_trading_day(date(2026, 9, 20)) is False   # 일
    assert is_trading_day(date(2026, 9, 21)) is True    # 월


# ================================================================
# 조기폐장
# ================================================================

def test_early_close_days_2026():
    e = early_close_days(2026)
    assert date(2026, 11, 27) in e        # 추수감사절 다음날
    assert date(2026, 12, 24) in e        # 성탄 전야 (목)


def test_early_close_excludes_holidays():
    """휴장일이 조기폐장으로 잡히면 안 된다"""
    assert not (early_close_days(2026) & market_holidays(2026))


def test_early_close_shortens_session():
    """조기폐장일은 마감이 3시간 빠르다"""
    normal = market_close_kst(date(2026, 11, 25))      # 16:00 ET
    early = market_close_kst(date(2026, 11, 27))       # 13:00 ET
    assert normal.hour == 6                             # EST → 06:00 KST
    assert early.hour == 3                              # EST → 03:00 KST
    assert is_early_close(date(2026, 11, 27)) is True


# ================================================================
# 세션 시각 (방법론 6번)
# ================================================================

def test_premarket_is_5pm_in_summer_6pm_in_winter():
    """방법론 6: 지정가매도는 프리장 시작인 저녁 5시(서머타임)
    또는 6시(비서머타임)에 건다"""
    summer = premarket_open_kst(date(2026, 9, 15))
    winter = premarket_open_kst(date(2026, 12, 15))
    assert summer.hour == 17
    assert winter.hour == 18


def test_market_close_kst():
    """마감은 서머타임 05:00, 비서머타임 06:00 KST"""
    assert market_close_kst(date(2026, 9, 15)).hour == 5
    assert market_close_kst(date(2026, 12, 15)).hour == 6


def test_close_is_next_calendar_day_in_kst():
    """미국 9/15 장 마감은 한국 9/16 새벽"""
    close = market_close_kst(date(2026, 9, 15))
    assert close.date() == date(2026, 9, 16)


# ================================================================
# 다음 세션 판정
# ================================================================

def test_upcoming_session_before_open():
    """한국 아침에는 그날 저녁 열리는 세션을 잡는다"""
    assert upcoming_session(_kst("2026-09-15 07:00")) == date(2026, 9, 15)


def test_upcoming_session_during_session():
    """한국 새벽(미국 장중)에는 아직 그 세션이다"""
    assert upcoming_session(_kst("2026-09-16 03:00")) == date(2026, 9, 15)


def test_upcoming_session_after_close():
    """마감 이후에는 다음 거래일로 넘어간다"""
    assert upcoming_session(_kst("2026-09-16 07:00")) == date(2026, 9, 16)


def test_upcoming_session_skips_weekend():
    """토요일 아침 → 월요일 세션"""
    assert upcoming_session(_kst("2026-09-19 10:00")) == date(2026, 9, 21)


def test_upcoming_session_skips_holiday():
    """추수감사절(11/26 목) → 11/27 금 세션"""
    assert upcoming_session(_kst("2026-11-26 09:00")) == date(2026, 11, 27)


def test_upcoming_session_skips_observed_holiday():
    """7/3(금) 대체휴장 → 7/6(월)"""
    assert upcoming_session(_kst("2026-07-03 09:00")) == date(2026, 7, 6)


# ================================================================
# 거래일 이동
# ================================================================

def test_next_prev_trading_day():
    assert next_trading_day(date(2026, 9, 18)) == date(2026, 9, 21)   # 금 → 월
    assert prev_trading_day(date(2026, 9, 21)) == date(2026, 9, 18)
    assert next_trading_day(date(2026, 11, 25)) == date(2026, 11, 27)  # 추수감사절 건너뜀


def test_recent_trading_days_for_reverse_star():
    """리버스 별지점은 직전 5거래일 종가 평균 — 주말·휴일을 건너뛴다"""
    days = recent_trading_days(date(2026, 9, 21), 5)   # 월요일 기준
    assert days == [date(2026, 9, 18), date(2026, 9, 17), date(2026, 9, 16),
                    date(2026, 9, 15), date(2026, 9, 14)]
    assert all(is_trading_day(d) for d in days)


# ================================================================
# 스케줄 시각
# ================================================================

def test_day_schedule_ordering():
    """지정가매도 → LOC 예약 → 검증 → EOD 순서여야 한다"""
    from core.market_calendar import DaySchedule
    s = DaySchedule.build(date(2026, 9, 15))
    assert s.premarket < s.submit_loc < s.verify < s.eod
    assert s.premarket.hour == 17                      # 서머타임
    assert s.eod > market_close_kst(date(2026, 9, 15))


def test_day_schedule_early_close():
    from core.market_calendar import DaySchedule
    s = DaySchedule.build(date(2026, 11, 27))
    assert s.early_close is True
    # 조기폐장이면 EOD 도 그만큼 앞당겨진다
    assert s.eod.hour == 3
    assert s.eod < DaySchedule.build(date(2026, 11, 25)).eod.replace(
        year=2026, month=11, day=28)


def test_day_schedule_verify_after_submit():
    """예약 검증은 접수 후에 돈다 (접수 직후엔 조회에 안 뜰 수 있다)"""
    from core.market_calendar import DaySchedule
    s = DaySchedule.build(date(2026, 9, 15))
    assert (s.verify - s.submit_loc) >= timedelta(minutes=15)


# ================================================================

if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:
                failed += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print("-" * 60)
    print("전부 통과" if failed == 0 else f"{failed}건 실패")
    sys.exit(1 if failed else 0)
