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
    # 추가
    app_key: str
    app_secret: str
    base_url: str


def load_config(logger):
    """.env 파일에서 설정 로드"""
    
    # .env 파일 로드 (기존과 동일)
    env_path = ".env"
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key, value)
    
    # === 텔레그램 설정 ===
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token or "봇아빠" in token or "입력" in token:
        logger.error("TELEGRAM_BOT_TOKEN 실제 값 필요")
        raise ValueError("TELEGRAM_BOT_TOKEN 미설정")
    
    chat_ids_str = os.getenv("TELEGRAM_CHAT_ID", "")
    if not chat_ids_str or "본인" in chat_ids_str or "입력" in chat_ids_str:
        logger.error("TELEGRAM_CHAT_ID 실제 값 필요")
        raise ValueError("TELEGRAM_CHAT_ID 미설정")
    
    chat_ids = [int(x.strip()) for x in chat_ids_str.split(",") if x.strip()]
    
    # === 키움증권 설정: 오타 수정 ===
    mode = os.getenv("KIWOOM_MODE", "mock")  # KIWOOOM → KIWOOM 수정
    
    is_mock = (mode != "real")  # real가 아니면 모의
    
    if is_mock:
        base_url = os.getenv("MOCK", "https://mockapi.kiwoom.com")
        app_key = os.getenv("APP_KEY_MOCK", "")
        app_secret = os.getenv("APP_SECRET_MOCK", "")
        logger.info("모의투자 모드")
    else:
        base_url = os.getenv("PRD", "https://openapi.kiwoom.com")
        app_key = os.getenv("APP_KEY", "")
        app_secret = os.getenv("APP_SECRET", "")
        logger.warning("실전투자 모드")
    
    # === 실전 필수 검증 ===
    if not is_mock:
        if not app_key or "입력" in app_key or "실제" in app_key:
            logger.error("실전 APP_KEY 미입력")
            raise ValueError("실전 APP_KEY 필요")
        if not app_secret or "입력" in app_secret or "실제" in app_secret:
            logger.error("실전 APP_SECRET 미입력")
            raise ValueError("실전 APP_SECRET 필요")
    
    # === 토큰 (없으면 발급 필요) ===
    kiwoom_token = os.getenv("KIWOOOM_ACCESS_TOKEN", "")
    if not kiwoom_token:
        logger.info("KIWOOOM_ACCESS_TOKEN 없음 - 봇 실행 시 발급 예정")
    
    logger.info(f"설정 완료: mode={'mock' if is_mock else 'real'}, url={base_url}")
    
    return BotConfig(
        telegram_token=token,
        chat_ids=chat_ids,
        kiwoom_token=kiwoom_token,
        is_mock=is_mock,
        app_key=app_key,
        app_secret=app_secret,
        base_url=base_url
    )
