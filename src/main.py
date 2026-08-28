#!/usr/bin/env python3
"""
무한매수법 V4.0 텔레그램 제어 봇
KST 영구 고정 (클라우드 UTC 초기화 방지)
"""

# ═══════════════════════════════════════════════════════
# 0단계: KST 선언 (모든 import보다 최상단)
# ═══════════════════════════════════════════════════════
import os
import sys
import time

os.environ["TZ"] = "Asia/Seoul"

try:
    time.tzset()
except AttributeError:
    pass  # Windows는 tzset() 없음

# ═══════════════════════════════════════════════════════
# 1단계: KST timezone 객체 생성 (전역)
# ═══════════════════════════════════════════════════════
from zoneinfo import ZoneInfo
KST = ZoneInfo("Asia/Seoul")

# ═══════════════════════════════════════════════════════
# 2단계: 시작 시각 검증 로그
# ═══════════════════════════════════════════════════════
from datetime import datetime

boot_kst = datetime.now(KST)
print(f"=" * 60, flush=True)
print(f"[BOOT] 봇 부팅", flush=True)
print(f"[BOOT] KST: {boot_kst.strftime('%Y-%m-%d %H:%M:%S %Z')}", flush=True)
print(f"[BOOT] TZ env: {os.environ.get('TZ', 'NOT SET')}", flush=True)
print(f"=" * 60, flush=True)

# ═══════════════════════════════════════════════════════
# 3단계: 정상 import
# ═══════════════════════════════════════════════════════
import asyncio
import logging
from pathlib import Path

from config.settings import load_config
from telegram.bot import V4TelegramBot


def setup_logging():
    """로깅 설정"""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        handlers=[
            logging.FileHandler(
                log_dir / f"v4bot_{datetime.now():%Y%m%d}.log",
                encoding="utf-8"
            ),
            logging.StreamHandler(sys.stdout)
        ]
    )
    return logging.getLogger("v4bot")


async def main():
    """메인 진입점"""
    logger = setup_logging()
    logger.info("🚀 무한매수법 V4.0 봇 시작")
    
    # 설정 로드
    config = load_config(logger)
    
    # 텔레그램 봇
    bot = V4TelegramBot(
        token=config.telegram_token,
        allowed_chat_ids=config.chat_ids
    )
    
    try:
        logger.info("텔레그램 봇 폴링 시작...")
        await bot.start()
    except KeyboardInterrupt:
        logger.info("사용자 중단")
    except Exception as e:
        logger.error(f"치명적 오류: {e}", exc_info=True)
    finally:
        logger.info("봇 종료")


if __name__ == "__main__":
    asyncio.run(main())