"""JSON 기반 상태 영속화"""

import json
from pathlib import Path
from decimal import Decimal
from dataclasses import dataclass, asdict
from typing import Optional


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        return super().default(obj)


class StateManager:
    BASE_DIR = Path("data/states")
    
    def __init__(self, account_no: str, ticker: str):
        self.account_no = account_no
        self.ticker = ticker
        self.file_path = self.BASE_DIR / f"{account_no}_{ticker}_state.json"
        self.BASE_DIR.mkdir(parents=True, exist_ok=True)
    
    def save(self, state_dict: dict):
        with open(self.file_path, 'w', encoding='utf-8') as f:
            json.dump(state_dict, f, cls=DecimalEncoder, indent=2)
    
    def load(self) -> Optional[dict]:
        if not self.file_path.exists():
            return None
        with open(self.file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def backup(self):
        import shutil
        from datetime import datetime
        backup_dir = self.BASE_DIR / "backups"
        backup_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(self.file_path, backup_dir / f"{self.ticker}_{timestamp}.json")