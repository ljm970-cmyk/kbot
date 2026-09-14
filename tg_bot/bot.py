#!/usr/bin/env python3
"""
================================================================
KBOT 텔레그램 봇 - 완성 버전

기존 [5] telegram_bot.py 구조 확장
================================================================
"""

import asyncio
import logging
import sys
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)

# 프로젝트 루트 설정
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import AppConfig
from tg_bot.commands_handler import CommandsHandler
from tg_bot.setup_wizard import SetupWizard

logger = logging.getLogger("kbot.telegram")


class KbotTelegramBot:
    """
    텔레그램 봇 메인 컨트롤러
    
    사용법 (main.py에서):
        from tg_bot.bot import KbotTelegramBot
        from core.state_manager import StateManager
        from kiwoom.api_client import KiwoomAPIClient
        
        state_mgr = StateManager()
        kiwoom = KiwoomAPIClient(config.kiwoom)
        
        bot = KbotTelegramBot(config, state_mgr, kiwoom, scheduler, ws_receiver)
        await bot.run()
    """

    def __init__(self, config: AppConfig, state_manager, kiwoom_api,
                 scheduler=None, ws_receiver=None):
        """
        Args:
            config: AppConfig 객체
            state_manager: StateManager 인스턴스
            kiwoom_api: KiwoomAPIClient 인스턴스
            scheduler: SchedulerEngine (선택)
            ws_receiver: WebSocketFillReceiver (선택)
        """
        self.config = config
        self.state = state_manager
        self.kiwoom = kiwoom_api
        self.scheduler = scheduler
        self.ws = ws_receiver
        
        # 핸들러 초기화 (실제 객체 주입)
        self.cmd_handler = CommandsHandler(state_manager, kiwoom_api, config)
        self.setup_wizard = SetupWizard(state_manager)
        
        # Application 빌드
        self.application = Application.builder().token(
            config.telegram.bot_token
        ).build()
        
        self._setup_handlers()
    
    def _setup_handlers(self):
        """핸들러 등록"""
        app = self.application
        
        # ========== 핵심 명령어 ==========
        app.add_handler(CommandHandler("status", self.cmd_handler.cmd_status))
        app.add_handler(CommandHandler("st", self.cmd_handler.cmd_status))
        app.add_handler(CommandHandler("orders", self.cmd_handler.cmd_orders))
        app.add_handler(CommandHandler("history", self.cmd_handler.cmd_history))
        app.add_handler(CommandHandler("hist", self.cmd_handler.cmd_history))
        app.add_handler(CommandHandler("config", self.cmd_handler.cmd_config))
        app.add_handler(CommandHandler("fix", self.cmd_handler.cmd_fix))
        app.add_handler(CommandHandler("calceod", self.cmd_handler.cmd_force_calc))
        
        # 기존 호환
        app.add_handler(CommandHandler("sync", self.cmd_handler.cmd_status))
        app.add_handler(CommandHandler("record", self.cmd_handler.cmd_history))
        
        # ========== 대화형 설정 마법사 ==========
        setup_handler = self.setup_wizard.get_handler()
        app.add_handler(setup_handler)
        
        # ========== 콜백 ==========
        app.add_handler(CallbackQueryHandler(self._handle_callback))
        
        # ========== 한글 자연어 ==========
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            self._handle_message
        ))
        
        # ========== 에러 ==========
        app.add_error_handler(self._error_handler)
    
    # ============================================================
    # 메시지 라우팅 (기존 [5] 확장)
    # ============================================================
    
    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """한글 자연어 명령 라우팅"""
        if not update.effective_message or not update.effective_message.text:
            return
        
        text = update.effective_message.text.strip()
        
        # 무한매수법 핵심
        if "상태" in text or "현황" in text or "관제탑" in text:
            return await self.cmd_handler.cmd_status(update, context)
        elif "주문내역" in text or "예약주문" in text:
            return await self.cmd_handler.cmd_orders(update, context)
        elif "히스토리" in text or "거래내역" in text:
            return await self.cmd_handler.cmd_history(update, context)
        elif "수동" in text or "보정" in text or "fix" in text.lower():
            return await self.cmd_handler.cmd_fix(update, context)
        elif "설정" in text or "분할" in text or "원금" in text or "수수료" in text:
            return await self.cmd_handler.cmd_config(update, context)
        elif "동기화" in text or "싱크" in text:
            return await self.cmd_handler.cmd_status(update, context)
        elif "계산" in text or "강제계산" in text:
            return await self.cmd_handler.cmd_force_calc(update, context)
        elif "도움" in text or "help" in text.lower():
            return await self._cmd_help(update, context)
        
        # 기존 호환
        elif "통합 지시서" in text or "지시서 조회" in text:
            return await self.cmd_handler.cmd_status(update, context)
        elif "장부 동기화" in text or "장부 조회" in text:
            return await self.cmd_handler.cmd_history(update, context)
        
        else:
            await update.effective_message.reply_text(
                "알 수 없는 명령입니다. /help 로 명령어를 확인하세요."
            )
    
    # ============================================================
    # 콜백 처리
    # ============================================================
    
    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """InlineKeyboardButton 콜백"""
        logger.info(f"[콜백수신] data={update.callback_query.data if update.callback_query else None}")
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        if data == "SYNC:NOW" or data.startswith("SYNC"):
            await self.cmd_handler.cmd_status(update, context)
        
        elif data.startswith("ORDERS:"):
            # 특정 종목 주문 내역
            ticker = data.split(":")[1] if ":" in data else None
            if ticker:
                context.args = [ticker]
            await self.cmd_handler.cmd_orders(update, context)
        
        elif data.startswith("CONFIG:"):
            action = data.split(":")[1] if ":" in data else None
            ticker = data.split(":")[2] if data.count(":") >= 2 else None
            
            if action == "EDIT" and ticker:
                # 설정 편집 메뉴
                await self.cmd_handler.cmd_config(update, context)
            else:
                await self.cmd_handler.cmd_config(update, context)
        
        elif data == "CALC:FORCE":
            await self.cmd_handler.cmd_force_calc(update, context)
        
        elif data.startswith("HIST:"):
            await self.cmd_handler.cmd_history(update, context)
        
        elif data == "RESET:CANCEL":
            await query.edit_message_text("❌ 취소됨")
        
        elif data.startswith("MODE:"):
            # 모드 전환 (수동)
            pass
        
        else:
            await query.edit_message_text(f"처리: {data}")
    
    # ============================================================
    # 도움말
    # ============================================================
    
    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """📖 도움말"""
        msg = (
            "🤖 <b>[kbot 명령어 안내]</b>\n\n"
            "<b>━━ 핵심 명령 ━━</b>\n"
            "/status, /st — 관제탑 현황\n"
            "/orders — 예약주문 내역\n"
            "/history, /hist — 거래 히스토리\n\n"
            "<b>━━ 설정/보정 ━━</b>\n"
            "/start — 초기 설정 (단독/동시 선택)\n"
            "/config — 설정 변경 (분할/원금/수수료)\n"
            "/fix — 수동 거래 보정\n\n"
            "<b>━━ 관리 ━━</b>\n"
            "/calceod — 강제 EOD 계산\n"
            "/sync — 상태 동기화\n\n"
            "<b>━━ 한글 입력 가능 ━━</b>\n"
            "'상태', '현황', '관제탑'\n"
            "'주문내역', '히스토리', '수동', '보정'\n"
            "'설정', '분할', '원금', '수수료', '계산'"
        )
        await update.effective_message.reply_text(msg, parse_mode='HTML')
    
    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """에러 핸들러"""
        logger.error(f"텔레그램 에러: {context.error}")
    
    # ============================================================
    # 실행
    # ============================================================
    
    async def run(self):
        """봇 시작 (메인 블로킹)"""
        logger.info("텔레그램 봇 polling 시작...")
        
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            logger.info("취소 요청")
        finally:
            await self.application.stop()


# ============================================================
# 팩토리 함수 (의존성 주입 편의)
# ============================================================

async def create_telegram_bot(config: AppConfig, 
                            state_manager=None, 
                            kiwoom_api=None,
                            scheduler=None,
                            ws_receiver=None):
    """
    KBOT 텔레그램 봇 팩토리
    
    사용법:
        from tg_bot.bot import create_telegram_bot
        from config.settings import ConfigLoader
        from core.state_manager import StateManager
        from kiwoom.api_client import KiwoomAPIClient
        
        config = ConfigLoader.load()
        state_mgr = StateManager()
        kiwoom = KiwoomAPIClient(config.kiwoom)
        
        bot = await create_telegram_bot(config, state_mgr, kiwoom)
        await bot.run()
    """
    if state_manager is None:
        from core.state_manager import StateManager
        state_manager = StateManager()
    
    if kiwoom_api is None:
        from kiwoom.api_client import KiwoomAPIClient
        kiwoom_api = KiwoomAPIClient(config.kiwoom)
    
    return KbotTelegramBot(
        config=config,
        state_manager=state_manager,
        kiwoom_api=kiwoom_api,
        scheduler=scheduler,
        ws_receiver=ws_receiver
    )
