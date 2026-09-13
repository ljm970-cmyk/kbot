"""
================================================================
InfiniteBuyState CRUD

런타임 상태 관리 + Cloud Storage 백업
기존 [4]의 _save_queue_ledger, _save_blink_account 
패턴을 확장한 영속 저장소
================================================================
"""

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pytz


@dataclass
class TickerConfig:
    """종목별 설정 (사용자 입력)"""
    user_id: str
    ticker: str
    division: int  # 20 or 40
    principal: float
    fee_rate: float  # 0.0007
    fee_display: float  # 0.07 (표시용)
    seed: float = 0
    run_mode: str = "single"  # single or both
    settings: dict = field(default_factory=dict)
    created_at: str = ""


@dataclass
class TickerState:
    """종목별 실행 상태 (런타임)"""
    user_id: str
    ticker: str
    division: int
    principal: float
    fee_rate: float
    mode: str = "normal"  # normal or reverse
    T: float = 0.0
    avg_price: float = 0.0
    holdings: int = 0
    cash: float = 0.0
    is_active: bool = True
    reverse_first_day: bool = False
    reverse_start_T: float = 0.0
    next_orders: list = field(default_factory=list)
    history: list = field(default_factory=list)
    last_eod: str = ""
    manual_corrections: list = field(default_factory=list)


class StateManager:
    """
    상태 관리자
    
    기존 [4]의 locked_accounts, blink_account 구조를
    JSON 파일 기반으로 일반화
    """
    
    DATA_DIR = Path("/opt/kbot/data")
    
    def __init__(self):
        for subdir in ["config", "state", "orders", "fills"]:
            (self.DATA_DIR / subdir).mkdir(parents=True, exist_ok=True)
    
    # ============================================================
    # 설정 CRUD
    # ============================================================
    
    def save_ticker_config(self, user_id: str, ticker: str, config: dict) -> None:
        """사용자 초기 설정 저장"""
        path = self.DATA_DIR / "config" / f"{user_id}_{ticker}_config.json"
        config['user_id'] = user_id
        config['ticker'] = ticker
        config['created_at'] = datetime.now(pytz.timezone('Asia/Seoul')).isoformat()
        
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False, default=str)
    
    def get_ticker_config(self, user_id: str, ticker: str) -> Optional[dict]:
        """종목 설정 조회"""
        path = self.DATA_DIR / "config" / f"{user_id}_{ticker}_config.json"
        if not path.exists():
            return None
        
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def get_user_tickers(self, user_id: str) -> list[str]:
        """사용자의 활성 종목 목록"""
        pattern = f"{user_id}_*_config.json"
        configs = list(self.DATA_DIR / "config" / pattern)
        
        tickers = []
        for cfg_path in configs:
            with open(cfg_path, 'r') as f:
                cfg = json.load(f)
                if cfg.get('is_active', True):
                    tickers.append(cfg['ticker'])
        
        return sorted(tickers)
    
    # ============================================================
    # 상태 CRUD
    # ============================================================
    
    def save_state(self, user_id: str, ticker: str, state: dict) -> None:
        """런타임 상태 저장"""
        path = self.DATA_DIR / "state" / f"{user_id}_{ticker}_state.json"
        
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2, ensure_ascii=False, default=str)
    
    def get_state(self, user_id: str, ticker: str) -> Optional[dict]:
        """현재 상태 조회 (없으면 초기화)"""
        path = self.DATA_DIR / "state" / f"{user_id}_{ticker}_state.json"
        
        if not path.exists():
            # 설정 기반 초기 상태 생성
            cfg = self.get_ticker_config(user_id, ticker)
            if not cfg:
                return None
            
            initial = {
                'user_id': user_id,
                'ticker': ticker,
                'division': cfg['division'],
                'principal': cfg['principal'],
                'fee_rate': cfg['fee_rate'],
                'mode': 'normal',
                'T': 0.0,
                'avg_price': 0.0,
                'holdings': 0,
                'cash': cfg['principal'],
                'is_active': True,
                'next_orders': [],
                'history': [],
                'last_eod': '',
                'manual_corrections': [],
            }
            self.save_state(user_id, ticker, initial)
            return initial
        
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    # ============================================================
    # 수동 보정
    # ============================================================
    
    def add_manual_correction(self, user_id: str, ticker: str, 
                            correction: dict) -> bool:
        """
        수동 거래 보정 (기존 [4] cmd_insert 대응)
        
        correction: {
            'date': '20250624',
            'qty': 10,
            'price': 150.50,
            'side': 'buy' or 'sell',
            'type': 'MANUAL_FIX'
        }
        """
        state = self.get_state(user_id, ticker)
        if not state:
            return False
        
        state['manual_corrections'].append({
            **correction,
            'added_at': datetime.now(pytz.timezone('Asia/Seoul')).isoformat()
        })
        
        self.save_state(user_id, ticker, state)
        return True
    
    # ============================================================
    # EOD 아카이브
    # ============================================================
    
    def archive_eod(self, user_id: str, ticker: str, 
                    date: datetime, result: dict) -> None:
        """일별 계산 결과 저장"""
        date_str = date.strftime('%Y%m%d')
        path = self.DATA_DIR / "state" / f"EOD_{date_str}.jsonl"
        
        record = {
            'user_id': user_id,
            'ticker': ticker,
            'date': date_str,
            'result': result,
            'archived_at': datetime.now(pytz.timezone('Asia/Seoul')).isoformat()
        }
        
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
    
    # ============================================================
    # 백업
    # ============================================================
    
    def get_principal(self, user_id: str, ticker: str) -> float:
        """원금 조회"""
        cfg = self.get_ticker_config(user_id, ticker)
        return cfg.get('principal', 0.0) if cfg else 0.0
    
    def deactivate_ticker(self, user_id: str, ticker: str) -> None:
        """종목 비활성화 (사이클 종료 후)"""
        state = self.get_state(user_id, ticker)
        if state:
            state['is_active'] = False
            self.save_state(user_id, ticker, state)
