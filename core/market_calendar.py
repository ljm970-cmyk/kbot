"""
================================================================
미국 증시 캘린더

기존 스케줄러는 휴장일·주말 판정이 전혀 없어서 휴일에도 예약주문을
밀어넣었고, 서머타임 처리도 타임존 없는 datetime.now() 를 써서
서버가 UTC 면 완전히 어긋났다.

여기서는 모든 판정을 명시적인 타임존으로 한다.

  미국 서머타임(DST)  3월 둘째 일요일 ~ 11월 첫째 일요일
  정규장              09:30 ~ 16:00 ET
  조기폐장            13:00 ET (독립기념일 전날, 추수감사절 다음날, 성탄 전야)

  한국시각 기준 미국장 마감
    서머타임    05:00 KST
    비서머타임  06:00 KST
  조기폐장일은 각각 02:00 / 03:00 KST
================================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")


# ================================================================
# 서머타임
# ================================================================

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """그 달의 n번째 요일 (weekday: 월=0 … 일=6)"""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """그 달의 마지막 요일"""
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def dst_range(year: int) -> tuple[date, date]:
    """해당 연도의 미국 서머타임 시작·종료일"""
    return _nth_weekday(year, 3, 6, 2), _nth_weekday(year, 11, 6, 1)


def is_dst(when: Optional[datetime] = None) -> bool:
    """미국 동부 기준 서머타임 여부.

    한국시각을 넣어도 ET 로 변환해 판정하므로 서버 타임존과 무관하다.
    """
    when = when or datetime.now(KST)
    if when.tzinfo is None:
        when = when.replace(tzinfo=KST)
    return when.astimezone(ET).dst() != timedelta(0)


# ================================================================
# 휴장일
# ================================================================

def _easter(year: int) -> date:
    """부활절 (Anonymous Gregorian algorithm). 성금요일 계산용."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date) -> date:
    """토요일이면 전날 금요일, 일요일이면 다음날 월요일로 대체 휴장"""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def market_holidays(year: int) -> set[date]:
    """NYSE / NASDAQ 정규 휴장일"""
    return {
        _observed(date(year, 1, 1)),                  # 신정
        _nth_weekday(year, 1, 0, 3),                  # MLK Day
        _nth_weekday(year, 2, 0, 3),                  # Presidents' Day
        _easter(year) - timedelta(days=2),            # Good Friday
        _last_weekday(year, 5, 0),                    # Memorial Day
        _observed(date(year, 6, 19)),                 # Juneteenth
        _observed(date(year, 7, 4)),                  # Independence Day
        _nth_weekday(year, 9, 0, 1),                  # Labor Day
        _nth_weekday(year, 11, 3, 4),                 # Thanksgiving
        _observed(date(year, 12, 25)),                # Christmas
    }


def early_close_days(year: int) -> set[date]:
    """13:00 ET 조기폐장일.

    조기폐장일은 LOC/MOC 접수 마감도 3시간 앞당겨지므로
    주문 시각을 반드시 조정해야 한다.
    """
    days = set()

    # 독립기념일 전날 (7/4 가 평일일 때)
    july4 = date(year, 7, 4)
    if july4.weekday() < 5:
        prev = july4 - timedelta(days=1)
        if prev.weekday() < 5:
            days.add(prev)

    # 추수감사절 다음날 (금요일)
    days.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))

    # 성탄 전야 (평일일 때)
    xmas_eve = date(year, 12, 24)
    if xmas_eve.weekday() < 5:
        days.add(xmas_eve)

    return days - market_holidays(year)


# ================================================================
# 거래일 판정
# ================================================================

def is_trading_day(d: date) -> bool:
    """주말·휴장일이 아닌 정규 거래일인지"""
    if d.weekday() >= 5:
        return False
    return d not in market_holidays(d.year)


def is_early_close(d: date) -> bool:
    return d in early_close_days(d.year)


def next_trading_day(d: date) -> date:
    nxt = d + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def prev_trading_day(d: date) -> date:
    prv = d - timedelta(days=1)
    while not is_trading_day(prv):
        prv -= timedelta(days=1)
    return prv


def recent_trading_days(d: date, n: int) -> list[date]:
    """d 이전 n거래일 (최신순). 리버스 별지점 검증용."""
    out, cur = [], d
    for _ in range(n):
        cur = prev_trading_day(cur)
        out.append(cur)
    return out


# ================================================================
# 한국시각 기준 세션
# ================================================================

def session_date_for(now: Optional[datetime] = None) -> date:
    """지금 접수하면 어느 거래일 주문이 되는지.

    한국 저녁에 접수하는 예약주문은 미국 기준 '같은 날' 정규장에 들어간다.
    한국 새벽(자정 이후)이면 이미 그 미국 거래일 장중이다.

    예: KST 9/15(화) 18:30 에 접수 → 미국 9/15(화) 정규장
        KST 9/16(수) 02:00 은 미국 9/15(화) 장중
    """
    now = now or datetime.now(KST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=KST)
    et = now.astimezone(ET)
    d = et.date()
    return d if is_trading_day(d) else next_trading_day(d)


