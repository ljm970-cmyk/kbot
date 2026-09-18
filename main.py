#!/usr/bin/env python3
"""
================================================================
kbot — 무한매수법 자동매매

USAGE
    python main.py                  포그라운드 실행
    python main.py --dry-run        주문 미전송 (계획만 확인)
    python main.py --mock           모의투자 서버
    python main.py --check          기동 점검만 하고 종료

ENVIRONMENT
    .env 탐색 순서
      1. KBOT_ENV_FILE
      2. <프로젝트 루트>/.env
      3. /opt/kbot/.env

    주요 변수
      KIWOOM_APP_KEY / KIWOOM_APP_SECRET   (필수)
      TELEGRAM_BOT_TOKEN / TELEGRAM_ADMIN_ID (필수)
      KIWOOM_MOCK=true      모의투자
      KBOT_DRY_RUN=true     주문 미전송
      KBOT_HOME / KBOT_DATA_DIR / KBOT_LOG_DIR

기존 구현에서 고친 것
  - authenticate() 가 async 가 되었는데 await 없이 호출해
    코루틴 객체(항상 truthy)를 검사하고 있었다. 인증 실패를 못 잡고
    빈 토큰으로 WebSocket 까지 넘어갔다.
  - SchedulerEngine 에 kiwoom/state/eod 를 주입하지 않아
    스케줄러가 아무것도 실행할 수 없었다.
  - 로그 파일에 로테이션이 없어 디스크가 계속 찼다.
  - 종료 시 API 세션을 닫지 않았다.
================================================================
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import signal
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import AppConfig, ConfigLoader  # noqa: E402


# ================================================================
# 로깅
# ================================================================

def setup_logging(log_dir: Path, level: int = logging.INFO) -> logging.Logger:
    """콘솔 + 로테이팅 파일 + 에러 전용 파일.

    기존 코드는 basicConfig 로 핸들러를 지정한 뒤 root 에 하나를 더
    붙여 중복 기록이 생겼고, 로테이션이 없어 로그가 무한히 쌓였다.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    app = logging.handlers.RotatingFileHandler(
        log_dir / "app.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    app.setFormatter(fmt)
    root.addHandler(app)

    err = logging.handlers.RotatingFileHandler(
        log_dir / "error.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    err.setLevel(logging.WARNING)
    err.setFormatter(fmt)
    root.addHandler(err)

    # 외부 라이브러리 소음 억제
    for noisy in ("httpx", "apscheduler.executors.default", "telegram.ext"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("kbot")


# ================================================================
# 기동 점검
# ================================================================

async def preflight(kiwoom, logger: logging.Logger) -> bool:
    """실제 주문을 내기 전에 배관이 살아있는지 확인한다.

    api_client 는 문서만 보고 쓴 코드라 한 번도 실행된 적이 없다.
    필드명·거래소구분·응답 파싱이 맞는지 여기서 먼저 드러난다.
    """
    from kiwoom.constants import EXCHANGE_MAP, exchange_of

    ok = True
    for ticker in EXCHANGE_MAP:
        try:
            q = await kiwoom.get_quote(ticker, exchange_of(ticker))
            if q["cur_price"] <= 0 and q["prev_close"] <= 0:
                logger.error("  %s 시세가 0입니다 — stex_tp(%s) 가 틀렸을 수 있습니다",
                             ticker, exchange_of(ticker))
                ok = False
            else:
                logger.info("  %s  현재가 %.2f  전일종가 %.2f  (%s)",
                            ticker, q["cur_price"], q["prev_close"], exchange_of(ticker))
        except Exception as e:
            logger.error("  %s 시세 조회 실패: %s", ticker, e)
            ok = False

    try:
        d = await kiwoom.get_deposit_usd()
        logger.info("  USD 예수금 D+0 $%.2f", d["d0_usd"])
    except Exception as e:
        logger.error("  예수금 조회 실패: %s", e)
        ok = False

    return ok


# ================================================================
# 실행
# ================================================================

async def run(config: AppConfig, check_only: bool = False) -> int:
    logger = logging.getLogger("kbot")

    from core.order_registry import OrderRegistry
    from core.state_manager import StateManager
    from eod.calculator import EndOfDayCalculator
    from kiwoom.api_client import KiwoomAPIClient
    from kiwoom.websocket_handler import WebSocketFillReceiver
    from scheduler.engine import SchedulerEngine
    from tg_bot.bot import create_telegram_bot

    from core.ops import mark_started
    mark_started()          # /health 의 가동시간 기준점

    logger.info("설정\n%s", config.summary())

    # --- 키움 API ---
    kiwoom = KiwoomAPIClient(config.kiwoom)
    try:
        await kiwoom.start()          # 세션 생성 + 토큰 발급 (반드시 await)
    except Exception as e:
        logger.error("키움 인증 실패: %s", e)
        return 1
    logger.info("키움 인증 성공 (만료 %s)", kiwoom.token_expiry)

    scheduler = None
    ws_receiver = None
    ws_task = None

    try:
        logger.info("기동 점검")
        healthy = await preflight(kiwoom, logger)
        if check_only:
            logger.info("점검 %s", "통과" if healthy else "실패")
            return 0 if healthy else 1
        if not healthy:
            logger.warning("기동 점검에 실패했지만 계속 진행합니다. "
                           "실주문 전에 반드시 원인을 확인하세요.")

        # --- 저장소 ---
        state_mgr = StateManager(config.paths.data)
        registry = OrderRegistry(config.paths.orders_db)
        eod = EndOfDayCalculator(registry)

        # --- WebSocket 실시간 체결 ---
        ws_receiver = WebSocketFillReceiver(
            access_token=lambda: kiwoom.token,      # 갱신된 토큰을 항상 따라간다
            db_path=str(config.paths.fills_db),
            ws_url=config.kiwoom.websocket_url,
        )
        ws_task = asyncio.create_task(ws_receiver.connect())

        # --- 스케줄러 (의존성 주입) ---
        scheduler = SchedulerEngine(
            config=config,
            kiwoom=kiwoom,
            state_mgr=state_mgr,
            registry=registry,
            eod=eod,
            notifier=None,          # 봇 생성 후 연결
            fill_source=getattr(ws_receiver, "fills_for", None),
        )

        # --- 텔레그램 봇 ---
        bot = await create_telegram_bot(
            config=config,
            state_manager=state_mgr,
            kiwoom_api=kiwoom,
            scheduler=scheduler,
            ws_receiver=ws_receiver,
        )

        # 봇이 알림 채널을 제공하면 스케줄러에 연결한다
        notifier = getattr(bot, "notifier", None)
        if notifier is not None:
            scheduler.notifier = notifier
        else:
            logger.warning("텔레그램 알림 채널을 찾지 못했습니다. "
                           "주문 결과가 로그에만 남습니다.")

        scheduler.start()
        logger.info("스케줄러 시작\n%s", scheduler.preview())

        if config.dry_run:
            logger.warning("DRY RUN — 주문은 전송되지 않습니다")

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass        # Windows

        logger.info("-" * 60)
        bot_task = asyncio.create_task(bot.run())
        await asyncio.wait([bot_task, asyncio.create_task(stop.wait())],
                           return_when=asyncio.FIRST_COMPLETED)
        if bot_task.done() and bot_task.exception():
            raise bot_task.exception()
        return 0

    except KeyboardInterrupt:
        logger.info("사용자 중단")
        return 0
    except Exception as e:
        logger.exception("예상치 못한 오류: %s", e)
        return 1
    finally:
        logger.info("종료 중")
        if scheduler:
            scheduler.shutdown()
        if ws_receiver:
            try:
                ws_receiver.stop()
            except Exception:
                pass
        if ws_task:
            ws_task.cancel()
        await kiwoom.close()          # 세션 정리 (기존 코드에 없었다)
        logger.info("kbot 종료")


# ================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="kbot — 무한매수법 자동매매")
    parser.add_argument("--dry-run", action="store_true", help="주문을 전송하지 않는다")
    parser.add_argument("--mock", action="store_true", help="모의투자 서버 사용")
    parser.add_argument("--check", action="store_true", help="기동 점검만 하고 종료")
    parser.add_argument("--env", help=".env 경로 직접 지정")
    parser.add_argument("--debug", action="store_true", help="DEBUG 로그")
    args = parser.parse_args()

    import os
    if args.dry_run:
        os.environ["KBOT_DRY_RUN"] = "true"
    if args.mock:
        os.environ["KIWOOM_MOCK"] = "true"

    try:
        config = ConfigLoader.load(args.env)
    except Exception as e:
        print(f"설정 로드 실패: {e}", file=sys.stderr)
        sys.exit(1)

    logger = setup_logging(config.paths.logs,
                           logging.DEBUG if args.debug else logging.INFO)
    logger.info("=" * 60)
    logger.info("kbot 시작")
    logger.info("=" * 60)

    sys.exit(asyncio.run(run(config, check_only=args.check)))


if __name__ == "__main__":
    main()
