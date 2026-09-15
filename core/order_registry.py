"""
================================================================
주문 원장 (Order Registry)

키움 API 에는 사용자 주문 태그 필드가 없다. 주문 접수 시 받는 것은
ord_no(실시간) 또는 rsrv_ord_no(예약)뿐이고, 체결 통보(F5 / ust21510)는
ord_no 만 돌려준다. 그래서 "이 체결이 별지점 매수였나 폭락대비였나"를
알 방법이 API 쪽에 없다.

이 모듈이 그 간극을 메운다. 주문을 낼 때 FillKind 태그와 주문 속성을
로컬 SQLite 에 적어두고, 다음날 체결이 들어오면 역조회해서 태그를 복원한다.
T값 계산 전체가 이 태그에 의존한다.

--- 예약주문의 함정 ------------------------------------------
ust21200/ust21201 은 rsrv_ord_no 만 주고, 그 예약이 실제 주문으로
전환될 때 새로 부여되는 ord_no 를 알려주지 않는다. ust21205
(예약주문 내역조회) 응답에도 ord_no 필드가 없다.

따라서 예약주문은 ord_no 로 직접 이을 수 없고, 주문 속성
(종목 · 매매구분 · 매매유형 · 주문단가 · 수량) 으로 매칭해야 한다.
무한매수법은 하루에 내는 주문들의 가격이 모두 다르므로
(별지점 / 평단 / 폭락대비 5단계) 단가만으로도 사실상 유일하게 갈린다.
================================================================
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from core.t_calculator import DayFills, FillKind

logger = logging.getLogger("kbot.registry")

#: 주문단가 매칭 허용 오차 (센트 단위 반올림 차이 흡수)
PRICE_TOLERANCE = 0.005


class OrderStatus:
    SUBMITTED = "submitted"     # 접수 완료
    REJECTED = "rejected"       # 예약 검증에서 거부됨
    CANCELLED = "cancelled"
    FILLED = "filled"
    PARTIAL = "partial"
    EXPIRED = "expired"         # 당일 미체결 종료


TERMINAL_STATUSES = frozenset({
    OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.EXPIRED,
})


@dataclass
class OrderRecord:
    """원장 1행"""
    id: int
    trade_date: str          # YYYYMMDD — 체결이 일어날 거래일
    ticker: str
    tag: str                 # FillKind
    side: str                # buy / sell
    trade_type: str          # TradeType
    qty: int
    price: Optional[float]
    ord_no: str = ""
    rsrv_ord_no: str = ""
    status: str = OrderStatus.SUBMITTED
    filled_qty: int = 0
    filled_price: float = 0.0
    note: str = ""
    applied_qty: int = 0        # record_fill 이 이번에 반영한 수량

    @property
    def is_open(self) -> bool:
        return self.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIAL)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date   TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    tag          TEXT NOT NULL,
    side         TEXT NOT NULL,
    trade_type   TEXT NOT NULL,
    qty          INTEGER NOT NULL,
    price        REAL,
    ord_no       TEXT DEFAULT '',
    rsrv_ord_no  TEXT DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'submitted',
    filled_qty   INTEGER NOT NULL DEFAULT 0,
    filled_price REAL NOT NULL DEFAULT 0,
    note         TEXT DEFAULT '',
    submitted_at TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_day    ON orders(trade_date, ticker);
CREATE INDEX IF NOT EXISTS idx_orders_ordno  ON orders(ord_no);
CREATE INDEX IF NOT EXISTS idx_orders_rsrv   ON orders(rsrv_ord_no);
CREATE INDEX IF NOT EXISTS idx_orders_match  ON orders(trade_date, ticker, side, price);
"""

_COLUMNS = (
    "id, trade_date, ticker, tag, side, trade_type, qty, price, "
    "ord_no, rsrv_ord_no, status, filled_qty, filled_price, note"
)


