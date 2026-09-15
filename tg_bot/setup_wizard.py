"""
================================================================
ConversationHandler 기반 초기 설정 마법사

기존 [5]의 /start, [4]의 cmd_seed 패턴을 확장
================================================================
"""

import html
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CommandHandler, ConversationHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

# 대화 상태
(
    SELECT_TICKER_MODE, SELECT_DIVISION, 
    INPUT_PRINCIPAL, SET_FEE, ADD_ANOTHER, 
    CONFIRM
) = range(6)


class SetupWizard:
    """
    초기 설정 마법사
    
    지원:
    - TQQQ 단독 / SOXL 단독 / 동시 운용
    - 수수료 직접 입력 (0.07% 등)
    """
    
    def __init__(self, state_manager):
        self.state = state_manager
    
    def get_handler(self) -> ConversationHandler:
        return ConversationHandler(
            entry_points=[CommandHandler('start', self.cmd_start)],
            states={
                SELECT_TICKER_MODE: [CallbackQueryHandler(self.on_ticker_mode)],
                SELECT_DIVISION: [CallbackQueryHandler(self.on_division)],
                INPUT_PRINCIPAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_principal)],
                SET_FEE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_fee)],
                ADD_ANOTHER: [CallbackQueryHandler(self.on_add_another)],
                CONFIRM: [CallbackQueryHandler(self.on_confirm)],
            },
            fallbacks=[
                CommandHandler('cancel', self.cmd_cancel),
                CommandHandler('start', self.cmd_start),  # 재시작
            ],
        )
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """최초 진입 또는 재설정"""
        user_id = str(update.effective_user.id)
        context.user_data['user_id'] = user_id
        context.user_data['tickers_config'] = []
        
        # 기존 설정 확인
        existing = self.state.get_user_tickers(user_id)
        if existing:
            msg = (
                f"🤖 <b>[kbot 무한매수법]</b>\n\n"
                f"이미 설정된 종목: <code>{', '.join(existing)}</code>\n\n"
                f"/config 로 설정 수정\n/status 로 관제탑 이동"
            )
            await update.message.reply_text(msg, parse_mode='HTML')
            return ConversationHandler.END
        
        # 신규
        keyboard = [
            [InlineKeyboardButton("🇺🇸 TQQQ 단독", callback_data="SINGLE:TQQQ")],
            [InlineKeyboardButton("🇺🇸 SOXL 단독", callback_data="SINGLE:SOXL")],
            [InlineKeyboardButton("💎 TQQQ + SOXL 동시", callback_data="BOTH")],
        ]
        
        await update.message.reply_text(
            "🤖 <b>[kbot 무한매수법]</b>\n\n"
            "처음 오셨군요! 사이클 설정을 도와드립니다.\n\n"
            "📌 <b>운용 방식 선택</b>\n"
            "어떤 방식으로 운용할까요?\n\n"
            "<i>단독: 한 종목 집중\n"
            "동시: 두 종목 병행 (원금 독립)</i>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        return SELECT_TICKER_MODE
    
    # ============================================================
    # 단계 1: 종목 선택
    # ============================================================
    
    async def on_ticker_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        if data.startswith("SINGLE:"):
            ticker = data.split(":")[1]
            context.user_data['mode'] = 'single'
            context.user_data['current_ticker'] = ticker
            
            await query.edit_message_text(
                f"📌 <b>{html.escape(ticker)} 단독 운용</b>\n\n"
                f"분할 수를 선택하세요:",
                reply_markup=self._division_keyboard(),
                parse_mode='HTML'
            )
            return SELECT_DIVISION
        
        elif data == "BOTH":
            context.user_data['mode'] = 'both'
            context.user_data['current_ticker'] = 'TQQQ'
            
            await query.edit_message_text(
                "📌 <b>TQQQ + SOXL 동시 운용</b>\n\n"
                "⚠️ 원금을 <b>독립적으로</b> 설정해야 합니다!\n\n"
                "첫 번째: <b>TQQQ</b>\n"
                "분할 수를 선택하세요:",
                reply_markup=self._division_keyboard(),
                parse_mode='HTML'
            )
            return SELECT_DIVISION
    
    # ============================================================
    # 단계 2: 분할 선택
    # ============================================================
    
    async def on_division(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        division = int(query.data)
        ticker = context.user_data['current_ticker']
        
        context.user_data['current_division'] = division
        
        await query.edit_message_text(
            f"💰 <b>{html.escape(ticker)}</b> 원금 입력\n\n"
            f"분할: <code>{division}</code>\n\n"
            f"사용할 원금(달러)을 입력하세요.\n"
            f"예: <code>20000</code>, <code>50000</code>\n\n"
            f"<i>⚠️ 잔금은 다른 투자에 사용하지 마세요!</i>",
            parse_mode='HTML'
        )
        return INPUT_PRINCIPAL
    
    # ============================================================
    # 단계 3: 원금 입력
    # ============================================================
    
    async def on_principal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip().replace(',', '').replace('$', '')
        
        try:
            principal = float(text)
            if principal < 1000:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ 최소 $1,000. 다시 입력해주세요:"
            )
            return INPUT_PRINCIPAL
        
        context.user_data['current_principal'] = principal
        
        await update.message.reply_text(
            "💰 <b>수수료 설정</b>\n\n"
            "<b>계좌 수수료율을 직접 입력하세요</b>\n\n"
            "예시:\n"
            "├ <code>0.25</code> → 0.25% (키움 기본)\n"
            "├ <code>0.07</code> → 0.07%\n"
            "├ <code>0.015</code> → 0.015% (VIP)\n"
            "└ <code>0</code> → 무료\n\n"
            "<i>실제 계좌 수수료율을 입력하세요</i>",
            parse_mode='HTML'
        )
        return SET_FEE
    
    # ============================================================
    # 단계 4: 수수료 입력
    # ============================================================
    
    async def on_fee(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip().replace('%', '')
        
        try:
            fee_input = float(text)
            if fee_input < 0 or fee_input > 1:
                raise ValueError
            
            # 변환: 0.25 → 0.0025, 0.07 → 0.0007
            fee_rate = fee_input / 100 if fee_input > 0.01 else fee_input
            
        except ValueError:
            await update.message.reply_text(
                "❌ 0~1 범위. 예: <code>0.07</code>, <code>0.25</code>"
            )
            return SET_FEE
        
        # 현재 종목 설정 완성
        current = {
            'ticker': context.user_data['current_ticker'],
            'division': context.user_data['current_division'],
            'principal': context.user_data['current_principal'],
            'fee_rate': fee_rate,
            'fee_display': fee_input,
        }
        context.user_data['tickers_config'].append(current)
        
        # 동시 운용 두 번째 종목 체크
        mode = context.user_data['mode']
        
        if mode == 'both' and context.user_data['current_ticker'] == 'TQQQ':
            # SOXL 설정 필요
            context.user_data['current_ticker'] = 'SOXL'
            
            keyboard = [
                [InlineKeyboardButton("동일 설정 유지", callback_data="KEEP_SAME")],
                [InlineKeyboardButton("새로 입력", callback_data="NEW_INPUT")],
            ]
            
            await update.message.reply_text(
                "✅ <b>TQQQ 설정 완료</b>\n\n"
                "<b>SOXL</b> 설정합니다.\n"
                "TQQQ와 동일하게 설정할까요?\n"
                f"(분할 {current['division']}, 원금 ${current['principal']:,.0f}, "
                f"수수료 {current['fee_display']}%)",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
            return ADD_ANOTHER
        
        # 단독 또는 동시 두 번째 완료
        return await self._show_final_confirm(update, context)
    
    # ============================================================
    # 추가: 동시 두 번째 종목
    # ============================================================
    
    async def on_add_another(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        if query.data == "KEEP_SAME":
            # TQQQ 설정 복사, 원금만 확인
            tqqq_cfg = context.user_data['tickers_config'][0]
            
            soxl_cfg = {
                'ticker': 'SOXL',
                'division': tqqq_cfg['division'],
                'principal': tqqq_cfg['principal'],
                'fee_rate': tqqq_cfg['fee_rate'],
                'fee_display': tqqq_cfg['fee_display'],
            }
            context.user_data['tickers_config'].append(soxl_cfg)
            
            return await self._show_final_confirm(update, context)
        
        else:  # NEW_INPUT
            await query.edit_message_text(
                "📌 <b>SOXL</b>\n분할 수를 선택하세요:",
                reply_markup=self._division_keyboard(),
                parse_mode='HTML'
            )
            return SELECT_DIVISION
    
    # ============================================================
    # 최종 확인
    # ============================================================
    
    async def _show_final_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        configs = context.user_data['tickers_config']
        
        summary = "📋 <b>[최종 설정 확인]</b>\n\n"
        total = 0
        
        for c in configs:
            one_buy = c['principal'] / c['division']
            summary += (
                f"💎 <b>{html.escape(c['ticker'])}</b>\n"
                f"├ 분할: <code>{c['division']}</code>\n"
                f"├ 원금: <code>${c['principal']:,.0f}</code>\n"
                f"├ 1회매수: <code>${one_buy:,.2f}</code>\n"
                f"├ 수수료: <code>{c['fee_display']}%</code>\n"
                f"└ MODE: <code>{'단독' if len(configs)==1 else '동시'}</code>\n\n"
            )
            total += c['principal']
        
        if len(configs) == 2:
            summary += f"💵 <b>총 투자원금: ${total:,.0f}</b>\n\n"
        
        summary += "<i>설정 완료 후 관제탑이 활성화됩니다.</i>"
        
        keyboard = [
            [InlineKeyboardButton("✅ 설정 완료", callback_data="CONFIRM")],
            [InlineKeyboardButton("🔄 처음부터", callback_data="RESTART")],
        ]
        
        await update.message.reply_text(
            summary,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        return CONFIRM
    
    async def on_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        if query.data == "RESTART":
            context.user_data.clear()
            return await self.cmd_start(update, context)
        
        # 저장
        user_id = context.user_data['user_id']
        configs = context.user_data['tickers_config']
        
        applied = []
        for c in configs:
            full_config = {
                "user_id": user_id,
                "ticker": c['ticker'],
                "division": c['division'],
                "principal": c['principal'],
                "seed": 0,
                "fee_rate": c['fee_rate'],
                "fee_display": c['fee_display'],
                "mode": "normal",
                "run_mode": "single" if len(configs) == 1 else "both",
                "is_active": True,
                "settings": {
                    "auto_order": True,
                    "notifications": True,
                    "sandbox": False,
                    "max_fallback_orders": 5,
                },
            }
            self.state.save_ticker_config(user_id, c['ticker'], full_config)

            # 설정을 실행 상태에 반영한다.
            # 저장만 하고 get_state() 를 부르면, 기존 상태 파일이 있을 때
            # 분할수·원금 변경이 조용히 무시된다.
            res = self.state.apply_config(c['ticker'])
            applied.append((c['ticker'], res))
        
        lines = ["✅ 설정 완료!"]
        for ticker, res in applied:
            for c in res.get('changed', []):
                lines.append(f"  [{ticker}] {c}")
            for b in res.get('blocked', []):
                lines.append(f"  ⚠️ [{ticker}] {b}")
            if res.get('error'):
                lines.append(f"  ⚠️ [{ticker}] {res['error']}")
        lines.append("")
        lines.append("관제탑을 불러옵니다...")
        await query.edit_message_text("\n".join(lines))
        
        # 관제탑으로 이동 (bot.py에서 처리)
        context.user_data['setup_complete'] = True
        return ConversationHandler.END
    
    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("❌ 설정 취소. /start 로 다시 시작하세요.")
        return ConversationHandler.END
    
    def _division_keyboard(self):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔥 20분할 (공격적)", callback_data="20"),
             InlineKeyboardButton("🛡️ 40분할 (방어적)", callback_data="40")],
        ])
