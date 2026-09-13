#!/usr/bin/env python3
"""
================================================================
kbot - 무한매수법 자동 매매 시스템
================================================================

USAGE:
    python main.py              # 포그라운드 실행
    python main.py --daemon     # 백그라운드 (systemd용)

DEPENDENCIES:
    - .env 파일 (VM에 직접 배포, Secret Manager 미사용)
    - data/ 디렉토리 (런타임 생성)

REFERENCE:
    [1] 키움증권 REST API 문서 (키움 REST API 문서.xlsx)
    [2] 무한매수법 V4.0 리버스 모드
    [3] 무한매수법 V4.0 일반모드
    [4] telegram_commands.py (기존 패턴 재사용)
    [5] telegram_bot.py (기존 라우팅 패턴 재사용)
================================================================
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# 프로젝트 루트를 Python 경로에 추가
PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import ConfigLoader
from scheduler.engine import SchedulerEngine
from kiwoom.websocket_handler import WebSocketFillReceiver
from telegram.bot import KbotTelegramBot


def setup_logging():
    """로깅 설정"""
    log_dir = Path("/var/log/kbot")
    log_dir.mkdir(parents=True, exist_ok=True)
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / "app.log", encoding='utf-8'),
            logging.FileHandler(log_dir / "error.log", encoding='utf-8'),
        ]
    )
    
    # 에러 로그는 WARNING 이상만
    for handler in logging.root.handlers:
        if isinstance(handler, logging.FileHandler) and "error" in handler.baseFilename:
            handler.setLevel(logging.WARNING)
    
    return logging.getLogger("kbot")


async def main_async():
    """비동기 메인 루프"""
    logger = setup_logging()
    logger.info("=" * 60)
    logger.info("kbot 무한매수법 시스템 시작")
    logger.info("=" * 60)
    
    # 설정 로드
    try:
        config = ConfigLoader.load()
        logger.info(f"설정 로드 완료: {config.kiwoom.api_base}")
    except Exception as e:
        logger.error(f"설정 로드 실패: {e}")
        sys.exit(1)
    
    # 데이터 디렉토리 생성
    for subdir in ["config", "state", "orders", "fills"]:
        (PROJECT_ROOT / "data" / subdir).mkdir(parents=True, exist_ok=True)
    
    # 스케줄러 시작
    scheduler = SchedulerEngine(config)
    scheduler.start()
    logger.info("스케줄러 시작 완료")
    
    # WebSocket (백그라운드)
    ws_receiver = None
    if config.kiwoom.app_key:
        ws_receiver = WebSocketFillReceiver(
            access_token="",  # 런타임 갱신
            db_path=str(PROJECT_ROOT / "data" / "fills" / "realtime_fills.db")
        )
        asyncio.create_task(ws_receiver.connect())
        logger.info("WebSocket 수신기 백그라운드 시작")
    
    # 텔레그램 봇 (메인 블로킹)
    bot = KbotTelegramBot(config, scheduler, ws_receiver)
    logger.info("텔레그램 봇 시작...")
    
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("사용자 중단 요청")
    finally:
        if ws_receiver:
            ws_receiver.stop()
        scheduler.shutdown()
        logger.info("kbot 종료")


def main():
    """동기 진입점"""
    parser = argparse.ArgumentParser(description="kbot - 무한매수법 자동 매매")
    parser.add_argument("--daemon", action="store_true", help="백그라운드 모드")
    args = parser.parse_args()
    
    if args.daemon:
        # systemd에서 실행 시 stdout 리다이렉션
        import daemon
        with daemon.DaemonContext():
            asyncio.run(main_async())
    else:
        asyncio.run(main_async())


if __name__ == "__main__":
    main()
