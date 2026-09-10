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

        # ------------------- 설정 마법사 핸들러 (Step 1 ~ 5) -------------------
        # Step 1: 수수료율
        @self.dp.message(SetupWizard.FEE)
        async def process_fee(message: types.Message, state: FSMContext):
            if message.chat.id not in self.allowed_chats:
                return

            text = message.text.strip()
            if text == "❌ 취소":
                await state.clear()
                await message.answer(
                    "설정이 취소되었습니다.", reply_markup=ReplyKeyboardRemove()
                )
                return

            try:
                fee = float(text)
                await state.update_data(fee=fee)
                await state.set_state(SetupWizard.COMPOUND)
                await message.answer(
                    f"✅ 수수료율({fee})이 설정되었습니다.\n\nStep 2/5: 복리 설정 (예: 1.0)",
                    reply_markup=ReplyKeyboardRemove(),
                )
            except ValueError:
                await message.answer(
                    "올바른 숫자 형식으로 입력해 주세요. (예: 0.015)"
                )

        # Step 2: 복리 설정
        @self.dp.message(SetupWizard.COMPOUND)
        async def process_compound(message: types.Message, state: FSMContext):
            if message.chat.id not in self.allowed_chats:
                return
            try:
                compound = float(message.text.strip())
                await state.update_data(compound=compound)
                await state.set_state(SetupWizard.SEED)
                await message.answer(
                    f"✅ 복리({compound}) 설정 완료.\n\nStep 3/5: 시드머니 ($ 단위, 예: 20000)"
                )
            except ValueError:
                await message.answer(
                    "올바른 숫자 형식으로 입력해 주세요. (예: 1.0)"
                )

        # Step 3: 시드머니
        @self.dp.message(SetupWizard.SEED)
        async def process_seed(message: types.Message, state: FSMContext):
            if message.chat.id not in self.allowed_chats:
                return
            try:
                seed = float(message.text.strip())
                await state.update_data(seed=seed)
                await state.set_state(SetupWizard.SPLITS)
                await message.answer(
                    f"✅ 시드머니(${seed}) 설정 완료.\n\nStep 4/5: 분할 횟수 (예: 20)"
                )
            except ValueError:
                await message.answer(
                    "올바른 숫자 형식으로 입력해 주세요. (예: 20000)"
                )

        # Step 4: 분할 횟수
        @self.dp.message(SetupWizard.SPLITS)
        async def process_splits(message: types.Message, state: FSMContext):
            if message.chat.id not in self.allowed_chats:
                return
            try:
                splits = int(message.text.strip())
                await state.update_data(splits=splits)
                await state.set_state(SetupWizard.TICKER)
                await message.answer(
                    f"✅ 분할 횟수({splits}회) 설정 완료.\n\nStep 5/5: 매매 종목 티커 (예: TQQQ, SOXL)"
                )
            except ValueError:
                await message.answer("정수로 입력해 주세요. (예: 20)")

    # Step 5: 티커 입력 및 완료 (설정값 .env 자동 저장 추가)
    @self.dp.message(SetupWizard.TICKER)
    async def process_ticker(message: types.Message, state: FSMContext):
        if message.chat.id not in self.allowed_chats:
            return
        ticker = message.text.strip().upper()
        data = await state.get_data()
        await state.clear()

        fee = data.get('fee', 0.015)
        compound = data.get('compound', 1.0)
        seed = data.get('seed', 10000)
        splits = data.get('splits', 20)

        # .env 파일에 설정값 반영
        env_path = ".env"
        env_vars = {
            "V4_TICKER": str(ticker),
            "V4_SEED": str(seed),
            "V4_SPLITS": str(splits),
            "V4_COMPOUND": str(compound),
            "V4_FEE": str(fee),
        }

        # .env 파일 업데이트 로직
        lines = []
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        
        new_lines = []
        updated_keys = set()
        for line in lines:
            key = line.split("=")[0].strip() if "=" in line else ""
            if key in env_vars:
                new_lines.append(f"{key}={env_vars[key]}\n")
                updated_keys.add(key)
            else:
                new_lines.append(line)
        
        for key, val in env_vars.items():
            if key not in updated_keys:
                new_lines.append(f"{key}={val}\n")

        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

        summary = (
            f"🎉 설정 완료 및 .env 저장 성공!\n\n"
            f"• 티커: {ticker}\n"
            f"• 시드: ${seed}\n"
            f"• 분할: {splits}회\n"
            f"• 복리: {compound}\n"
            f"• 수수료: {fee}\n\n"
            f"💡 미국 정규장(22:30/23:30)에 키움 API를 통해 자동 주문이 진행됩니다."
        )
        await message.answer(summary)
        # ----------------------------------------------------------------------

        @self.dp.message(Command("checkkst"))
        async def cmd_check_kst(message: types.Message):
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
