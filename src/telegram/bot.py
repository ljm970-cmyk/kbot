"""텔레그램 완전 통합 봇 - 설정 마법사"""

import asyncio
import logging
from decimal import Decimal
from typing import List

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, 
    KeyboardButton,
    InlineKeyboardMarkup, 
    InlineKeyboardButton,
    ReplyKeyboardRemove
)

logger = logging.getLogger("v4bot.telegram")


class SetupWizard(StatesGroup):
    FEE = State()
    COMPOUND = State()
    SEED = State()
    SPLITS = State()
    TICKER = State()
    CONFIRM = State()


class V4TelegramBot:
    def __init__(self, token: str, allowed_chat_ids: List[int]):
        self.token = token
        self.allowed_chats = set(allowed_chat_ids)
        self.bot = Bot(token=token)
        self.dp = Dispatcher(storage=MemoryStorage())
        self._register_handlers()
    
    def _register_handlers(self):
        @self.dp.message(Command("start"))
        async def cmd_start(message: types.Message):
            if message.chat.id not in self.allowed_chats:
                return
            await message.answer("🤖 무한매수법 V4.0\n/setup 으로 시작하세요.")
        
        @self.dp.message(Command("setup"))
        async def cmd_setup(message: types.Message, state: FSMContext):
            if message.chat.id not in self.allowed_chats:
                return
            await state.clear()
            await state.set_state(SetupWizard.FEE)
            await message.answer(
                "🚀 설정 마법사\nStep 1/5: 수수료율\n(0.015, 0.025 등)",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(text="0.015")],
                             [KeyboardButton(text="0.025")],
                             [KeyboardButton(text="❌ 취소")]],
                    resize_keyboard=True
                )
            )
        
        @self.dp.message(Command("checkkst"))
        async def cmd_check_kst(message: types.Message):
            from zoneinfo import ZoneInfo
            KST = ZoneInfo("Asia/Seoul")
            now = datetime.now(KST)
            await message.answer(
                f"🔍 KST: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                f"TZ env: {os.environ.get('TZ', '미설정')}"
            )
    
    async def start(self):
        await self.dp.start_polling(self.bot)
    
    async def send_alert(self, text: str):
        for chat_id in self.allowed_chats:
            try:
                await self.bot.send_message(chat_id=chat_id, text=text)
            except Exception as e:
                logger.error(f"알림 실패: {e}")


from datetime import datetime
import os