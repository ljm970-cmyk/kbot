#!/usr/bin/env python3
"""
================================================================
kbot - 무한매수법 자동 매매 시스템
================================================================

메인 진입점. 모든 서비스 초기화 및 실행.

USAGE:
    python main.py              # 포그라운드 실행
    python main.py --daemon     # 백그라운드 (systemd용)

ENVIRONMENT:
    /opt/kbot/.env (VM에 직접 배포, Secret Manager 미사용)

DEPENDENCIES:
    - .env 파일 (KIWOOM_APP_KEY, KIWOOM_APP_SECRET, TELEGRAM_BOT_TOKEN)
    - data/ 디렉토리 (런타임 생성)

REFERENCE:
    [1] 키움증권 REST API 문서
    [2] 무한매수법 V4.0 리버스 모드
    [3] 무한매수법 V4.0 일반모드
    [4] telegram_commands.py (기존 패턴 재사용)
    [5] telegram_bot.py (기존 라우팅 패턴 재사용)
================================================================
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# 경로 설정
PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import ConfigLoader


def setup_logging():
    """로깅 설정"""
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    
    # vm 배포 시 /var/log/kbot 사용
    vm_log_dir = Path("/var/log/kbot")
    if vm_log_dir.parent.exists():
        log_dir = vm_log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / "app.log", encoding='utf-8'),
        ]
    )
    
    # 에러 별도 파일
    error_handler = logging.FileHandler(log_dir / "error.log", encoding='utf-8')
    error_handler.setLevel(logging.WARNING)
    
    root_logger = logging.getLogger()
    root_logger.addHandler(error_handler)
    
    return logging.getLogger("kbot")


async def main_async():
    """비동기 메인 루프"""
    logger = setup_logging()
    logger.info("=" * 60)
    logger.info("kbot 무한매수법 시스템 시작")
    logger.info(f"프로젝트 루트: {PROJECT_ROOT}")
    logger.info("=" * 60)
    
    # 설정 로드
    try:
        config = ConfigLoader.load()
        logger.info(f"설정 로드 완료")
        logger.info(f"  API: {config.kiwoom.api_base}")
        logger.info(f"  관리자: {config.telegram.admin_id}")
    except Exception as e:
        logger.error(f"설정 로드 실패: {e}")
        sys.exit(1)
    
    # 데이터 디렉토리 생성
    for subdir in ["config", "state", "orders", "fills"]:
        (PROJECT_ROOT / "data" / subdir).mkdir(parents=True, exist_ok=True)
    
    # ============================================================
    # 의존성 초기화
    # ============================================================
    
    from core.state_manager import StateManager
    from kiwoom.api_client import KiwoomAPIClient
    from kiwoom.websocket_handler import WebSocketFillReceiver
    from scheduler.engine import SchedulerEngine
    from tg_bot.bot import create_telegram_bot
    
    # 상태 관리
    state_mgr = StateManager()
    
    # 키움 API
    kiwoom = KiwoomAPIClient(config.kiwoom)
    auth_result = kiwoom.authenticate()
    if not auth_result:
        logger.error("키움 인증 실패")
        sys.exit(1)
    
    logger.info("키움 인증 성공")
    
    # 스케줄러
    scheduler = SchedulerEngine(config)
    scheduler.start()
    logger.info("스케줄러 시작")
    
    # WebSocket (F5 실시간 체결)
    ws_receiver = WebSocketFillReceiver(
        access_token=kiwoom.token,
        db_path=str(PROJECT_ROOT / "data" / "fills" / "realtime_fills.db")
    )
    asyncio.create_task(ws_receiver.connect())
    logger.info("WebSocket 수신기 시작")
    
    # 텔레그램 봇 (팩토리 함수)
    bot = await create_telegram_bot(
        config=config,
        state_manager=state_mgr,
        kiwoom_api=kiwoom,
        scheduler=scheduler,
        ws_receiver=ws_receiver
    )
    
    logger.info("텔레그램 봇 초기화 완료")
    logger.info("-" * 60)
    
    # ============================================================
    # 메인 실행
    # ============================================================
    
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("사용자 중단 요청 (Ctrl+C)")
    except Exception as e:
        logger.exception(f"예상치 못한 오류: {e}")
    finally:
        logger.info("종료 중...")
        ws_receiver.stop()
        scheduler.shutdown()
        logger.info("kbot 종료 완료")


def main():
    """동기 진입점"""
    parser = argparse.ArgumentParser(description="kbot - 무한매수법 자동 매매")
    parser.add_argument("--daemon", action="store_true", help="백그라운드 모드")
    args = parser.parse_args()
    
    if args.daemon:
        # python-daemon 라이브러리 필요: pip install python-daemon
        try:
            import daemon
            with daemon.DaemonContext():
                asyncio.run(main_async())
        except ImportError:
            print("python-daemon 설치 필요: pip install python-daemon")
            sys.exit(1)
    else:
        asyncio.run(main_async())


if __name__ == "__main__":
    main()
