#!/usr/bin/env python3
"""
무한매수법 V4.0 - 메인 매매 엔진
"""

import os
os.environ['TZ'] = 'Asia/Seoul'
import time
time.tzset()

import sys
import asyncio
import signal
import logging
import json
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field
from enum import Enum

# 3rd Party
import aiohttp
import aiosqlite

# 로깅
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler('logs/app.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('kbot_engine')

os.makedirs('logs', exist_ok=True)
os.makedirs('data', exist_ok=True)

# ==========================================================
# 설정
# ==========================================================

@dataclass(frozen=True)
class Config:
    PRINCIPAL_USD: float = float(os.getenv('PRINCIPAL_USD', '20000'))
    DIVISION: int = int(os.getenv('DIVISION', '40'))
    STOCK_CODE: str = os.getenv('STOCK_CODE', 'TQQQ')
    STOCK_TYPE: str = os.getenv('STOCK_TYPE', 'TQQQ')
    EXCHANGE: str = os.getenv('EXCHANGE', 'ND')
    
    FIRST_BUY_PREMIUM_PCT: float = 0.15
    CONDITIONAL_RETRY_PREMIUM_PCT: float = 0.15
    MAX_CRASH_ORDERS: int = 5
    
    # 수수료 (이론 [10] 완전 반영)
    FEE_PER_SHARE: float = float(os.getenv('FEE_PER_SHARE', '0.00555'))
    MIN_TRADE_FEE: float = float(os.getenv('MIN_TRADE_FEE', '2.95'))
    FEE_MODE: str = os.getenv('FEE_MODE', 'per_share')
    
    # 제세금 0 고정
    TAX_RATE: float = 0.0
    
    KIWOOM_APP_KEY: str = os.getenv('KIWOOM_APP_KEY', 'mock')
    KIWOOM_APP_SECRET: str = os.getenv('KIWOOM_APP_SECRET', 'mock')
    KIWOOM_ACCESS_TOKEN: str = os.getenv('KIWOOM_ACCESS_TOKEN', '')
    
    API_BASE_URL: str = 'https://api.kiwoom.com'
    DATABASE_URL: str = os.getenv('DATABASE_URL', 'sqlite:///./data/kbot.db')
    
    TELEGRAM_BOT_TOKEN: str = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID: str = os.getenv('TELEGRAM_CHAT_ID', '')
    TELEGRAM_ADMIN_CHAT_ID: str = os.getenv('TELEGRAM_ADMIN_CHAT_ID', '')
    
    MAX_RETRIES: int = 3
    RETRY_TIMEOUT: float = 45.0

CFG = Config()

# ==========================================================
# 수수료 계산기 (이론 [10] 완전 반영)
# ==========================================================

class FeeCalculator:
    """
    이론 [10]: "매도 대금이 발생하면 즉시 전략 잔금에 더해지며, 
    수수료와 제세금 차감 후 금액을 사용한다."
    """
    
    @staticmethod
    def calc_sell_fee(gross: float, quantity: int) -> float:
        """매도 수수료 계산"""
        if CFG.FEE_MODE == 'per_share':
            calculated = quantity * CFG.FEE_PER_SHARE
            fee = max(calculated, CFG.MIN_TRADE_FEE)
        else:
            fee = CFG.MIN_TRADE_FEE
        return round(fee, 2)
    
    @staticmethod
    def calc_sell_net(gross: float, quantity: int) -> float:
        """매도 순수익 = 매도금 - 수수료 (제세금 0)"""
        fee = FeeCalculator.calc_sell_fee(gross, quantity)
        tax = 0.0  # 제세금 0 고정
        return round(gross - fee - tax, 2)
    
    @staticmethod
    def format_fee_summary(gross: float, fee: float, net: float) -> str:
        return f"매도금액${gross:.2f} - 수수료${fee:.2f} = 순수익${net:.2f}"

# ==========================================================
# 상태 모델
# ==========================================================

class Mode(Enum):
    NORMAL = 'normal'
    REVERSE = 'reverse'

@dataclass
class State:
    mode: Mode = Mode.NORMAL
    t_value: float = 0.0
    principal: float = 20000.0
    balance: float = 20000.0
    total_quantity: int = 0
    avg_price: float = 0.0
    reverse_start_t: float = 0.0
    completed_cycles: int = 0
    total_realized_pnl: float = 0.0
    total_fees_paid: float = 0.0  # 누적 수수료
    stock_code: str = 'TQQQ'
    division: int = 40
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self):
        return {
            'mode': self.mode.value,
            't_value': self.t_value,
            'balance': self.balance,
            'total_quantity': self.total_quantity,
            'avg_price': self.avg_price,
            'total_fees_paid': self.total_fees_paid,
        }
    
    @classmethod
    def from_dict(cls, d: dict):
        s = cls()
        s.mode = Mode(d.get('mode', 'normal'))
        s.t_value = d.get('t_value', 0.0)
        s.principal = d.get('principal', 20000.0)
        s.balance = d.get('balance', 20000.0)
        s.total_quantity = d.get('total_quantity', 0)
        s.avg_price = d.get('avg_price', 0.0)
        s.total_fees_paid = d.get('total_fees_paid', 0.0)
        s.stock_code = d.get('stock_code', 'TQQQ')
        s.division = d.get('division', 40)
        return s

