"""
================================================================
TelegramController (진입점)

기존 [5]의 TelegramController 구조를 확장
- setup_handlers: 16개 기존 + 6개 신규
- handle_message: 한글 자연어 라우팅
- handle_callback: 버튼 콜백
================================================================
"""

import logging
import os

from telegram import Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)

from config.settings import AppConfig
from telegram.commands_handler import CommandsHandler
from telegram.setup_wizard import SetupWizard

logger = logging.getLogger("kbot.telegram")


class KbotTelegramBot:
    """
    텔레그램 봇 메인 컨트롤러
    
    기존 [5] 구조:
    - __init__ → setup_handlers → run
    - handle_message → 한글 라우팅
    - handle_callback → 버튼 처리
    """
    
    def __init__(self, config: AppConfig, scheduler, ws_receiver):
        self.config = config
        self.scheduler = scheduler
        self.ws = ws_receiver
        
        # 핸들러 초기화
        self.cmd_handler = CommandsHandler(None, None, config)  # TODO: DI
        self.setup_wizard = SetupWizard(None)  # TODO: DI
        
        # Application 빌드
        self.application = Application.builder().token(
            config.telegram.bot_token
        ).build()
        
        self._setup_handlers()
    
    def _setup_handlers(self):
        """핸들러 등록 (기존 [5] setup_handlers 확장)"""
        app = self.application
        
        # ========== 명령어 (기존 16개 + 신규 6개) ==========
        
        # 핵심
        app.add_handler(CommandHandler("status", self.cmd_handler.cmd_status))
        app.add_handler(CommandHandler("st", self.cmd_handler.cmd_status))
        
        # 조회
        app.add_handler(CommandHandler("orders", self.cmd_handler.cmd_orders))
        app.add_handler(CommandHandler("history", self.cmd_handler.cmd_history))
        app.add_handler(CommandHandler("hist", self.cmd_handler.cmd_history))
        
        # 설정/보정
        app.add_handler(CommandHandler("config", self.cmd_handler.cmd_config))
        app.add_handler(CommandHandler("fix", self.cmd_handler.cmd_fix))
        
        # 관리
        app.add_handler(CommandHandler("calceod", self.cmd_handler.cmd_force_calc))
        
        # 기존 호환
        app.add_handler(CommandHandler("sync", self.cmd_handler.cmd_status))  # 동기화→관제탑
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
    # 메시지 라우팅 (기존 [5] handle_message 확장)
    # ============================================================
    
    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """한글 자연어 명령 라우팅"""
        if not update.effective_message or not update.effective_message.text:
            return
        
        text = update.effective_message.text.strip()
        
        # ===== 무한매수법 핵심 =====
        if "상태" in text or "현황" in text or "관제탑" in text:
            return await self.cmd_handler.cmd_status(update, context)
        elif "주문내역" in text or "예약주문" in text:
            return await self.cmd_handler.cmd_orders(update, context)
        elif "히스토리" in text or "거래내역" in text:
            return await self.cmd_handler.cmd_history(update, context)
        
        # 설정/보정
        elif "수동" in text or "보정" in text or "fix" in text.lower():
            return await self.cmd_handler.cmd_fix(update, context)
        elif "설정" in text or "분할" in text or "원금" in text or "수수료" in text:
            return await self.cmd_handler.cmd_config(update, context)
        elif "동기화" in text or "싱크" in text or "sync" in text.lower():
            return await self.cmd_handler.cmd_status(update, context)
        
        # 관리
        elif "계산" in text or "강제계산" in text:
            return await self.cmd_handler.cmd_force_calc(update, context)
        elif "도움" in text or "help" in text.lower():
            return await self._cmd_help(update, context)
        
        # 기존 호환 (기존 [5])
        elif "통합 지시서" in text or "지시서 조회" in text:
            return await self.cmd_handler.cmd_status(update, context)
        elif "장부 동기화" in text or "장부 조회" in text:
            return await self.cmd_handler.cmd_history(update, context)
        
        else:
            # 알 수 없는 메시지
            await update.effective_message.reply_text(
                "알 수 없는 명령입니다. /help 로 명령어를 확인하세요."
            )
    
    # ============================================================
    # 콜백 처리
    # ============================================================
    
    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """InlineKeyboardButton 콜백"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        # 동기화
        if data == "SYNC:NOW":
            await self.cmd_handler.cmd_status(update, context)
        elif data.startswith("SYNC"):
            await self.cmd_handler.cmd_status(update, context)
        
        # 주문
        elif data.startswith("ORDERS:"):
            await self.cmd_handler.cmd_orders(update, context)
        
        # 설정
        elif data.startswith("CONFIG:"):
            await self.cmd_handler.cmd_config(update, context)
        
        # 히스토리
        elif data.startswith("HIST:"):
            await self.cmd_handler.cmd_history(update, context)
        
        # 강제계산
        elif data == "CALC:FORCE":
            await self.cmd_handler.cmd_force_calc(update, context)
        
        # 취소
        elif data == "RESET:CANCEL":
            await query.edit_message_text("❌ 취소됨")
    
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
            "'상태', '현황', '관제탑', '주문내역', \n"
            "'히스토리', '수동', '보정', '설정', '계산'"
        )
        await update.effective_message.reply_text(msg, parse_mode='HTML')
    
    def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """에러 핸들러"""
        logger.error(f"텔레그램 에러: {context.error}")
    
    # ============================================================
    # 실행
    # ============================================================
    
    async def run(self):
        """봇 시작 (블로킹)"""
        logger.info("텔레그램 봇 polling 시작...")
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()
        
        # 무한 대기
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await self.application.stop()
