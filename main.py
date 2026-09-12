#!/usr/bin/env python3
"""
무한매수법 V4.0 - 통합 실행 파일
키움증권 REST API 기반 미국주식 자동매매
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
from datetime import datetime, timedelta, time as dt_time
from typing import Optional, List, Dict
from dataclasses import dataclass, field
from enum import Enum

try:
    import aiohttp
    import aiosqlite
    from telegram import Bot
    from telegram.constants import ParseMode
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    import websockets
except ImportError as e:
    print(f"필수 패키지 설치 필요: {e}")
    print("pip install -r requirements.txt")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler('logs/app.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('infinite_buy_v4')
os.makedirs('logs', exist_ok=True)
os.makedirs('data', exist_ok=True)

@dataclass(frozen=True)
class Config:
    PRINCIPAL_USD: float = float(os.getenv('PRINCIPAL_USD', '20000'))
    DIVISION: int = int(os.getenv('DIVISION', '40'))
    STOCK_CODE: str = os.getenv('STOCK_CODE', 'TQQQ')
    STOCK_TYPE: str = os.getenv('STOCK_TYPE', 'TQQQ')
    EXCHANGE: str = os.getenv('EXCHANGE', 'ND')
    COMPOUND_MODE: str = os.getenv('COMPOUND_MODE', 'compound')
    FIRST_BUY_PREMIUM_PCT: float = 0.15
    CONDITIONAL_RETRY_PREMIUM_PCT: float = 0.15
    MAX_CRASH_ORDERS: int = 5
    FEE_PER_SHARE: float = float(os.getenv('FEE_PER_SHARE', '0.00555'))
    MIN_TRADE_FEE: float = float(os.getenv('MIN_TRADE_FEE', '2.95'))
    TELEGRAM_BOT_TOKEN: str = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID: str = os.getenv('TELEGRAM_CHAT_ID', '')
    TELEGRAM_ADMIN_CHAT_ID: str = os.getenv('TELEGRAM_ADMIN_CHAT_ID', '')
    KIWOOM_APP_KEY: str = os.getenv('KIWOOM_APP_KEY', '')
    KIWOOM_APP_SECRET: str = os.getenv('KIWOOM_APP_SECRET', '')
    KIWOOM_ACCESS_TOKEN: str = os.getenv('KIWOOM_ACCESS_TOKEN', '')
    API_BASE_URL: str = 'https://api.kiwoom.com'
    DATABASE_URL: str = os.getenv('DATABASE_URL', 'sqlite:///./data/infinite_buy.db')
    PRE_MARKET_ORDER_SUMMER: dt_time = dt_time(17, 0)
    PRE_MARKET_ORDER_WINTER: dt_time = dt_time(18, 0)
    MAX_RETRIES: int = 3
    RETRY_TIMEOUT: float = 45.0

CFG = Config()

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
    stock_code: str = 'TQQQ'
    division: int = 40
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self):
        return {
            'mode': self.mode.value,
            't_value': self.t_value,
            'principal': self.principal,
            'balance': self.balance,
            'total_quantity': self.total_quantity,
            'avg_price': self.avg_price,
            'completed_cycles': self.completed_cycles,
            'reverse_start_t': self.reverse_start_t,
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
        s.completed_cycles = d.get('completed_cycles', 0)
        s.reverse_start_t = d.get('reverse_start_t', 0.0)
        s.stock_code = d.get('stock_code', 'TQQQ')
        s.division = d.get('division', 40)
        return s

class Database:
    def __init__(self, db_path: str = './data/infinite_buy.db'):
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
                    reverse_start_t REAL DEFAULT 0.0,
                    completed_cycles INTEGER DEFAULT 0,
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
                    t_before REAL,
                    t_after REAL,
                    mode TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS completed_cycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_number INTEGER UNIQUE,
                    stock_code TEXT,
                    start_date TEXT,
                    end_date TEXT,
                    principal REAL,
                    final_balance REAL,
                    pnl REAL,
                    return_pct REAL
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS backups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    backup_time TEXT DEFAULT (datetime('now')),
                    state_json TEXT,
                    reason TEXT
                )
            ''')
            await db.execute('''
                INSERT OR IGNORE INTO state (id, mode, principal, balance, stock_code, division)
                VALUES (1, 'normal', ?, ?, ?, ?)
            ''', (CFG.PRINCIPAL_USD, CFG.PRINCIPAL_USD, CFG.STOCK_CODE, CFG.DIVISION))
            await db.commit()
    
    async def load_state(self) -> Optional[State]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute('SELECT * FROM state WHERE id = 1') as cur:
                row = await cur.fetchone()
                if row:
                    return State.from_dict(dict(row))
                return None
    
    async def save_state(self, state: State, reason: str = 'scheduled'):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT OR REPLACE INTO state 
                (id, mode, t_value, principal, balance, total_quantity, avg_price,
                 reverse_start_t, completed_cycles, stock_code, division, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (1, state.mode.value, state.t_value, state.principal,
                  state.balance, state.total_quantity, state.avg_price,
                  state.reverse_start_t, state.completed_cycles,
                  state.stock_code, state.division, datetime.now().isoformat()))
            await db.execute('''
                INSERT INTO backups (state_json, reason)
                VALUES (?, ?)
            ''', (json.dumps(state.to_dict()), reason))
            await db.execute('''
                DELETE FROM backups WHERE backup_time < datetime('now', '-30 days')
            ''')
            await db.commit()
    
    async def log_transaction(self, tx: Dict):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT INTO transactions
                (trade_date, order_type, side, stock_code, quantity, price, t_before, t_after, mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (datetime.now().strftime('%Y%m%d'), tx.get('order_type'),
                  tx.get('side'), tx.get('stock_code'), tx.get('quantity'),
                  tx.get('price'), tx.get('t_before'), tx.get('t_after'), tx.get('mode')))
            await db.commit()

class TokenManager:
    def __init__(self):
        self._token: Optional[str] = None
        self._expires_at: Optional[datetime] = None
    
    async def initialize(self):
        if CFG.KIWOOM_ACCESS_TOKEN:
            self._token = CFG.KIWOOM_ACCESS_TOKEN
            self._expires_at = datetime.now() + timedelta(hours=23)
            logger.info('토큰 초기화 (환경변수)')
        else:
            await self._refresh()
    
    async def ensure_valid(self):
        if not self._token or not self._expires_at or datetime.now() > self._expires_at - timedelta(minutes=5):
            await self._refresh()
    
    async def _refresh(self):
        if not CFG.KIWOOM_APP_KEY or not CFG.KIWOOM_APP_SECRET:
            logger.warning('키움 APP_KEY/SECRET 미설정, 목업 모드')
            self._token = 'mock_token'
            self._expires_at = datetime.now() + timedelta(days=1)
            return
        async with aiohttp.ClientSession() as session:
            url = f'{CFG.API_BASE_URL}/oauth2/token'
            payload = {
                'grant_type': 'client_credentials',
                'appkey': CFG.KIWOOM_APP_KEY,
                'appsecret': CFG.KIWOOM_APP_SECRET,
            }
            try:
                async with session.post(url, data=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    data = await resp.json()
                    self._token = data['access_token']
                    expires = data.get('expires_in', 86400)
                    self._expires_at = datetime.now() + timedelta(seconds=int(expires))
                    logger.info('토큰 갱신 완료')
            except Exception as e:
                logger.error(f'토큰 갱신 실패: {e}')
                raise
    
    @property
    def access_token(self) -> str:
        if not self._token:
            raise RuntimeError('토큰 미설정')
        return f'Bearer {self._token}'

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
            return {'rv': '0', 'ord_no': f'MOCK_{datetime.now().strftime("%H%M%S")}', 'return_msg': 'mock'}
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
    
    async def cancel(self, order_no: str, stock_code: str) -> Dict:
        return await self._request('ust20003', {
            'orig_ord_no': order_no,
            'stex_tp': CFG.EXCHANGE,
            'stk_cd': stock_code,
        })

class AccountAPI:
    def __init__(self, token_mgr: TokenManager):
        self.tm = token_mgr
    
    async def get_balance(self) -> float:
        await self.tm.ensure_valid()
        if CFG.KIWOOM_APP_KEY == 'mock':
            return CFG.PRINCIPAL_USD
        url = f'{CFG.API_BASE_URL}/api/us/acnt'
        headers = {
            'Content-Type': 'application/json;charset=UTF-8',
            'api-id': 'ust21110',
            'authorization': self.tm.access_token,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json={'fc_krw_tp': '0'}) as resp:
                data = await resp.json()
                return float(data.get('avl_fc', CFG.PRINCIPAL_USD))

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
            body = {'stk_cd': stock_code, 'stex_tp': CFG.EXCHANGE}
            async with session.post(url, headers=headers, json=body) as resp:
                return await resp.json()
    
    async def get_previous_close(self, stock_code: str) -> float:
        data = await self.get_current_price(stock_code)
        return float(data.get('pre_close', data.get('cur_prc', 50.0)))
    
    async def get_5day_history(self, stock_code: str) -> List[float]:
        await self.tm.ensure_valid()
        if CFG.KIWOOM_APP_KEY == 'mock':
            return [50.0, 49.5, 51.0, 48.0, 52.0]
        url = f'{CFG.API_BASE_URL}/api/dostk/ustk'
        headers = {
            'Content-Type': 'application/json;charset=UTF-8',
            'api-id': 'usa10007',
            'authorization': self.tm.access_token,
        }
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=15)
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

class TValueCalc:
    @staticmethod
    def on_full_buy(t: float) -> float: return t + 1.0
    @staticmethod
    def on_half_buy(t: float) -> float: return t + 0.5
    @staticmethod
    def on_quarter_sell(t: float) -> float: return t * 0.75
    @staticmethod
    def target_then_full(t: float) -> float: return t * 0.25 + 1.0
    @staticmethod
    def target_then_half(t: float) -> float: return t * 0.25 + 0.5
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
    
    def reverse_star(self, closes: List[float]) -> float:
        if len(closes) < 5:
            raise ValueError(f'5거래일 필요, 현재 {len(closes)}')
        return round(sum(closes) / 5, 2)

class OrderPlanner:
    @staticmethod
    def first_buy(prev_close: float, principal: float, division: int) -> List[Dict]:
        big_p = round(prev_close * 1.15, 2)
        one_buy = principal / division
        main_qty = max(int(one_buy / big_p), 1)
        plans = [{'type': 'FIRST_BUY_MAIN', 'price': big_p, 'qty': main_qty, 'trde_tp': '30'}]
        for i in range(1, min(CFG.MAX_CRASH_ORDERS + 1, 6)):
            crash_p = round(one_buy / (main_qty + i), 2)
            if crash_p >= big_p * 0.5:
                plans.append({'type': 'CRASH_BUY', 'price': crash_p, 'qty': 1, 'trde_tp': '30'})
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
                {'type': 'STAR_BUY', 'price': buy_p, 'qty': star_qty, 'trde_tp': '30'},
                {'type': 'AVG_BUY', 'price': avg, 'qty': avg_qty, 'trde_tp': '30'},
            ]
        else:
            main_qty = int(one_buy / buy_p) if buy_p > 0 else 0
            plans = [{'type': 'STAR_BUY', 'price': buy_p, 'qty': main_qty, 'trde_tp': '30'}]
        for i in range(1, 6):
            crash_p = round(buy_p * (0.9 ** i), 2)
            if crash_p >= buy_p * 0.5:
                plans.append({'type': 'CRASH_BUY', 'price': crash_p, 'qty': 1, 'trde_tp': '30'})
        return plans
    
    @staticmethod
    def normal_sell(state: State, avg: float, star: float) -> List[Dict]:
        q = round(state.total_quantity / 4) if state.total_quantity > 0 else 0
        tgt_p = round(avg * 1.15 if state.stock_type == 'TQQQ' else avg * 1.20, 2)
        return [
            {'type': 'QUARTER_SELL', 'price': star, 'qty': q, 'trde_tp': '30'},
            {'type': 'TARGET_SELL', 'price': tgt_p, 'qty': state.total_quantity - q, 'trde_tp': '00'},
        ]
    
    @staticmethod
    def reverse_first_sell(state: State) -> Dict:
        divisor = 10 if state.division == 20 else 20
        qty = state.total_quantity // divisor
        return {'type': 'REVERSE_MOC', 'qty': qty, 'trde_tp': '33'}
    
    @staticmethod
    def reverse_subsequent(prev_holdings: int, division: int, star: float, balance: float) -> List[Dict]:
        divisor = 10 if division == 20 else 20
        sell_qty = max(prev_holdings // divisor, 1)
        buy_p = round(star - 0.01, 2)
        buy_amount = balance / 4
        buy_qty = int(buy_amount / buy_p) if buy_p > 0 else 0
        return [
            {'type': 'REVERSE_SELL', 'price': star, 'qty': sell_qty, 'trde_tp': '30'},
            {'type': 'REVERSE_BUY', 'price': buy_p, 'qty': buy_qty, 'trde_tp': '30'},
        ]

class TelegramBot:
    def __init__(self):
        self.bot = Bot(token=CFG.TELEGRAM_BOT_TOKEN) if CFG.TELEGRAM_BOT_TOKEN else None
        self.chat_id = CFG.TELEGRAM_CHAT_ID
        self.admin_id = CFG.TELEGRAM_ADMIN_CHAT_ID or CFG.TELEGRAM_CHAT_ID
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
    
    async def start(self):
        if self.bot:
            self._task = asyncio.create_task(self._sender())
    
    async def stop(self):
        if self._task:
            self._task.cancel()
    
    async def _sender(self):
        while True:
            try:
                msg = await self._queue.get()
                await self._send(msg)
                await asyncio.sleep(0.04)
            except asyncio.CancelledError:
                break
    
    async def _send(self, msg: Dict):
        if not self.bot:
            return
        try:
            await self.bot.send_message(
                chat_id=msg.get('chat_id', self.chat_id),
                text=msg['text'],
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f'텔레그램 발송 실패: {e}')
    
    async def notify(self, text: str, level: str = 'info'):
        emojis = {'info': 'ℹ️', 'success': '✅', 'warning': '⚠️', 'error': '🚨', 'critical': '‼️'}
        emoji = emojis.get(level, '📋')
        await self._queue.put({
            'text': f"{emoji} {text}",
            'chat_id': self.admin_id if level in ('error', 'critical') else self.chat_id
        })
    
    async def send_start(self, state: State):
        await self.notify(
            f"🟢 *무한매수법 V4.0 시작*\n"
            f"모드: {state.mode.value}\n"
            f"T값: {state.t_value:.4f}\n"
            f"잔금: ${state.balance:.2f}",
            'info'
        )

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

class ModeTransition:
    @staticmethod
    def is_exhausted(t: float, division: int) -> bool:
        return t > division - 1
    
    @staticmethod
    def is_reverse_exit(state: State, close: float) -> bool:
        threshold = 0.15 if state.stock_type == 'TQQQ' else 0.20
        return close >= state.avg_price * (1 - threshold) if state.avg_price > 0 else False
    
    @staticmethod
    async def to_reverse(state: State, db: Database, bot: TelegramBot):
        state.mode = Mode.REVERSE
        state.reverse_start_t = state.t_value
        await db.save_state(state, 'to_reverse')
        await bot.notify(
            f'🔴 리버스모드 진입\nT: {state.t_value:.4f}\n첫날 MOC 매도 예정',
            'warning'
        )
    
    @staticmethod
    async def to_normal(state: State, db: Database, bot: TelegramBot):
        state.mode = Mode.NORMAL
        await db.save_state(state, 'to_normal')
        await bot.notify(
            f'🟢 일반모드 복귀\nT: {state.t_value:.4f}\n잔금: ${state.balance:.2f}',
            'success'
        )

class TradingScheduler:
    def __init__(self, app):
        self.app = app
        self.sch = AsyncIOScheduler(timezone='Asia/Seoul')
    
    def setup(self):
        self.sch.add_job(
            self.app.pre_market,
            CronTrigger(hour='17,18', minute=0),
            id='pre_market'
        )
        self.sch.add_job(
            self.app.end_of_day,
            CronTrigger(hour='5,6', minute=5),
            id='eod'
        )
        self.sch.add_job(
            self.app.check_moc,
            CronTrigger(hour='5,6', minute=10),
            id='check_moc'
        )
        self.sch.add_job(
            lambda: asyncio.create_task(self.app.backup()),
            'interval', minutes=5,
            id='backup'
        )
    
    def start(self):
        self.sch.start()
    
    def shutdown(self):
        self.sch.shutdown()

class InfiniteBuyApp:
    def __init__(self):
        self.db = Database()
        self.state: Optional[State] = None
        self.token_mgr = TokenManager()
        self.order_api = OrderAPI(self.token_mgr)
        self.account_api = AccountAPI(self.token_mgr)
        self.market_api = MarketDataAPI(self.token_mgr)
        self.telegram = TelegramBot()
        self.scheduler = TradingScheduler(self)
        self.is_running = False
        self.is_reverse_first = False
    
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
        logger.info(f'상태: T={self.state.t_value}, mode={self.state.mode.value}')
    
    async def pre_market(self):
        now = MarketTime.kst_now()
        expected = MarketTime.get_pre_market_hour()
        if now.hour != expected:
            return
        logger.info(f'프리장 주문: {now.strftime("%H:%M")}')
        if self.state.mode == Mode.REVERSE and self.is_reverse_first:
            plan = OrderPlanner.reverse_first_sell(self.state)
            result = await self.order_api.moc_sell(self.state.stock_code, plan['qty'])
            await self.telegram.notify(f'🔴 리버스 MOC: {plan["qty"]}주')
            self.is_reverse_first = False
            await self.db.log_transaction({
                'order_type': 'REVERSE_MOC', 'side': 'SELL',
                'stock_code': self.state.stock_code, 'quantity': plan['qty'],
                'price': 0, 't_before': self.state.t_value,
                't_after': TValueCalc.reverse_sell(self.state.t_value, self.state.division),
                'mode': self.state.mode.value
            })
            return
        prev_close = await self.market_api.get_previous_close(self.state.stock_code)
        if self.state.mode == Mode.NORMAL:
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
            plans = OrderPlanner.normal_buy(s, s.avg_price, star, s.balance)
            sells = OrderPlanner.normal_sell(s, s.avg_price, star)
            plans = sells + plans
        for plan in plans:
            if 'BUY' in plan['type']:
                result = await self.order_api.loc_buy(s.stock_code, plan['price'], plan['qty'])
            elif 'SELL' in plan['type']:
                result = await self.order_api.loc_sell(s.stock_code, plan['price'], plan['qty'])
            else:
                continue
            logger.info(f'주문: {plan["type"]} {plan["qty"]}주 @ {plan["price"]}')
        await self.telegram.notify(f'📋 일반모드 주문 {len(plans)}건')
    
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
            else:
                result = await self.order_api.loc_buy(s.stock_code, plan['price'], plan['qty'])
        await self.telegram.notify(f'📋 리버스모드 주문 {len(plans)}건')
    
    async def end_of_day(self):
        now = MarketTime.kst_now()
        logger.info(f'장 마감 정산: {now.strftime("%H:%M")}')
        s = self.state
        data = await self.market_api.get_current_price(s.stock_code)
        close = float(data.get('cur_prc', s.avg_price))
        if s.mode == Mode.NORMAL and ModeTransition.is_exhausted(s.t_value, s.division):
            await ModeTransition.to_reverse(s, self.db, self.telegram)
            self.is_reverse_first = True
        elif s.mode == Mode.REVERSE and ModeTransition.is_reverse_exit(s, close):
            await ModeTransition.to_normal(s, self.db, self.telegram)
        await self.db.save_state(s, 'eod')
        await self.telegram.notify(
            f'📊 EOD 정산\nT: {s.t_value:.4f}\n잔금: ${s.balance:.2f}\n보유: {s.total_quantity}주'
        )
    
    async def check_moc(self):
        pass
    
    async def backup(self):
        if self.state:
            await self.db.save_state(self.state, 'scheduled')
    
    async def start(self):
        self.is_running = True
        await self.init()
        await self.telegram.send_start(self.state)
        self.scheduler.setup()
        self.scheduler.start()
        logger.info('시스템 실행 중')
    
    async def stop(self):
        self.is_running = False
        self.scheduler.shutdown()
        await self.telegram.stop()
        if self.state:
            await self.db.save_state(self.state, 'shutdown')
        logger.info('시스템 종료')

async def main():
    app = InfiniteBuyApp()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(app.stop()))
    try:
        await app.start()
        while app.is_running:
            await asyncio.sleep(60)
    except Exception as e:
        logger.critical(f'치명적 오류: {e}', exc_info=True)
        await app.stop()
        raise

if __name__ == '__main__':
    asyncio.run(main())