def upcoming_session(now: Optional[datetime] = None) -> date:
    """지금 이후로 주문을 낼 수 있는 다음 미국 거래일.

    session_date_for() 는 '현재 미국 날짜' 기준이라 이미 마감된 세션을
    돌려줄 수 있다. 스케줄러가 다음 장을 준비할 때는 이 함수를 쓴다.

    예: KST 9/15(화) 07:00 은 ET 9/14(월) 18:00 — 9/14 장은 이미 마감.
        따라서 다음 세션인 9/15(화)를 돌려준다.
    """
    now = now or datetime.now(KST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=KST)
    d = now.astimezone(ET).date()
    for _ in range(15):
        if is_trading_day(d) and market_close_kst(d) > now:
            return d
        d += timedelta(days=1)
    raise RuntimeError("15일 내에 거래일을 찾지 못했습니다")


def market_close_kst(d: date) -> datetime:
    """해당 미국 거래일의 장 마감 시각을 한국시각으로"""
    hour = 13 if is_early_close(d) else 16
    close_et = datetime(d.year, d.month, d.day, hour, 0, tzinfo=ET)
    return close_et.astimezone(KST)


def premarket_open_kst(d: date) -> datetime:
    """프리장 시작(04:00 ET)을 한국시각으로.

    방법론 6번: 지정가매도를 최대한 길게 쓰려면 프리장 시작에 건다.
    서머타임이면 17:00 KST, 아니면 18:00 KST.
    """
    open_et = datetime(d.year, d.month, d.day, 4, 0, tzinfo=ET)
    return open_et.astimezone(KST)


def regular_open_kst(d: date) -> datetime:
    """정규장 개장(09:30 ET)을 한국시각으로. 서머타임 22:30, 아니면 23:30."""
    open_et = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
    return open_et.astimezone(KST)


# ================================================================
# 하루 작업 시각
# ================================================================

@dataclass(frozen=True)
class DaySchedule:
    """한 거래일의 작업 시각 (모두 KST).

    고정 cron 대신 매일 이 값을 계산해 일회성 작업으로 등록한다.
    서머타임과 조기폐장이 자동으로 반영되고, 시각 계산이 한 곳에 모인다.
    APScheduler 에 의존하지 않으므로 단독으로 검증할 수 있다.
    """
    session: date
    premarket: datetime      # 지정가매도 접수 (방법론 6) — 실시간 주문
    submit_loc: datetime     # LOC 실시간 접수 (MOC 는 예약)
    verify: datetime         # 예약주문 검증 (ust21205) — 접수 확인
    verify_open: datetime    # 개장 후 재검증 — 실주문 전환 시 거부 확인
    eod: datetime            # 정산
    early_close: bool

    #: 프리장 시작 후 지정가매도 접수까지 대기.
    #: 개장 정각에 넣으면 세션 전환 순간과 겹쳐 거부될 수 있다.
    TARGET_DELAY = timedelta(minutes=1)
    #: 프리장 시작 후 LOC 접수까지 대기. 지정가매도 바로 다음에 낸다.
    #: (둘 다 실시간 주문이라 프리장에 함께 걸린다)
    LOC_DELAY = timedelta(minutes=2)
    #: 정규장 개장 후 재검증까지 대기.
    #: 예약주문은 정규장 개장 때 실주문으로 넘어가고, 증거금·가격제한폭
    #: 거부도 그때 결정된다. 프리장 검증만으로는 거부를 잡지 못한다.
    OPEN_VERIFY_DELAY = timedelta(minutes=10)
    #: 접수 후 검증까지 대기 (접수 직후엔 조회에 안 뜰 수 있다)
    VERIFY_DELAY = timedelta(minutes=35)
    #: 마감 후 정산까지 대기 (체결 데이터 반영 시간)
    EOD_DELAY = timedelta(minutes=30)

    @classmethod
    def build(cls, session: date) -> "DaySchedule":
        pre = premarket_open_kst(session)
        return cls(
            session=session,
            premarket=pre + cls.TARGET_DELAY,
            submit_loc=pre + cls.LOC_DELAY,
            verify=pre + cls.VERIFY_DELAY,
            verify_open=regular_open_kst(session) + cls.OPEN_VERIFY_DELAY,
            eod=market_close_kst(session) + cls.EOD_DELAY,
            early_close=is_early_close(session),
        )

    def describe(self) -> str:
        tag = " · 조기폐장" if self.early_close else ""
        return (f"{self.session} 일정{tag}\n"
                f"  🔵 지정가매도 {self.premarket:%m/%d %H:%M}\n"
                f"  🔴 LOC 접수   {self.submit_loc:%m/%d %H:%M}\n"
                f"  🔎 주문 검증  {self.verify:%m/%d %H:%M}\n"
                f"  🔎 개장 검증  {self.verify_open:%m/%d %H:%M}\n"
                f"  🧾 EOD 정산   {self.eod:%m/%d %H:%M}")


def describe(d: Optional[date] = None) -> str:
    """사람이 읽을 요약 (텔레그램 /status 용)"""
    d = d or session_date_for().date() if isinstance(d, datetime) else (d or session_date_for())
    if not is_trading_day(d):
        return f"{d} 휴장"
    tag = " · 조기폐장(13:00 ET)" if is_early_close(d) else ""
    dst = "서머타임" if is_dst(datetime.combine(d, datetime.min.time(), tzinfo=ET)) else "비서머타임"
    return (f"{d} 거래일{tag} · {dst}\n"
            f"  프리장 시작 {premarket_open_kst(d):%m/%d %H:%M} KST\n"
            f"  마감       {market_close_kst(d):%m/%d %H:%M} KST")
