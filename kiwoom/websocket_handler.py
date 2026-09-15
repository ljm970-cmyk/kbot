"""
================================================================
미국주식 실시간 체결 수신 (F5)

    wss://api.kiwoom.com:10000/api/us/websocket      운영
    wss://mockapi.kiwoom.com:10000/api/us/websocket  모의투자

F5 는 종목 등록과 무관하게, 토큰을 발급한 계좌의 모든 매매 이벤트를
보낸다. 값은 필드명이 아니라 **숫자 코드**로 오고, 0 패딩된 문자열이다.

    {"data": [{"type": "F5", "item": "NVDA",
               "values": {"9001": "NVDA", "901": "0000198.4200", ...}}],
     "trnm": "REAL"}

기존 구현은 slby_gubun / fil_q / fil_prc 같은 존재하지 않는 필드명을
읽어서 모든 체결이 0으로 들어왔고, 태그 매칭에 반드시 필요한
**주문가격(901)** 을 아예 받지 않았다.

LOC 는 주문가격과 체결가가 다르다 (76.02 에 걸어도 종가 68.10 에 체결).
주문가격이 없으면 어떤 주문이 체결됐는지 복원할 수 없다.
================================================================
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import websockets

logger = logging.getLogger("kbot.ws")
KST = ZoneInfo("Asia/Seoul")


# ================================================================
# F5 필드 코드
# ================================================================

class F5:
    """미국주식 실시간 체결 필드 코드"""
    COUNTRY = "1091"
    EXCHANGE = "8046"
    TICKER = "9001"
    NAME = "302"
    ORIG_ORD_NO = "904"
    ORD_NO = "9203"
    ORD_KIND = "905"        # 10:원주문 11:정정 12:취소
    SIDE = "907"            # 01:매도 02:매수
    TIME = "908"            # 주문/체결시간 HHMMSS
    STATUS = "913"          # 텍스트: 접수 / 부분체결 / 체결완료 / 무효주문
    ORD_QTY = "900"
    ORD_PRICE = "901"       # 주문가격 — 태그 매칭 키
    REMAIN_QTY = "902"
    FILL_NO = "909"         # 체결번호
    FILL_PRICE = "910"      # 체결가
    FILL_QTY = "911"        # 체결량
    HOLDINGS = "930"        # 보유수량 (증권사 기준)
    AVG_PRICE = "931"       # 매입단가 (증권사 기준)
    PL_AMOUNT = "8018"
    CURRENCY = "8043"
    ACCOUNT = "9201"
    TRADE_KIND = "50073"    # 매매구분명: 지정가 / 시장가 / LOC ...
    STOP_PRICE = "50810"
    RESERVED = "50841"      # 예약구분


#: 매매구분명 → TradeType 코드 (텍스트로만 오므로 역매핑이 필요하다)
TRADE_KIND_TO_CODE = {
    "지정가": "00", "시장가": "03",
    "LOC": "30", "MOC": "33",
    "STOP LIMIT": "34", "STOP": "35",
    "VWAP지정가": "26", "TWAP지정가": "27",
    "VWAP시장가": "36", "TWAP시장가": "37",
}


def _num(v) -> float:
    """0 패딩 문자열 → float. ("0000198.4200" → 198.42)"""
    if v is None:
        return 0.0
    s = str(v).strip().replace(",", "")
    if not s:
        return 0.0
    neg = s.startswith("-")
    s = s.lstrip("+-")
    try:
        f = float(s)
    except ValueError:
        return 0.0
    return -f if neg else f


def _int(v) -> int:
    return int(_num(v))


# ================================================================
# 체결 레코드
# ================================================================

@dataclass
class RealtimeFill:
    """F5 수신 체결 1건"""
    ord_no: str
    fill_no: str
    ticker: str
    side: str               # buy / sell
    fill_qty: int
    fill_price: float
    order_price: float      # 주문가격 — LOC 태그 매칭에 필수
    trade_type: str         # TradeType 코드
    status: str
    fill_time: str          # HHMMSS
    trade_date: str         # YYYYMMDD (KST 기준 미국 거래일)
    kst_timestamp: str
    broker_holdings: int = 0     # 증권사 기준 보유수량 — 대조용
    broker_avg_price: float = 0.0

    def to_row(self) -> tuple:
        return (self.ord_no, self.fill_no, self.ticker, self.side,
                self.fill_qty, self.fill_price, self.order_price,
                self.trade_type, self.status, self.fill_time,
                self.trade_date, self.kst_timestamp,
                self.broker_holdings, self.broker_avg_price, 0)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS realtime_fills (
    ord_no            TEXT NOT NULL,
    fill_no           TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    side              TEXT NOT NULL,
    fill_qty          INTEGER NOT NULL,
    fill_price        REAL NOT NULL,
    order_price       REAL NOT NULL DEFAULT 0,
    trade_type        TEXT DEFAULT '',
    status            TEXT DEFAULT '',
    fill_time         TEXT DEFAULT '',
    trade_date        TEXT NOT NULL,
    kst_timestamp     TEXT NOT NULL,
    broker_holdings   INTEGER DEFAULT 0,
    broker_avg_price  REAL DEFAULT 0,
    processed         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ord_no, fill_no)
);
CREATE INDEX IF NOT EXISTS idx_fills_day ON realtime_fills(trade_date, ticker);
"""


