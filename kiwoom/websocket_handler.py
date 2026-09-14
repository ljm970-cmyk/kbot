"""
================================================================
WebSocket 실시간 체결 수신 (F5)

[1] 키움 REST API 문서:
    - F5: 실시간 체결
    - FE: 실시간 체결가
    "ACCESS TOKEN 발급 계좌의 주문 접수/체결/정정/취소 실시간 수신"
    "종목코드(item) 등록과 무관"

모의투자 관련 URL 완전 제거
================================================================
"""

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

import pytz
import websockets

logger = logging.getLogger("kbot.websocket")


@dataclass
class RealtimeFill:
    """실시간 체결 데이터 구조"""
    order_no: str
    fill_no: str
    stock: str
    fill_type: str  # 'buy' or 'sell'
    fill_qty: int
    fill_price: float
    fill_time: str  # HHMMSS
    kst_timestamp: str
    is_reverse_mode: bool = False
    
    def to_db_tuple(self):
        return (
            self.order_no, self.fill_no, self.stock,
            self.fill_type, self.fill_qty, self.fill_price,
            self.fill_time, self.kst_timestamp,
            1 if self.is_reverse_mode else 0
        )


class WebSocketFillReceiver:
    """
    키움 WebSocket 실시간 체결 수신
    
    [1] 운영만 사용:
        wss://api.kiwoom.com:10000
    """
    
    # 운영만 사용 (모의투자 제거)
    WS_URL = "wss://api.kiwoom.com:10000/api/us/websocket"
    
    def __init__(self, access_token: str, db_path: str = "data/fills/realtime_fills.db"):
        self.access_token = access_token
        self.db_path = db_path
        self.running = False
        self.ws = None
        self._callbacks: List[Callable[[RealtimeFill], None]] = []
        
        # DB 초기화
        self._init_db()
    
    def _init_db(self):
        """SQLite: 미처리 체결 임시 저장"""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS realtime_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_no TEXT,
                    fill_no TEXT UNIQUE,
                    stock TEXT,
                    fill_type TEXT,
                    fill_qty INTEGER,
                    fill_price REAL,
                    fill_time TEXT,
                    kst_timestamp TEXT,
                    is_reverse_mode INTEGER DEFAULT 0,
                    is_processed INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_unprocessed 
                ON realtime_fills(stock, is_processed)
            """)
            conn.commit()
        finally:
            conn.close()
    
    def add_callback(self, callback: Callable[[RealtimeFill], None]):
        """체결 수신 시 호출할 콜백 등록"""
        self._callbacks.append(callback)
    
    # ============================================================
    # 연결 관리
    # ============================================================
    
    async def connect(self):
        """WebSocket 연결 및 F5 등록"""
        self.running = True
        
        headers = {
            "Authorization": f"Bearer {self.access_token}"
        }
        
        try:
            async with websockets.connect(
                self.WS_URL,
                extra_headers=headers,
                ping_interval=30,
                ping_timeout=10
            ) as ws:
                self.ws = ws
                logger.info(f"WebSocket 연결: {self.WS_URL}")

                # LOGIN 인증 (REG보다 먼저 필요)
                login_msg = {
                    "trnm": "LOGIN",
                    "token": self.access_token
                }
                await ws.send(json.dumps(login_msg))
                login_resp = json.loads(await ws.recv())
                logger.info(f"LOGIN 응답: {login_resp}")
                
                if login_resp.get("return_code") != 0:
                    logger.error(f"LOGIN 실패: {login_resp.get('return_msg')}")
                    return
                
                # F5 실시간 체결 등록 [1]
                reg_msg = {
                    "trnm": "REG",
                    "grp_no": "0001",
                    "refresh": "0",
                    "data": [
                        {
                            "item": [{"jmcode": "", "stex_tp": "ND"}],  # 전체 종목
                            "type": ["F5"]  # 실시간 체결
                        }
                    ]
                }
                await ws.send(json.dumps(reg_msg))
                logger.info("F5 실시간 체결 등록 완료")
                
                # 수신 루프
                await self._receive_loop(ws)
                
        except Exception as e:
            logger.error(f"WebSocket 연결 오류: {type(e).__name__}: {e!r}", exc_info=True)
            # 재연결 지연
            await asyncio.sleep(10)
            if self.running:
                asyncio.create_task(self.connect())
    
    async def _receive_loop(self, ws):
        """메시지 수신 루프"""
        while self.running:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=60.0)
                await self._handle_message(json.loads(msg))
            except asyncio.TimeoutError:
                # ping 유지
                try:
                    await ws.send(json.dumps({"trnm": "PING"}))
                except:
                    break
            except websockets.exceptions.ConnectionClosed:
                logger.warning(f"WebSocket 연결 종료: code={ws.close_code}, reason={ws.close_reason}")
                break
            except Exception as e:
                logger.error(f"수신 오류: {e}")
    
    async def _handle_message(self, msg: dict):
        """수신 메시지 처리"""
        trnm = msg.get("trnm", "")
        
        if trnm == "F5":  # 실시간 체결 데이터
            fill = self._parse_fill(msg)
            self._save_fill(fill)
            
            # 콜백 실행 (로깅만, T값 계산은 장마감 후)
            for cb in self._callbacks:
                try:
                    if asyncio.iscoroutinefunction(cb):
                        await cb(fill)
                    else:
                        cb(fill)
                except Exception as e:
                    logger.error(f"콜백 오류: {e}")
            
            logger.info(
                f"[실시간체결] {fill.stock} {fill.fill_type} "
                f"{fill.fill_qty}주 @{fill.fill_price} "
                f"(보류→장마감후계산)"
            )
        
        elif trnm == "PONG":
            pass  # 핑퐁 응답
        
        elif trnm == "REG":
            logger.info(f"REG 응답: {msg}")

        elif trnm.startswith("CLOSE"):
            logger.warning(f"서버 종료 신호: {msg}")
    
    def _parse_fill(self, raw: dict) -> RealtimeFill:
        """원시 메시지 → RealtimeFill 파싱"""
        now = datetime.now(pytz.timezone('Asia/Seoul'))
        
        # [1] 키움 API 응답 필드 (실제 응답 확인 필요)
        return RealtimeFill(
            order_no=raw.get("ord_no", ""),
            fill_no=raw.get("fil_no", "") or raw.get("fill_no", ""),
            stock=raw.get("stk_cd", ""),
            fill_type="buy" if raw.get("slby_gubun") == "2" else "sell",
            fill_qty=int(raw.get("fil_q", 0) or raw.get("fill_qty", 0)),
            fill_price=float(raw.get("fil_prc", 0) or raw.get("fill_price", 0)),
            fill_time=raw.get("fil_tm", ""),
            kst_timestamp=now.isoformat(),
            is_reverse_mode=False  # state_manager에서 조회 필요
        )
    
    def _save_fill(self, fill: RealtimeFill):
        """DB에 미처리 상태로 저장"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                INSERT OR IGNORE INTO realtime_fills 
                (order_no, fill_no, stock, fill_type, fill_qty, 
                 fill_price, fill_time, kst_timestamp, is_reverse_mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, fill.to_db_tuple())
            conn.commit()
        finally:
            conn.close()
    
    # ============================================================
    # 조회/처리 (EOD 계산기에서 호출)
    # ============================================================
    
    def get_unprocessed_fills(self, stock: str = None) -> List[RealtimeFill]:
        """장마감 후 처리되지 않은 체결 내역 조회"""
        conn = sqlite3.connect(self.db_path)
        try:
            if stock:
                rows = conn.execute("""
                    SELECT * FROM realtime_fills 
                    WHERE stock = ? AND is_processed = 0
                    ORDER BY kst_timestamp
                """, (stock,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM realtime_fills 
                    WHERE is_processed = 0
                    ORDER BY stock, kst_timestamp
                """,).fetchall()
            
            return [RealtimeFill(
                order_no=r[1], fill_no=r[2], stock=r[3],
                fill_type=r[4], fill_qty=r[5], fill_price=r[6],
                fill_time=r[7], kst_timestamp=r[8], is_reverse_mode=bool(r[9])
            ) for r in rows]
        finally:
            conn.close()
    
    def mark_processed(self, fill_no: str):
        """처리 완료 표시"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                UPDATE realtime_fills SET is_processed = 1 
                WHERE fill_no = ?
            """, (fill_no,))
            conn.commit()
        finally:
            conn.close()
    
    def stop(self):
        """수신 중지"""
        self.running = False
        if self.ws:
            asyncio.create_task(self.ws.close())
        logger.info("WebSocket 수신 종료")
