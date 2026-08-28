"""환경변수 설정 로드"""

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import List


@dataclass
class BotConfig:
    telegram_token: str
    chat_ids: List[int]
    kiwoom_token: str
    is_mock: bool


def load_config(logger):
    """.env 파일에서 설정 로드"""
    
    env_path = ".env"
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key, value)
    
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token or "your_" in token:
        logger.error("TELEGRAM_BOT_TOKEN 설정 필요")
        raise ValueError("TELEGRAM_BOT_TOKEN 미설정")
    
    chat_ids_str = os.getenv("TELEGRAM_CHAT_ID", "")
    if not chat_ids_str or "your_" in chat_ids_str:
        logger.error("TELEGRAM_CHAT_ID 설정 필요")
        raise ValueError("TELEGRAM_CHAT_ID 미설정")
    
    chat_ids = [int(x.strip()) for x in chat_ids_str.split(",") if x.strip()]
    
    logger.info(f"설정 로드 완료: {len(chat_ids)}명 사용자")
    
    return BotConfig(
        telegram_token=token,
        chat_ids=chat_ids,
        kiwoom_token=os.getenv("KIWOOOM_ACCESS_TOKEN", ""),
        is_mock=os.getenv("KIWOOOM_MODE", "mock") == "mock"
    )