# ================================================================
# 수신기
# ================================================================

class WebSocketFillReceiver:
    """F5 실시간 체결 수신기

    토큰은 콜백으로 받는다. 생성 시점 토큰을 복사해 두면 갱신 후
    옛 토큰으로 재연결을 시도하게 된다 (기존 구현의 문제).
    """

    PING_INTERVAL = 30
    MAX_BACKOFF = 300

    def __init__(
        self,
        access_token: str | Callable[[], str] = "",
        db_path: str | Path = "data/fills/realtime_fills.db",
        ws_url: str = "wss://api.kiwoom.com:10000",
        group_no: str = "1",
    ):
        self._token_source = access_token
        self.db_path = str(db_path)
        self.url = f"{ws_url.rstrip('/')}/api/us/websocket"
        self.group_no = group_no
        self.running = False
        self.connected = False
        self._ws = None
        self._callbacks: list[Callable[[RealtimeFill], None]] = []
        self._last_message_at: Optional[datetime] = None

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as c:
            c.executescript(_SCHEMA)

    @property
    def token(self) -> str:
        src = self._token_source
        return src() if callable(src) else str(src)

    def add_callback(self, cb: Callable[[RealtimeFill], None]) -> None:
        self._callbacks.append(cb)

    # ------------------------------------------------------------
    # 연결
    # ------------------------------------------------------------

    async def connect(self) -> None:
        """끊기면 지수 백오프로 재연결한다"""
        self.running = True
        backoff = 1
        while self.running:
            try:
                async with websockets.connect(self.url, ping_interval=self.PING_INTERVAL) as ws:
                    self._ws = ws
                    await self._login(ws)
                    await self._register(ws)
                    self.connected = True
                    backoff = 1
                    logger.info("F5 실시간 체결 수신 시작")
                    await self._receive_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self.running:
                    break
                logger.warning("WebSocket 끊김 (%s) — %d초 후 재연결", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.MAX_BACKOFF)
            finally:
                self.connected = False
                self._ws = None

    async def _login(self, ws) -> None:
        await ws.send(json.dumps({"trnm": "LOGIN", "token": self.token}))
        resp = json.loads(await ws.recv())
        if str(resp.get("return_code", "0")) not in ("0", "None"):
            raise RuntimeError(f"WebSocket 로그인 실패: {resp.get('return_msg')}")

    async def _register(self, ws) -> None:
        """F5 등록. 종목 등록과 무관하게 계좌 전체 이벤트가 온다."""
        await ws.send(json.dumps({
            "trnm": "REG",
            "grp_no": self.group_no,
            "refresh": "1",
            "data": [{"item": [], "type": ["F5"]}],
        }))

    async def _receive_loop(self, ws) -> None:
        async for raw in ws:
            if not self.running:
                break
            try:
                await self._handle(json.loads(raw))
            except Exception:
                logger.exception("메시지 처리 실패: %s", str(raw)[:300])

    async def _handle(self, msg: dict) -> None:
        self._last_message_at = datetime.now(KST)

        if msg.get("trnm") == "PING":
            if self._ws:
                await self._ws.send(json.dumps(msg))
            return
        if msg.get("trnm") != "REAL":
            return

        for block in msg.get("data", []):
            if block.get("type") != "F5":
                continue
            fill = self._parse(block.get("values", {}))
            if fill is None:
                continue
            self._save(fill)
            logger.info("체결 %s %s %d주 @%.2f (주문가 %.2f, %s)",
                        fill.ticker, fill.side, fill.fill_qty,
                        fill.fill_price, fill.order_price, fill.status)
            for cb in self._callbacks:
                try:
                    cb(fill)
                except Exception:
                    logger.exception("체결 콜백 실패")

    # ------------------------------------------------------------
    # 파싱
    # ------------------------------------------------------------

    def _parse(self, v: dict) -> Optional[RealtimeFill]:
        """F5 values → RealtimeFill.

        F5 는 주문 접수 시점에도 온다 (913="접수", 911="0").
        체결량이 0이면 체결이 아니므로 건너뛴다.
        """
        fill_qty = _int(v.get(F5.FILL_QTY))
        if fill_qty <= 0:
            return None

        now = datetime.now(KST)
        kind = str(v.get(F5.TRADE_KIND, "")).strip()

        return RealtimeFill(
            ord_no=str(v.get(F5.ORD_NO, "")).lstrip("0") or "0",
            fill_no=str(v.get(F5.FILL_NO, "")).lstrip("0") or "0",
            ticker=str(v.get(F5.TICKER, "")).upper(),
            side="sell" if str(v.get(F5.SIDE, "")).zfill(2) == "01" else "buy",
            fill_qty=fill_qty,
            fill_price=_num(v.get(F5.FILL_PRICE)),
            order_price=_num(v.get(F5.ORD_PRICE)),
            trade_type=TRADE_KIND_TO_CODE.get(kind, ""),
            status=str(v.get(F5.STATUS, "")),
            fill_time=str(v.get(F5.TIME, "")),
            trade_date=self._session_date(now),
            kst_timestamp=now.isoformat(timespec="seconds"),
            broker_holdings=_int(v.get(F5.HOLDINGS)),
            broker_avg_price=_num(v.get(F5.AVG_PRICE)),
        )

    @staticmethod
    def _session_date(now: datetime) -> str:
        """체결이 속한 미국 거래일(YYYYMMDD).

        한국 새벽에 들어오는 체결은 전날 저녁에 시작된 미국 세션의 것이다.
        """
        from core.market_calendar import ET
        return now.astimezone(ET).strftime("%Y%m%d")

    # ------------------------------------------------------------
    # 저장 / 조회
    # ------------------------------------------------------------

    def _save(self, fill: RealtimeFill) -> None:
        with sqlite3.connect(self.db_path, timeout=10) as c:
            c.execute(
                "INSERT OR IGNORE INTO realtime_fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                fill.to_row())

    async def fills_for(self, trade_date: str, ticker: str) -> list:
        """EOD 계산기에 넘길 FillEvent 목록.

        SchedulerEngine(fill_source=...) 에 그대로 연결된다.
        """
        from eod.calculator import FillEvent

        with sqlite3.connect(self.db_path, timeout=10) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM realtime_fills WHERE trade_date=? AND ticker=?",
                (trade_date, ticker.upper())).fetchall()

        return [
            FillEvent(
                ticker=r["ticker"], side=r["side"],
                qty=r["fill_qty"], price=r["fill_price"],
                order_price=r["order_price"] or None,
                ord_no=r["ord_no"], fill_no=r["fill_no"],
                trade_type=r["trade_type"],
                time=r["fill_time"], source="websocket",
            )
            for r in rows
        ]

    def broker_position(self, trade_date: str, ticker: str) -> Optional[tuple[int, float]]:
        """증권사가 알려준 마지막 보유수량·매입단가.

        전략 장부와 대조해 어긋남을 조기에 잡는 데 쓴다.
        """
        with sqlite3.connect(self.db_path, timeout=10) as c:
            r = c.execute(
                "SELECT broker_holdings, broker_avg_price FROM realtime_fills "
                "WHERE trade_date=? AND ticker=? ORDER BY kst_timestamp DESC LIMIT 1",
                (trade_date, ticker.upper())).fetchone()
        return (r[0], r[1]) if r else None

    def mark_processed(self, trade_date: str, ticker: str = "") -> int:
        sql = "UPDATE realtime_fills SET processed=1 WHERE trade_date=?"
        args = [trade_date]
        if ticker:
            sql += " AND ticker=?"
            args.append(ticker.upper())
        with sqlite3.connect(self.db_path, timeout=10) as c:
            return c.execute(sql, args).rowcount

    def health(self) -> str:
        if not self.connected:
            return "WebSocket 끊김"
        if self._last_message_at:
            gap = (datetime.now(KST) - self._last_message_at).total_seconds()
            return f"WebSocket 연결됨 (마지막 수신 {gap:.0f}초 전)"
        return "WebSocket 연결됨 (수신 없음)"

    def stop(self) -> None:
        self.running = False