# ==========================================================
# DB
# ==========================================================

class Database:
    def __init__(self, db_path: str = './data/kbot.db'):
        self.db_path = db_path
    
    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    mode TEXT DEFAULT 'normal',
                    t_value REAL DEFAULT 0.0,
                    principal REAL DEFAULT 20000.0,
                    balance REAL DEFAULT 20000.0,
                    total_quantity INTEGER DEFAULT 0,
                    avg_price REAL DEFAULT 0.0,
                    total_fees_paid REAL DEFAULT 0.0,
                    stock_code TEXT DEFAULT 'TQQQ',
                    division INTEGER DEFAULT 40,
                    updated_at TEXT
                )
            ''')
            
            await db.execute('''
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT,
                    order_type TEXT,
                    side TEXT,
                    stock_code TEXT,
                    quantity INTEGER,
                    price REAL,
                    gross_amount REAL,
                    fee REAL DEFAULT 0.0,
                    net_amount REAL,
                    mode TEXT,
                    created_at TEXT
                )
            ''')
            
            await db.execute('''
                CREATE TABLE IF NOT EXISTS completed_cycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_number INTEGER UNIQUE,
                    stock_code TEXT,
                    start_date TEXT,
                    end_date TEXT,
                    realized_pnl REAL,
                    total_fees REAL
                )
            ''')
            
            await db.execute('''
                INSERT OR IGNORE INTO state (id, principal, balance, stock_code, division)
                VALUES (1, ?, ?, ?, ?)
            ''', (CFG.PRINCIPAL_USD, CFG.PRINCIPAL_USD, CFG.STOCK_CODE, CFG.DIVISION))
            
            await db.commit()
    
    async def load_state(self) -> Optional[State]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute('SELECT * FROM state WHERE id = 1') as cur:
                row = await cur.fetchone()
                return State.from_dict(dict(row)) if row else None
    
    async def save_state(self, state: State, reason: str = 'scheduled'):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT OR REPLACE INTO state 
                (id, mode, t_value, principal, balance, total_quantity, avg_price,
                total_fees_paid, stock_code, division, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (1, state.mode.value, state.t_value, state.principal,
                  state.balance, state.total_quantity, state.avg_price,
                  state.total_fees_paid, state.stock_code, state.division,
                  datetime.now().isoformat()))
            
            await db.execute('''
                INSERT INTO backups (backup_time, state_json, reason)
                VALUES (datetime('now'), ?, ?)
            ''', (json.dumps(state.to_dict()), reason))
            
            await db.commit()
    
    async def log_transaction(self, tx: Dict):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT INTO transactions
                (trade_date, order_type, side, stock_code, quantity, price,
                gross_amount, fee, net_amount, mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (datetime.now().strftime('%Y%m%d'), tx.get('order_type'),
                  tx.get('side'), tx.get('stock_code'), tx.get('quantity'),
                  tx.get('price'), tx.get('gross_amount', 0),
                  tx.get('fee', 0), tx.get('net_amount', 0), tx.get('mode')))
            await db.commit()
    
    async def get_today_transactions(self) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute('''
                SELECT * FROM transactions WHERE trade_date = ? ORDER BY created_at
            ''', (datetime.now().strftime('%Y%m%d'),)) as cur:
                return [dict(r) for r in await cur.fetchall()]
    
    async def get_total_fees(self) -> float:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute('SELECT SUM(fee) FROM transactions') as cur:
                row = await cur.fetchone()
                return row[0] or 0.0

# ==========================================================
# 키움 API
# ==========================================================

class TokenManager:
    def __init__(self):
        self._token: Optional[str] = None
        self._expires_at: Optional[datetime] = None
    
    async def initialize(self):
        if CFG.KIWOOM_ACCESS_TOKEN:
            self._token = CFG.KIWOOM_ACCESS_TOKEN
            self._expires_at = datetime.now() + timedelta(hours=23)
        else:
            await self._refresh()
    
    async def ensure_valid(self):
        if not self._token or not self._expires_at or datetime.now() > self._expires_at - timedelta(minutes=5):
            await self._refresh()
    
    async def _refresh(self):
        if CFG.KIWOOM_APP_KEY == 'mock':
            self._token = 'mock_token'
            self._expires_at = datetime.now() + timedelta(days=1)
            return
        
        url = f'{CFG.API_BASE_URL}/oauth2/token'
        async with aiohttp.ClientSession() as session:
            payload = {
                'grant_type': 'client_credentials',
                'appkey': CFG.KIWOOM_APP_KEY,
                'appsecret': CFG.KIWOOM_APP_SECRET,
            }
            async with session.post(url, data=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                data = await resp.json()
                self._token = data['access_token']
                expires = data.get('expires_in', 86400)
                self._expires_at = datetime.now() + timedelta(seconds=int(expires))
    
    @property
    def access_token(self) -> str:
        return f'Bearer {self._token}' if self._token else ''


class OrderAPI:
    def __init__(self, token_mgr: TokenManager):
        self.tm = token_mgr
    
    async def _request(self, api_id: str, body: Dict) -> Dict:
        await self.tm.ensure_valid()
        url = f'{CFG.API_BASE_URL}/api/us/ordr'
        headers = {
            'Content-Type': 'application/json;charset=UTF-8',
            'api-id': api_id,
            'authorization': self.tm.access_token,
        }
        
        if CFG.KIWOOM_APP_KEY == 'mock':
            return {'rv': '0', 'ord_no': f'MOCK_{datetime.now().strftime("%H%M%S")}'}
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=CFG.RETRY_TIMEOUT)) as resp:
                return await resp.json()
    
    async def loc_buy(self, stock_code: str, price: float, qty: int) -> Dict:
        return await self._request('ust20000', {
            'stex_tp': CFG.EXCHANGE,
            'stk_cd': stock_code,
            'ord_qty': str(qty),
            'trde_tp': '30',
            'ord_uv': str(price),
        })
    
    async def loc_sell(self, stock_code: str, price: float, qty: int) -> Dict:
        return await self._request('ust20001', {
            'stex_tp': CFG.EXCHANGE,
            'stk_cd': stock_code,
            'ord_qty': str(qty),
            'trde_tp': '30',
            'ord_uv': str(price),
        })
    
    async def moc_sell(self, stock_code: str, qty: int) -> Dict:
        return await self._request('ust20001', {
            'stex_tp': CFG.EXCHANGE,
            'stk_cd': stock_code,
            'ord_qty': str(qty),
            'trde_tp': '33',
        })


class MarketDataAPI:
    def __init__(self, token_mgr: TokenManager):
        self.tm = token_mgr
    
    async def get_current_price(self, stock_code: str) -> Dict:
        await self.tm.ensure_valid()
        if CFG.KIWOOM_APP_KEY == 'mock':
            return {'cur_prc': '50.0', 'pre_close': '49.0'}
        
        url = f'{CFG.API_BASE_URL}/api/dostk/ustk'
        headers = {
            'Content-Type': 'application/json;charset=UTF-8',
            'api-id': 'usa10009',
            'authorization': self.tm.access_token,
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json={'stk_cd': stock_code, 'stex_tp': CFG.EXCHANGE}) as resp:
                return await resp.json()
    
    async def get_previous_close(self, stock_code: str) -> float:
        data = await self.get_current_price(stock_code)
        return float(data.get('pre_close', data.get('cur_prc', 50.0)))
    
    async def get_5day_history(self, stock_code: str) -> List[float]:
        await self.tm.ensure_valid()
        if CFG.KIWOOM_APP_KEY == 'mock':
            return [50.0, 49.5, 51.0, 48.0, 52.0]
        
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=15)
        
        url = f'{CFG.API_BASE_URL}/api/dostk/ustk'
        headers = {
            'Content-Type': 'application/json;charset=UTF-8',
            'api-id': 'usa10007',
            'authorization': self.tm.access_token,
        }
        
        body = {
            'stk_cd': stock_code,
            'stex_tp': CFG.EXCHANGE,
            'strt_dt': start_dt.strftime('%Y%m%d'),
            'end_dt': end_dt.strftime('%Y%m%d'),
            'qry_tp': '0',
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=body) as resp:
                data = await resp.json()
                result = data.get('result_list', [])
                valid = [r for r in result if int(r.get('vol', 0)) > 0]
                valid.sort(key=lambda x: x.get('dt', ''))
                return [float(r['close']) for r in valid[-5:]]

# ==========================================================
# 계산기
# ==========================================================

class TValueCalc:
    @staticmethod
    def on_full_buy(t: float) -> float: return t + 1.0
    @staticmethod
    def on_half_buy(t: float) -> float: return t + 0.5
    @staticmethod
    def on_quarter_sell(t: float) -> float: return t * 0.75
    
    @staticmethod
    def reverse_sell(t: float, division: int) -> float:
        return t * 0.9 if division == 20 else t * 0.95
    
    @staticmethod
    def reverse_buy(t: float, division: int) -> float:
        return t + (division - t) * 0.25


class StarCalc:
    def __init__(self, stock_type: str, division: int):
        self.stock_type = stock_type
        self.division = division
    
    def normal_pct(self, t: float) -> float:
        if self.stock_type == 'TQQQ':
            return 15 - 1.5 * t if self.division == 20 else 15 - 0.75 * t
        else:
            return 20 - 2.0 * t if self.division == 20 else 20 - 1.0 * t
    
    def normal_star(self, avg: float, t: float) -> float:
        pct = self.normal_pct(t)
        return round(avg * (1 + pct / 100), 2)
    
    def buy_price(self, star: float) -> float:
        return round(star - 0.01, 2)

# ==========================================================
# 주문 실행 + 수수료 완전 반영
# ==========================================================

class OrderPlanner:
    @staticmethod
    def first_buy(prev_close: float, principal: float, division: int) -> List[Dict]:
        big_p = round(prev_close * 1.15, 2)
        one_buy = principal / division
        main_qty = max(int(one_buy / big_p), 1)
        
        plans = [{'type': 'FIRST_BUY_MAIN', 'price': big_p, 'qty': main_qty}]
        for i in range(1, CFG.MAX_CRASH_ORDERS + 1):
            crash_p = round(one_buy / (main_qty + i), 2)
            if crash_p >= big_p * 0.5:
                plans.append({'type': 'CRASH_BUY', 'price': crash_p, 'qty': 1})
        return plans
    
    @staticmethod
    def normal_buy(state: State, avg: float, star: float, balance: float) -> List[Dict]:
        one_buy = balance / (state.division - state.t_value)
        buy_p = round(star - 0.01, 2)
        
        if state.t_value < state.division / 2:
            star_qty = int((one_buy / 2) / buy_p) if buy_p > 0 else 0
            avg_qty = int((one_buy / 2) / avg) if avg > 0 else 0
            total_approx = int(one_buy / avg) if avg > 0 else 0
            if total_approx % 2 == 1:
                avg_qty += 1
            plans = [
                {'type': 'STAR_BUY', 'price': buy_p, 'qty': star_qty},
                {'type': 'AVG_BUY', 'price': avg, 'qty': avg_qty},
            ]
        else:
            main_qty = int(one_buy / buy_p) if buy_p > 0 else 0
            plans = [{'type': 'STAR_BUY', 'price': buy_p, 'qty': main_qty}]
        
        for i in range(1, 6):
            crash_p = round(buy_p * (0.9 ** i), 2)
            if crash_p >= buy_p * 0.5:
                plans.append({'type': 'CRASH_BUY', 'price': crash_p, 'qty': 1})
        return plans
    
    @staticmethod
    def normal_sell(state: State, avg: float, star: float) -> List[Dict]:
        q = round(state.total_quantity / 4)
        tgt_p = round(avg * 1.15 if state.stock_type == 'TQQQ' else avg * 1.20, 2)
        return [
            {'type': 'QUARTER_SELL', 'price': star, 'qty': q},
            {'type': 'TARGET_SELL', 'price': tgt_p, 'qty': state.total_quantity - q},
        ]
    
    @staticmethod
    def reverse_first_sell(state: State) -> Dict:
        divisor = 10 if state.division == 20 else 20
        return {'type': 'REVERSE_MOC', 'qty': state.total_quantity // divisor}
    
    @staticmethod
    def reverse_subsequent(prev_holdings: int, division: int, star: float, balance: float) -> List[Dict]:
        divisor = 10 if division == 20 else 20
        sell_qty = max(prev_holdings // divisor, 1)
        buy_p = round(star - 0.01, 2)
        buy_amount = balance / 4
        buy_qty = int(buy_amount / buy_p) if buy_p > 0 else 0
        return [
            {'type': 'REVERSE_SELL', 'price': star, 'qty': sell_qty},
            {'type': 'REVERSE_BUY', 'price': buy_p, 'qty': buy_qty},
        ]

# ==========================================================
# 시장 시간
# ==========================================================

class MarketTime:
    @staticmethod
    def is_summer() -> bool:
        now = datetime.utcnow() + timedelta(hours=9)
        year = now.year
        march = datetime(year, 3, 1)
        dst_start = march + timedelta(days=(6 - march.weekday()) + 7)
        nov = datetime(year, 11, 1)
        dst_end = nov + timedelta(days=(6 - nov.weekday()))
        return dst_start <= now.replace(tzinfo=None) < dst_end
    
    @staticmethod
    def get_pre_market_hour() -> int:
        return 17 if MarketTime.is_summer() else 18
    
    @staticmethod
    def kst_now() -> datetime:
        return datetime.utcnow() + timedelta(hours=9)

# ==========================================================
# 모드 전환
# ==========================================================

class ModeTransition:
    @staticmethod
    def is_exhausted(t: float, division: int) -> bool:
        return t > division - 1
    
    @staticmethod
    def is_reverse_exit(state: State, close: float) -> bool:
        threshold = 0.15 if state.stock_type == 'TQQQ' else 0.20
        return close >= state.avg_price * (1 - threshold) if state.avg_price > 0 else False

# ==========================================================
# 텔레그램 단방향 알림 (telegram_bot.py와 연동)
# ==========================================================

class TelegramNotifier:
    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self.bot = None
        if CFG.TELEGRAM_BOT_TOKEN:
            from telegram import Bot
            self.bot = Bot(token=CFG.TELEGRAM_BOT_TOKEN)
    
    async def start(self):
        if self.bot:
            self._task = asyncio.create_task(self._sender())
    
    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
    
    async def _sender(self):
        while True:
            try:
                msg = await self._queue.get()
                if msg.get('shutdown'):
                    break
                chat_id = msg.get('chat_id', CFG.TELEGRAM_CHAT_ID)
                if self.bot:
                    await self.bot.send_message(chat_id=chat_id, text=msg['text'], parse_mode='HTML')
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f'텔레그램 발송 실패: {e}')
    
    async def notify(self, text: str, level: str = 'info', chat_id: str = None):
        if not self.bot:
            return
        emojis = {'info': 'ℹ️', 'success': '✅', 'warning': '⚠️', 'error': '🚨'}
        emoji = emojis.get(level, '📋')
        await self._queue.put({
            'text': f"{emoji} <b>[{level.upper()}]</b>\n{text}",
            'chat_id': chat_id or (CFG.TELEGRAM_ADMIN_CHAT_ID if level in ('error', 'critical') else CFG.TELEGRAM_CHAT_ID)
        })

# ==========================================================
# 메인 엔진
# ==========================================================

class KBotEngine:
    def __init__(self):
        self.db = Database()
        self.state: Optional[State] = None
        self.token_mgr = TokenManager()
        self.order_api = OrderAPI(self.token_mgr)
        self.market_api = MarketDataAPI(self.token_mgr)
        self.telegram = TelegramNotifier()
        self.is_running = False
        self.is_reverse_first = False
        self.scheduler = None
    
    async def init(self):
        await self.db.init()
        self.state = await self.db.load_state()
        if not self.state:
            self.state = State()
            self.state.principal = CFG.PRINCIPAL_USD
            self.state.balance = CFG.PRINCIPAL_USD
            self.state.stock_code = CFG.STOCK_CODE
            self.state.division = CFG.DIVISION
            await self.db.save_state(self.state, 'initial')
        
        await self.token_mgr.initialize()
        await self.telegram.start()
        logger.info(f'엔진 초기화: T={self.state.t_value}, mode={self.state.mode.value}')
    
    async def pre_market(self):
        now = MarketTime.kst_now()
        expected = MarketTime.get_pre_market_hour()
        if now.hour != expected:
            return
        
        logger.info(f'프리장 주문: {now.strftime("%H:%M")}')
        s = self.state
        
        # 리버스 첫날 MOC
        if s.mode == Mode.REVERSE and self.is_reverse_first:
            plan = OrderPlanner.reverse_first_sell(s)
            result = await self.order_api.moc_sell(s.stock_code, plan['qty'])
            await self.telegram.notify(
                f'🔴 리버스 MOC: {plan["qty"]}주, 주문={result.get("ord_no")}',
                'warning'
            )
            self.is_reverse_first = False
            old_t = s.t_value
            s.t_value = TValueCalc.reverse_sell(s.t_value, s.division)
            await self.db.log_transaction({
                'order_type': 'REVERSE_MOC', 'side': 'SELL', 'stock_code': s.stock_code,
                'quantity': plan['qty'], 'price': 0, 'gross_amount': 0,
                'fee': 0, 'net_amount': 0, 'mode': s.mode.value
            })
            await self.db.save_state(s, 'reverse_moc')
            return
        
        # 전일 종가
        prev_close = await self.market_api.get_previous_close(s.stock_code)
        if s.mode == Mode.NORMAL:
            await self._normal_orders(prev_close)
        else:
            await self._reverse_orders()
    
    async def _normal_orders(self, prev_close: float):
        s = self.state
        sc = StarCalc(s.stock_type, s.division)
        
        if s.total_quantity == 0 and s.t_value == 0:
            plans = OrderPlanner.first_buy(prev_close, s.principal, s.division)
        else:
            star = sc.normal_star(s.avg_price, s.t_value)
            buys = OrderPlanner.normal_buy(s, s.avg_price, star, s.balance)
            sells = OrderPlanner.normal_sell(s, s.avg_price, star)
            plans = sells + buys
        
        for plan in plans:
            result = None
            if 'BUY' in plan['type'] or 'CRASH' in plan['type']:
                result = await self.order_api.loc_buy(s.stock_code, plan['price'], plan['qty'])
                if result.get('rv') == '0' and 'CRASH' not in plan['type']:
                    s.t_value = TValueCalc.on_full_buy(s.t_value)
            elif 'SELL' in plan['type']:
                result = await self.order_api.loc_sell(s.stock_code, plan['price'], plan['qty'])
                if result.get('rv') == '0':
                    # ★★★ 수수료 완전 반영 ★★★
                    gross = plan['price'] * plan['qty']
                    fee = FeeCalculator.calc_sell_fee(gross, plan['qty'])
                    net = FeeCalculator.calc_sell_net(gross, plan['qty'])
                    
                    s.total_fees_paid += fee
                    s.balance += net
                    s.total_quantity -= plan['qty']
                    s.t_value = TValueCalc.on_quarter_sell(s.t_value)
                    s.total_realized_pnl += net - (plan['qty'] * s.avg_price)
                    
                    # 수수료 로깅
                    logger.info(FeeCalculator.format_fee_summary(gross, fee, net))
            
            if result:
                logger.info(f'주문: {plan["type"]} {plan["qty"]}주 @ ${plan["price"]} rv={result.get("rv")}')
        
        await self.telegram.notify(f'📋 일반모드 주문 {len(plans)}건', 'info')
    
    async def _reverse_orders(self):
        s = self.state
        closes = await self.market_api.get_5day_history(s.stock_code)
        if len(closes) < 5:
            await self.telegram.notify('⚠️ 5거래일 데이터 부족', 'warning')
            return
        
        star = StarCalc(s.stock_type, s.division).reverse_star(closes)
        plans = OrderPlanner.reverse_subsequent(s.total_quantity, s.division, star, s.balance)
        
        for plan in plans:
            if 'SELL' in plan['type']:
                result = await self.order_api.loc_sell(s.stock_code, plan['price'], plan['qty'])
                if result.get('rv') == '0':
                    gross = plan['price'] * plan['qty']
                    fee = FeeCalculator.calc_sell_fee(gross, plan['qty'])
                    net = FeeCalculator.calc_sell_net(gross, plan['qty'])
                    s.total_fees_paid += fee
                    s.balance += net
                    s.total_quantity -= plan['qty']
                    s.t_value = TValueCalc.reverse_sell(s.t_value, s.division)
            else:
                result = await self.order_api.loc_buy(s.stock_code, plan['price'], plan['qty'])
                if result.get('rv') == '0':
                    s.t_value = TValueCalc.reverse_buy(s.t_value, s.division)
        
        await self.telegram.notify(f'📋 리버스모드 주문 {len(plans)}건', 'info')
    
    async def end_of_day(self):
        s = self.state
        data = await self.market_api.get_current_price(s.stock_code)
        close = float(data.get('cur_prc', s.avg_price))
        
        txs = await self.db.get_today_transactions()
        today_fees = sum(t.get('fee', 0) for t in txs)
        
        # 모드 전환
        if s.mode == Mode.NORMAL and ModeTransition.is_exhausted(s.t_value, s.division):
            s.mode = Mode.REVERSE
            s.reverse_start_t = s.t_value
            await self.db.save_state(s, 'to_reverse')
            await self.telegram.notify(f'🔴 리버스 진입 T={s.t_value:.4f}', 'warning')
            self.is_reverse_first = True
        elif s.mode == Mode.REVERSE and ModeTransition.is_reverse_exit(s, close):
            s.mode = Mode.NORMAL
            await self.db.save_state(s, 'to_normal')
            await self.telegram.notify(f'🟢 일반 복귀 T={s.t_value:.4f}', 'success')
        
        await self.db.save_state(s, 'eod')
        await self.telegram.notify(
            f'📊 EOD\n잔금: ${s.balance:.2f}\n수수료: ${today_fees:.2f}\nT: {s.t_value:.4f}',
            'info'
        )
    
    async def start(self):
        self.is_running = True
        await self.init()
        await self.telegram.notify(
            f'🟢 KBot 시작\nT: {self.state.t_value:.4f}\\n잔금: ${self.state.balance:.2f}',
            'success'
        )
        while self.is_running:
            await asyncio.sleep(60)
    
    async def stop(self):
        self.is_running = False
        await self.telegram.stop()
        if self.state:
            await self.db.save_state(self.state, 'shutdown')
        logger.info('엔진 종료')

# ==========================================================
# 진입점
# ==========================================================

async def main():
    engine = KBotEngine()
    loop = asyncio.get_event_loop()
n    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(engine.stop()))
    
    try:
        await engine.start()
    except Exception as e:
        logger.critical(f'치명적 오류: {e}', exc_info=True)
        await engine.stop()
        raise

if __name__ == '__main__':
    asyncio.run(main())