class OrderRegistry:
    """주문 태그 ↔ 주문번호 매핑 저장소"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _row(r: sqlite3.Row) -> OrderRecord:
        return OrderRecord(
            id=r["id"], trade_date=r["trade_date"], ticker=r["ticker"],
            tag=r["tag"], side=r["side"], trade_type=r["trade_type"],
            qty=r["qty"], price=r["price"], ord_no=r["ord_no"],
            rsrv_ord_no=r["rsrv_ord_no"], status=r["status"],
            filled_qty=r["filled_qty"], filled_price=r["filled_price"],
            note=r["note"],
        )

    # ============================================================
    # 기록
    # ============================================================

    def record_submission(
        self,
        trade_date: str,
        ticker: str,
        planned_order,
        ord_no: str = "",
        rsrv_ord_no: str = "",
    ) -> int:
        """주문 접수 직후 호출. PlannedOrder 의 태그를 원장에 남긴다.

        Args:
            trade_date: 체결이 일어날 거래일(YYYYMMDD). 예약주문을 전날 저녁에
                접수하더라도 실제 정규장 날짜를 넣어야 체결과 이어진다.
        Returns:
            원장 행 id
        """
        now = self._now()
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO orders (trade_date, ticker, tag, side, trade_type, qty, "
                "price, ord_no, rsrv_ord_no, status, note, submitted_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (trade_date, ticker.upper(), planned_order.tag, planned_order.side,
                 planned_order.trade_type, int(planned_order.qty), planned_order.price,
                 str(ord_no), str(rsrv_ord_no), OrderStatus.SUBMITTED,
                 getattr(planned_order, "note", ""), now, now),
            )
            return cur.lastrowid

    def record_plan(self, trade_date: str, ticker: str, planned_orders: Iterable) -> list[int]:
        """여러 건을 한 번에 (접수 번호는 나중에 attach)"""
        return [self.record_submission(trade_date, ticker, o) for o in planned_orders]

    def attach_ord_no(self, record_id: int, ord_no: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE orders SET ord_no=?, updated_at=? WHERE id=?",
                      (str(ord_no), self._now(), record_id))

    def attach_rsrv_ord_no(self, record_id: int, rsrv_ord_no: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE orders SET rsrv_ord_no=?, updated_at=? WHERE id=?",
                      (str(rsrv_ord_no), self._now(), record_id))

    def set_status(self, record_id: int, status: str, note: str = "") -> None:
        with self._conn() as c:
            if note:
                c.execute("UPDATE orders SET status=?, note=?, updated_at=? WHERE id=?",
                          (status, note, self._now(), record_id))
            else:
                c.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?",
                          (status, self._now(), record_id))

    def mark_rejected_by_rsrv_no(self, rsrv_ord_no: str, reason: str) -> int:
        """ust21205 검증에서 거부로 확인된 예약주문을 표시.

        거부된 주문은 체결될 수 없으므로 매칭 후보에서 빠져야 한다.
        """
        with self._conn() as c:
            cur = c.execute(
                "UPDATE orders SET status=?, note=?, updated_at=? WHERE rsrv_ord_no=?",
                (OrderStatus.REJECTED, reason, self._now(), str(rsrv_ord_no)),
            )
            return cur.rowcount

    def expire_open_orders(self, trade_date: str, ticker: str = "") -> int:
        """거래일 종료 후 미체결 주문을 만료 처리.

        LOC/MOC 는 당일 종가로만 체결되므로 다음날로 넘어가지 않는다.

        목표 지정가매도도 함께 만료시킨다. 일반예약(당일 1회)으로 매일
        새로 접수하기 때문이다 — 평단이 바뀌면 목표가도 바뀌므로 매일
        재계산해서 거는 것이 맞다.

        이걸 빼먹으면 원장에 미체결 매도 주문이 날마다 쌓여서,
        보유수량보다 많은 매도 주문이 열려 있는 것처럼 보이고
        체결 태그 매칭도 모호해진다.
        """
        with self._conn() as c:
            sql = ("UPDATE orders SET status=?, updated_at=? "
                   "WHERE trade_date=? AND status IN (?,?)")
            args = [OrderStatus.EXPIRED, self._now(), trade_date,
                    OrderStatus.SUBMITTED, OrderStatus.PARTIAL]
            if ticker:
                sql += " AND ticker=?"
                args.append(ticker.upper())
            return c.execute(sql, args).rowcount

    # ============================================================
    # 조회
    # ============================================================

    def get(self, record_id: int) -> Optional[OrderRecord]:
        with self._conn() as c:
            r = c.execute(f"SELECT {_COLUMNS} FROM orders WHERE id=?", (record_id,)).fetchone()
            return self._row(r) if r else None

    def by_ord_no(self, ord_no: str) -> Optional[OrderRecord]:
        with self._conn() as c:
            r = c.execute(f"SELECT {_COLUMNS} FROM orders WHERE ord_no=? AND ord_no!=''",
                          (str(ord_no),)).fetchone()
            return self._row(r) if r else None

    def open_orders(self, trade_date: str, ticker: str = "") -> list[OrderRecord]:
        with self._conn() as c:
            sql = (f"SELECT {_COLUMNS} FROM orders WHERE trade_date=? "
                   f"AND status IN (?,?)")
            args = [trade_date, OrderStatus.SUBMITTED, OrderStatus.PARTIAL]
            if ticker:
                sql += " AND ticker=?"
                args.append(ticker.upper())
            sql += " ORDER BY side, price DESC"
            return [self._row(r) for r in c.execute(sql, args)]

    def day_orders(self, trade_date: str, ticker: str = "") -> list[OrderRecord]:
        with self._conn() as c:
            sql = f"SELECT {_COLUMNS} FROM orders WHERE trade_date=?"
            args = [trade_date]
            if ticker:
                sql += " AND ticker=?"
                args.append(ticker.upper())
            sql += " ORDER BY id"
            return [self._row(r) for r in c.execute(sql, args)]

    def already_submitted(self, trade_date: str, ticker: str,
                          tags: Iterable[str]) -> list[str]:
        """해당 거래일에 이미 접수한 태그를 돌려준다.

        같은 날 같은 주문을 두 번 내면 포지션이 두 배가 된다.
        스케줄러 재등록이나 /run submit 수동 실행으로 실제 발생할 수 있다.
        거부·취소된 건은 다시 내야 하므로 후보에서 뺀다.
        """
        want = set(tags)
        if not want:
            return []
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT tag FROM orders WHERE trade_date=? AND ticker=? "
                "AND status NOT IN (?,?)",
                (trade_date, ticker.upper(), OrderStatus.REJECTED, OrderStatus.CANCELLED),
            ).fetchall()
        return sorted({r["tag"] for r in rows} & want)

    # ============================================================
    # 체결 매칭
    # ============================================================

    def resolve(
        self,
        trade_date: str,
        ticker: str,
        side: str,
        price: Optional[float],
        qty: int = 0,
        ord_no: str = "",
        trade_type: str = "",
    ) -> Optional[OrderRecord]:
        """체결 1건을 원장의 주문과 잇는다.

        1. ord_no 가 원장에 있으면 그것으로 확정
        2. 없으면 (거래일·종목·매매구분·단가) 로 매칭 — 예약주문 경로
        3. 그래도 없으면 단가 무시하고 미체결 후보 중 수량이 맞는 것

        무한매수법은 하루 주문들의 단가가 모두 다르므로 2단계에서
        거의 항상 유일하게 갈린다.
        """
        if ord_no:
            rec = self.by_ord_no(ord_no)
            if rec:
                return rec

        candidates = [
            r for r in self.open_orders(trade_date, ticker)
            if r.side == side and (not trade_type or r.trade_type == trade_type)
        ]
        if not candidates:
            return None

        # 단가 매칭
        if price is not None:
            exact = [r for r in candidates
                     if r.price is not None and abs(r.price - price) <= PRICE_TOLERANCE]
            if len(exact) == 1:
                return exact[0]
            if len(exact) > 1:
                # 같은 단가가 여럿이면 매매유형 → 수량 순으로 가른다.
                #
                # 실제로 겹치는 경우가 있다: T=0 에서 TQQQ 별%가 정확히 15%,
                # SOXL 이 20% 가 되어 쿼터매도(별지점 LOC)와
                # 목표매도(평단+15%/20% 지정가)의 단가가 같아진다.
                # 매매유형(LOC=30 / 지정가=00)이 다르므로 그것으로 갈린다.
                if trade_type:
                    by_type = [r for r in exact if r.trade_type == trade_type]
                    if len(by_type) == 1:
                        return by_type[0]
                    if by_type:
                        exact = by_type
                by_qty = [r for r in exact if r.qty == qty]
                if len(by_qty) == 1:
                    return by_qty[0]
                logger.warning(
                    "단가 %.2f 에 후보가 %d건 — 태그 매칭이 모호합니다 (%s %s). "
                    "체결 데이터에 매매구분(frgn_trde_tp)을 함께 넘기세요.",
                    price, len(exact), ticker, trade_date)
                return exact[0]

        # MOC/시장가처럼 단가가 없는 주문
        no_price = [r for r in candidates if r.price is None]
        if len(no_price) == 1:
            return no_price[0]

        by_qty = [r for r in candidates if r.qty == qty]
        if len(by_qty) == 1:
            return by_qty[0]

        return None

    def record_fill(
        self,
        trade_date: str,
        ticker: str,
        side: str,
        fill_qty: int,
        fill_price: float,
        ord_no: str = "",
        order_price: Optional[float] = None,
        trade_type: str = "",
    ) -> Optional[OrderRecord]:
        """체결을 원장에 반영하고 매칭된 주문을 돌려준다.

        Args:
            fill_price: 실제 체결단가 (LOC 는 종가)
            order_price: 주문단가. 매칭 키로 쓰인다. 생략하면 fill_price 로 시도하지만,
                LOC 는 주문단가와 체결단가가 다르므로 가급적 넘겨야 한다.
        """
        rec = self.resolve(trade_date, ticker, side,
                           order_price if order_price is not None else fill_price,
                           fill_qty, ord_no, trade_type)
        if rec is None:
            logger.warning("체결을 주문과 잇지 못했습니다: %s %s %s %d주 @%.2f",
                           trade_date, ticker, side, fill_qty, fill_price)
            return None

        # 멱등성 — 이미 주문수량만큼 체결 처리된 건은 다시 더하지 않는다.
        #
        # EOD 를 같은 날 두 번 돌리면(스케줄 + /run eod) 같은 체결이 다시
        # 들어온다. resolve() 는 ord_no 로 찾을 때 상태를 보지 않으므로
        # 이미 체결된 주문을 다시 찾아내고, 그대로 두면 수량이 두 배가 된다.
        if rec.filled_qty >= rec.qty:
            logger.info("이미 체결 처리된 주문입니다 (ord_no=%s, %d/%d주) — 건너뜁니다",
                        rec.ord_no or ord_no, rec.filled_qty, rec.qty)
            return None

        # 주문수량을 넘는 체결은 있을 수 없다
        add_qty = min(int(fill_qty), rec.qty - rec.filled_qty)
        filled = rec.filled_qty + add_qty
        status = OrderStatus.FILLED if filled >= rec.qty else OrderStatus.PARTIAL

        # 부분체결이 서로 다른 가격에 나면 체결단가는 가중평균이어야 한다.
        # 마지막 값으로 덮어쓰면 이력의 평균단가와 금액이 어긋난다.
        if filled > 0:
            avg_fill = (rec.filled_price * rec.filled_qty
                        + float(fill_price) * add_qty) / filled
        else:
            avg_fill = float(fill_price)

        with self._conn() as c:
            c.execute(
                "UPDATE orders SET filled_qty=?, filled_price=?, status=?, "
                "ord_no=CASE WHEN ?!='' THEN ? ELSE ord_no END, updated_at=? WHERE id=?",
                (filled, avg_fill, status, str(ord_no), str(ord_no),
                 self._now(), rec.id),
            )

        rec.filled_qty = filled
        rec.filled_price = avg_fill
        rec.status = status
        # 이번 호출에서 실제로 반영된 수량. 호출부(EOD)는 체결 이벤트의
        # 수량이 아니라 이 값을 써야 원장과 장부가 어긋나지 않는다.
        rec.applied_qty = add_qty
        return rec

    # ============================================================
    # DayFills 생성
    # ============================================================

    def build_day_fills(self, trade_date: str, ticker: str, holdings_after: int) -> DayFills:
        """그 날 체결된 태그 집합을 모아 T 계산기에 넘길 형태로 만든다."""
        kinds = {
            r.tag for r in self.day_orders(trade_date, ticker)
            if r.status in (OrderStatus.FILLED, OrderStatus.PARTIAL) and r.filled_qty > 0
        }
        return DayFills(kinds=kinds, holdings_after=holdings_after)

    def day_summary(self, trade_date: str, ticker: str) -> str:
        """텔레그램 보고용 한 눈 요약"""
        rows = self.day_orders(trade_date, ticker)
        if not rows:
            return f"[{ticker}] {trade_date} 주문 없음"
        lines = [f"[{ticker}] {trade_date}"]
        for r in rows:
            p = f"@{r.price:.2f}" if r.price is not None else "@MKT"
            mark = {
                OrderStatus.FILLED: "체결",
                OrderStatus.PARTIAL: f"부분({r.filled_qty}/{r.qty})",
                OrderStatus.REJECTED: "거부",
                OrderStatus.CANCELLED: "취소",
                OrderStatus.EXPIRED: "미체결",
            }.get(r.status, r.status)
            lines.append(f"  {r.side:4} {r.qty:>4}주 {p:>10}  [{r.tag}] {mark}")
        return "\n".join(lines)

    # ============================================================
    # 거래 이력
    # ============================================================

    def trade_history(self, ticker: str, days: int = 60) -> list[dict]:
        """일자별 매매 이력. 같은 날 같은 방향은 가중평균으로 묶는다.

        폭락대비까지 포함하면 하루에 매수 주문이 6건까지 나오므로,
        사람이 읽을 때는 하루 한 줄로 합치는 편이 낫다.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT trade_date, side, filled_qty, filled_price FROM orders "
                "WHERE ticker=? AND status IN (?,?) AND filled_qty > 0 "
                "ORDER BY trade_date",
                (ticker.upper(), OrderStatus.FILLED, OrderStatus.PARTIAL),
            ).fetchall()

        merged: dict[tuple, list] = {}
        for r in rows:
            key = (r["trade_date"], r["side"])
            amt, qty = merged.get(key, [0.0, 0])
            merged[key] = [amt + r["filled_price"] * r["filled_qty"],
                           qty + r["filled_qty"]]

        out = []
        for (date, side), (amt, qty) in sorted(merged.items()):
            if qty <= 0:
                continue
            out.append({"date": date, "side": side, "qty": qty,
                        "avg_price": amt / qty, "amount": amt})
        return out[-days:]

    def totals(self, ticker: str) -> dict:
        """누적 매수액·매도액·수량"""
        buy_amt = buy_qty = sell_amt = sell_qty = 0.0
        for t in self.trade_history(ticker, days=10_000):
            if t["side"] == "buy":
                buy_amt += t["amount"]; buy_qty += t["qty"]
            else:
                sell_amt += t["amount"]; sell_qty += t["qty"]
        return {"buy_amount": buy_amt, "buy_qty": int(buy_qty),
                "sell_amount": sell_amt, "sell_qty": int(sell_qty),
                "net_qty": int(buy_qty - sell_qty)}

    # ============================================================
    # 정리
    # ============================================================

    def purge_before(self, trade_date: str) -> int:
        """오래된 원장 삭제 (기본 보존은 호출자가 결정)"""
        with self._conn() as c:
            return c.execute("DELETE FROM orders WHERE trade_date < ?", (trade_date,)).rowcount
