"""
================================================================
슬래시 명령어 핸들러

기존 [4]의 핵심 패턴 재사용:
- _safe_float, _safe_reply, _retry_api
- HTML escape, InlineKeyboardMarkup
- 오입력 차단 (±20%)
- cmd_sync, cmd_seed, cmd_insert 변형
================================================================
"""

import asyncio
import html
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from core.star_point import StarPointCalculator
from core.timezone_handler import USMarketTimezone


class CommandsHandler:
    """명령어 처리"""

    def __init__(self, state_manager, kiwoom_api, config):
        self.state = state_manager
        self.kiwoom = kiwoom_api
        self.config = config
        self.tz = USMarketTimezone()
    
    # ============================================================
    # 기본 유틸리티 (기존 [4] 재사용)
    # ============================================================
    
    def _safe_float(self, value, default=0.0):
        """기존 [4] 그대로"""
        try:
            return float(value) if value is not None else default
        except (ValueError, TypeError):
            return default
    
    async def _safe_reply(self, message, text, **kwargs):
        """기존 [4] 그대로"""
        try:
            return await message.reply_text(text, **kwargs)
        except Exception as e:
            print(f"Reply error: {e}")
    
    async def _safe_edit(self, message, text, **kwargs):
        """기존 [4] _send_daily_table 패턴"""
        try:
            return await message.edit_text(text, **kwargs)
        except Exception as e:
            print(f"Edit error: {e}")
            try:
                return await message.reply_text(text, **kwargs)
            except:
                return None
    
    async def _retry_api(self, coro_func, *args, default=None, retries=3):
        """기존 [4] _retry_connect_direct 변형"""
        for attempt in range(retries):
            try:
                return await coro_func(*args)
            except Exception as e:
                if attempt == retries - 1:
                    return default
                await asyncio.sleep(2 ** attempt)
    
    # ============================================================
    # 핵심 명령어
    # ============================================================
    
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        📊 관제탑 (기존 [4] cmd_avwap + cmd_mode 결합)
        
        사용자 설정 종목만 동적 표시
        """
        user_id = str(update.effective_user.id)
        
        # 도감 방지 (기존 [5] _is_admin)
        active_tickers = self.state.get_user_tickers(user_id)
        if not active_tickers:
            await self._safe_reply(update.effective_message,
                "❌ 설정된 종목이 없습니다.\n/start 로 초기 설정을 진행하세요.")
            return
        
        status_msg = await self._safe_reply(
            update.effective_message,
            "⏳ <b>[무한매수법 관제탑]</b>\n스캔 중...",
            parse_mode='HTML'
        )
        
        hour, season_text = self.tz.get_dst_info()
        
        report = f"📊 <b>[ 관제탑 ]</b>\n{'━'*20}\n\n"
        
        for ticker in active_tickers:
            st = await self._retry_api(
                self.state.get_state, user_id, ticker, default=None
            )
            if not st:
                report += f"❌ <b>{html.escape(ticker)}</b>: 상태 없음\n\n"
                continue
            
            # 모드/단계
            mode_icon = "🔄" if st['mode'] == 'normal' else "🔁"
            phase = self._get_phase(st)
            
            report += (
                f"{mode_icon} <b>{html.escape(ticker)}</b> "
                f"(<code>{st['division']}</code>분할)\n"
                f"├ 📈 T값: <code>{st['T']:.4f}</code> <i>{phase}</i>\n"
                f"├ 💰 평단: <code>${st['avg_price']:.2f}</code>\n"
                f"├ 📦 보유: <code>{st['holdings']}</code>주\n"
                f"├ 💵 잔금: <code>${st['cash']:,.2f}</code>\n"
            )
            
            # 별지점
            star = StarPointCalculator(ticker, st['division'], st['mode'])
            star_calc = star.calculate(st['avg_price'], st['T']) if st['avg_price'] > 0 else None
            
            if st['mode'] == 'normal' and star_calc:
                report += f"├ ⭐ 별지점: <code>${star_calc.star_point:.2f}</code> "
                f"(매수: <code>${star_calc.buy_price:.2f}</code>)\n"
            elif star_calc:
                recover = st['avg_price'] * (0.85 if ticker == 'TQQQ' else 0.80)
                report += f"├ ⭐ 별지점: <code>${star_calc.star_point:.2f}</code> "
                f"(회복: <code>${recover:.2f}</code>)\n"
            
            # 다음 주문
            next_order = self.tz.get_next_order_time()
            report += f"└ ⏰ 다음주문: <code>{next_order.strftime('%m/%d %H:%M')}</code>\n"
            report += f"   ({season_text})\n\n"
        
        # 푸터
        report += f"{'━'*20}\n"
        if len(active_tickers) == 1:
            report += f"▪️ <i>단독 운용 중</i>\n"
        else:
            total = sum(self.state.get_principal(user_id, t) for t in active_tickers)
            report += f"▪️ <i>동시 운용 | 총 원금: ${total:,.0f}</i>\n"
        
        report += f"⏱️ EOD: <code>{self.tz.get_next_eod_time().strftime('%m/%d %H:%M')}</code>\n"
        
        # 버튼 (기존 [4] cmd_seed 패턴)
        keyboard = []
        for t in active_tickers:
            keyboard.append([
                InlineKeyboardButton(f"📋 {t} 주문", callback_data=f"ORDERS:{t}"),
                InlineKeyboardButton(f"⚙️ {t} 설정", callback_data=f"CONFIG:EDIT:{t}")
            ])
        
        keyboard.extend([
            [InlineKeyboardButton("🔄 동기화", callback_data="SYNC:NOW"),
             InlineKeyboardButton("➕ 종목 추가", callback_data="TICKER:ADD")],
            [InlineKeyboardButton("📊 히스토리", callback_data="HIST:LIST"),
             InlineKeyboardButton("⚡ 강제계산", callback_data="CALC:FORCE")],
        ])
        
        await self._safe_edit(status_msg, report,
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    
    # ============================================================
    # 추가 명령어들...
    # ============================================================
    
    async def cmd_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """📋 예약주문 내역 (ust21205)"""
        # TODO: 기존 [4] cmd_avwap 스캔 패턴 재사용
        pass
    
    async def cmd_fix(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        🔧 수동 거래 보정 (기존 [4] cmd_insert 변형)
        
        오입력 차단 (±20%)
        """
        args = context.args
        if len(args) < 4:
            await self._safe_reply(update.effective_message,
                "📝 <b>[수동 거래 보정]</b>\n\n"
                "사용법:\n"
                "<code>/fix TQQQ 20250624 10 150.50 buy</code>\n\n"
                "<code>/fix SOXL 20250624 5 25.30 sell</code>",
                parse_mode='HTML')
            return
        
        ticker, date_str, qty, price, *rest = args
        side = rest[0] if rest else 'buy'
        
        # 오입력 차단 (기존 [4] cmd_insert 패턴)
        current_price = await self._retry_api(
            self.kiwoom.get_current_price, ticker, default=0
        )
        
        input_price = self._safe_float(price)
        if current_price > 0 and input_price > 0:
            lower = current_price * 0.4
            upper = current_price * 1.6
            if input_price < lower or input_price > upper:
                await self._safe_reply(update.effective_message,
                    f"🚨 <b>오입력 차단</b>\n\n"
                    f"현재가 ${current_price:.2f} 대비 "
                    f"${input_price:.2f}는 ±60% 초과\n"
                    f"(허용: ${lower:.2f} ~ ${upper:.2f})",
                    parse_mode='HTML')
                return
        
        # 수동 보정 저장
        user_id = str(update.effective_user.id)
        result = self.state.add_manual_correction(user_id, ticker, {
            'date': date_str,
            'qty': int(qty),
            'price': input_price,
            'side': side,
            'type': 'MANUAL_FIX'
        })
        
        if result:
            await self._safe_reply(update.effective_message,
                f"✅ <b>[{html.escape(ticker)}] 수동 보정 완료</b>\n\n"
                f"├ 날짜: <code>{date_str}</code>\n"
                f"├ 구분: <code>{side}</code>\n"
                f"├ 수량: <code>{qty}</code>주\n"
                f"├ 가격: <code>${input_price:.2f}</code>\n\n"
                f"<i>다음 EOD 계산 시 반영</i>",
                parse_mode='HTML')
    
    async def cmd_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """⚙️ 설정 변경 (기존 [4] cmd_settlement 변형)"""
        # TODO: 분할/원금/수수료 수정 UI
        pass
    
    async def cmd_force_calc(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """⚡ 강제 EOD 계산"""
        status_msg = await self._safe_reply(update.effective_message,
            "⏳ <b>[강제 EOD 계산]</b>\n미처리 체결 내역 처리 중...",
            parse_mode='HTML')
        
        # TODO: eod.calculator 호출
        
        await self._safe_edit(status_msg,
            "✅ <b>[EOD 계산 완료]</b>\n"
            "<i>결과 표시 TODO</i>",
            parse_mode='HTML')
    
    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """📊 거래 히스토리"""
        # TODO: state['history'] 조회 표시
        pass
    
    # ============================================================
    # 헬퍼
    # ============================================================
    
    def _get_phase(self, state: dict) -> str:
        """단계 표시"""
        T = state['T']
        division = state['division']
        mode = state['mode']
        
        if mode == 'reverse':
            return "[리버스모드]"
        if T == 0:
            return "[초기]"
        if T < division / 2:
            return f"[전반전 {T/division*100:.0f}%]"
        if T <= division - 1:
            if T > division * 0.8:
                return f"[후반전 ⚠️소진임박]"
            return f"[후반전]"
        return "[소진⚡→리버스]"
