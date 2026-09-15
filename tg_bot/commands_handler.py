"""
================================================================
슬래시 명령어 핸들러 (COMPLETE)

기존 [4] 패턴 + 신규 구현:
- cmd_orders: ust21205 예약주문 조회
- cmd_config: 설정 변경 (분할/원금/수수료)
- cmd_history: 거래 히스토리
================================================================
"""

import asyncio
import html
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from core.star_point import StarPointCalculator
from core.timezone_handler import USMarketTimezone


class CommandsHandler:
    """명령어 처리 (COMPLETE)"""

    def __init__(self, state_manager, kiwoom_api, config):
        self.state = state_manager
        self.kiwoom = kiwoom_api
        self.config = config
        self.tz = USMarketTimezone()
    
    # ============================================================
    # 기본 유틸리티 (기존 [4] 그대로)
    # ============================================================
    
    def _safe_float(self, value, default=0.0):
        try:
            return float(value) if value is not None else default
        except (ValueError, TypeError):
            return default
    
    async def _safe_reply(self, message, text, **kwargs):
        try:
            return await message.reply_text(text, **kwargs)
        except Exception as e:
            print(f"Reply error: {e}")
    
    async def _safe_edit(self, message, text, **kwargs):
        try:
            return await message.edit_text(text, **kwargs)
        except Exception:
            try:
                return await message.reply_text(text, **kwargs)
            except:
                return None
    
    async def _retry_api(self, coro_func, *args, default=None, retries=3):
        for attempt in range(retries):
            try:
                return await coro_func(*args)
            except Exception as e:
                if attempt == retries - 1:
                    return default
                await asyncio.sleep(2 ** attempt)
    
    async def _get_user_id(self, update: Update) -> str:
        """사용자 ID 추출"""
        return str(update.effective_user.id)
    
    # ============================================================
    # cmd_status (이전 완성)
    # ============================================================
    
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """📊 관제탑"""
        user_id = await self._get_user_id(update)
        
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
            
            mode_icon = "🔄" if st['mode'] == 'normal' else "🔁"
            phase = self._get_phase(st)
            
            report += (
                f"{mode_icon} <b>{html.escape(ticker)}</b> "
                f"(<code>{st['division']}</code>분할)\n"
                f"├ 📈 T값: <code>{st['T']:.4f}</code> <i>{phase}</i>\n"
                f"├ 💰 평단: <code>${st['avg_price']:.2f}</code>\n"
                f"├ 📦 보유: <code>{st['holdings']}</code>주\n"
                f"├ 💵 잔금: <code>{st['cash']:,.2f}</code>\n"
            )
            
            # 별지점
            if st['avg_price'] > 0:
                star = StarPointCalculator(ticker, st['division'], st['mode'])
                ma5 = st.get('ma5', 0)

                if st['mode'] == 'normal':
                    star_calc = star.calculate(st['avg_price'], st['T'])
                    report += (
                        f"├ ⭐ 별지점: <code>${star_calc.star_point:.2f}</code> "
                        f"(매수: <code>${star_calc.buy_price:.2f}</code>)\n"
                    )
                elif ma5 > 0:
                    star_calc = star.calculate(st['avg_price'], st['T'], ma5=ma5)
                    star_pct = StarPointCalculator.STAR_PCT_REVERSE[ticker]
                    recover = st['avg_price'] * (1 + star_pct / 100)
                    report += (
                        f"├ ⭐ 별지점(MA5): <code>${star_calc.star_point:.2f}</code> "
                        f"(회복: <code>${recover:.2f}</code>)\n"
                    )
                else:
                    report += "├ ⭐ 별지점: <i>MA5 대기중</i>\n"
            
            report += f"└ ⏰ 다음주문: {self.tz.get_next_order_time().strftime('%m/%d %H:%M')}\n"
            report += f"   ({season_text})\n\n"
        
        report += f"{'━'*20}\n"
        if len(active_tickers) == 1:
            report += f"▪️ <i>단독 운용 중</i>\n"
        else:
            total = sum(self.state.get_principal(user_id, t) for t in active_tickers)
            report += f"▪️ <i>동시 운용 | 총 원금: ${total:,.0f}</i>\n"
        
        report += f"⏱️ EOD: <code>{self.tz.get_next_eod_time().strftime('%m/%d %H:%M')}</code>\n"
        
        # 버튼
        keyboard = []
        for t in active_tickers:
            keyboard.append([
                InlineKeyboardButton(f"📋 {t} 주문", callback_data=f"ORDERS:{t}"),
                InlineKeyboardButton(f"⚙️ {t} 설정", callback_data=f"CONFIG:EDIT:{t}")
            ])
        
        keyboard.extend([
            [InlineKeyboardButton("🔄 동기화", callback_data="SYNC:NOW"),
             InlineKeyboardButton("⚡ 강제계산", callback_data="CALC:FORCE")],
            [InlineKeyboardButton("📊 히스토리", callback_data="HIST:LIST"),
             InlineKeyboardButton("➕ 종목 추가", callback_data="TICKER:ADD")],
        ])
        
        await self._safe_edit(status_msg, report,
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    
    # ============================================================
    # TODO 완성: cmd_orders
    # ============================================================
    
    async def cmd_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        📋 예약주문 내역 조회 (ust21205)
        
        [1] 키움 REST API:
            POST /api/us/acnt
            api-id: ust21205 (미국주식 예약주문내역조회)
        """
        user_id = await self._get_user_id(update)
        active_tickers = self.state.get_user_tickers(user_id)
        
        if not active_tickers:
            await self._safe_reply(update.effective_message,
                "❌ 설정된 종목이 없습니다.")
            return
        
        # 특정 종목 조회 또는 전체
        target_ticker = None
        if context.args:
            target_ticker = context.args[0].upper()
            if target_ticker not in active_tickers:
                await self._safe_reply(update.effective_message,
                    f"❌ 미설정 종목: {html.escape(target_ticker)}")
                return
        
        status_msg = await self._safe_reply(
            update.effective_message,
            "⏳ <b>[예약주문 내역]</b>\n키움 서버 조회 중...",
            parse_mode='HTML'
        )
        
        # 조회 기간: 오늘
        today = datetime.now().strftime('%Y%m%d')
        
        report = f"📋 <b>[ 예약주문 내역 ]</b>\n{'━'*20}\n\n"
        
        tickers_to_check = [target_ticker] if target_ticker else active_tickers
        
        for ticker in tickers_to_check:
            # ust21205 호출 [1]
            orders = await self._retry_api(
                self.kiwoom.get_reserv_orders,
                fr_rsrv_dt=today,
                to_rsrv_dt=today,
                ticker=ticker,
                default=[]
            )
            
            if not orders:
                report += f"{html.escape(ticker)}: <i>예약주문 없음</i>\n\n"
                continue
            
            report += f"<b>{html.escape(ticker)}</b> ({len(orders)}건)\n"
            
            for i, o in enumerate(orders, 1):
                # 주문 상태
                ord_stat = o.get('ord_stat', '01')
                status_icon = "🟢" if ord_stat == '01' else "🟡" if ord_stat == '02' else "🔴"
                
                # 주문 유형
                gubun = o.get('ord_gubun', '30')
                gubun_map = {
                    '30': ('LOC', '🔵'),
                    '32': ('MOC', '🟠'),
                    '00': ('지정가', '🟣'),
                }
                type_name, type_icon = gubun_map.get(gubun, (gubun, '⚪'))
                
                # 매수/매도
                slby = "매수" if o.get('slby_gubun') == '2' else "매도"
                
                # 가격
                price = o.get('ord_uv', '시장가')
                
                report += (
                    f"├ {status_icon} <code>{o.get('rsrv_ord_no', 'N/A')}</code> | "
                    f"{type_icon}{type_name}{slby[-1]} | "
                    f"{price} | {o.get('ord_qty', '0')}주 | "
                    f"{'실행대기' if ord_stat == '01' else '처리중'}\n"
                )
            
            report += "\n"
        
        # 버튼
        keyboard = [
            [InlineKeyboardButton("❌ 주문취소", callback_data="ORDER:CANCEL"),
             InlineKeyboardButton("🗑️ 일괄취소", callback_data="ORDER:CANCEL_ALL")],
            [InlineKeyboardButton("🔄 새로고침", callback_data="ORDERS:REFRESH")],
        ]
        
        await self._safe_edit(status_msg, report,
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    
    # ============================================================
    # TODO 완성: cmd_config
    # ============================================================
    
    async def cmd_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        ⚙️ 설정 변경
        
        사용법:
            /config              → 설정 메뉴
            /config TQQQ division 20    → TQQQ 분할 변경
            /config SOXL principal 30000 → SOXL 원금 변경
            /config TQQQ fee 0.05        → TQQQ 수수료 변경
        """
        user_id = await self._get_user_id(update)
        active_tickers = self.state.get_user_tickers(user_id)
        
        if not active_tickers:
            await self._safe_reply(update.effective_message,
                "❌ 설정된 종목이 없습니다.\n/start 로 설정하세요.")
            return
        
        args = context.args
        
        # 직접 수정: /config TQQQ division 20
        if len(args) >= 3:
            return await self._config_direct_update(update, user_id, args)
        
        # 설정 메뉴 표시
        return await self._config_menu(update, user_id, active_tickers)
    
    async def _config_menu(self, update: Update, user_id: str, tickers: list):
        """설정 메뉴 버튼 표시"""
        
        keyboard = []
        msg = "⚙️ <b>[ 무한매수법 설정 ]</b>\n\n"
        
        for ticker in tickers:
            cfg = self.state.get_ticker_config(user_id, ticker)
            if not cfg:
                continue
            
            division = cfg.get('division', 40)
            principal = cfg.get('principal', 0)
            fee_display = cfg.get('fee_display', 0.25)
            auto = "ON" if cfg.get('settings', {}).get('auto_order', True) else "OFF"
            
            msg += (
                f"💎 <b>{html.escape(ticker)}</b>\n"
                f"├ 분할: <code>{division}</code>\n"
                f"├ 원금: <code>${principal:,.0f}</code>\n"
                f"├ 수수료: <code>{fee_display}%</code>\n"
                f"└ 자동주문: <code>{auto}</code>\n\n"
            )
            
            # 종목별 버튼
            keyboard.append([
                InlineKeyboardButton(f"🔢 {ticker} 분할", callback_data=f"CONFIG:DIV:{ticker}"),
                InlineKeyboardButton(f"💵 {ticker} 원금", callback_data=f"CONFIG:SEED:{ticker}"),
                InlineKeyboardButton(f"💰 {ticker} 수수료", callback_data=f"CONFIG:FEE:{ticker}")
            ])
        
        # 전역 설정
        msg += f"🌍 <b>전역</b>\n{self.tz.get_dst_info()[1]}\n"
        
        keyboard.extend([
            [InlineKeyboardButton("⏰ 자동주문 토글", callback_data="CONFIG:AUTO"),
             InlineKeyboardButton("🔔 알림 토글", callback_data="CONFIG:NOTIFY")],
            [InlineKeyboardButton("🧪 샌드박스 ON", callback_data="CONFIG:SANDBOX:ON"),
             InlineKeyboardButton("🚀 샌드박스 OFF", callback_data="CONFIG:SANDBOX:OFF")],
        ])
        
        await self._safe_reply(update.effective_message, msg,
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    
    async def _config_direct_update(self, update: Update, user_id: str, args: list):
        """직접 설정 변경: /config TQQQ division 20"""
        
        ticker, key, value = args[0].upper(), args[1].lower(), args[2]
        
        cfg = self.state.get_ticker_config(user_id, ticker)
        if not cfg:
            await self._safe_reply(update.effective_message,
                f"❌ {html.escape(ticker)} 설정 없음")
            return
        
        # 키 매핑
        key_map = {
            'division': ('division', int, '분할'),
            'div': ('division', int, '분할'),
            'principal': ('principal', float, '원금'),
            '원금': ('principal', float, '원금'),
            'seed': ('principal', float, '원금'),  # 호환
            'fee': ('fee_rate', self._parse_fee, '수수료'),
            '수수료': ('fee_rate', self._parse_fee, '수수료'),
            'mode': ('mode', str, '모드'),
        }
        
        mapped = key_map.get(key)
        if not mapped:
            await self._safe_reply(update.effective_message,
                f"❌ 알 수 없는 설정: {key}\n"
                f"가능: division, principal, fee, mode")
            return
        
        real_key, converter, display_name = mapped
        
        try:
            converted = converter(value)
        except ValueError as e:
            await self._safe_reply(update.effective_message,
                f"❌ 값 오류: {value}\n{str(e)}")
            return
        
        old = cfg.get(real_key, 'N/A')
        cfg[real_key] = converted
        
        # 수수료는 display도 업데이트
        if real_key == 'fee_rate':
            cfg['fee_display'] = converted * 100 if converted < 0.01 else converted
        
        # 원금 변경 시 cash 재조정 (T=0일 때만)
        if real_key == 'principal' and cfg.get('T', 0) == 0:
            cfg['cash'] = converted
        
        self.state.save_ticker_config(user_id, ticker, cfg)
        
        await self._safe_reply(update.effective_message,
            f"✅ <b>{html.escape(ticker)}</b> 설정 변경:\n"
            f"<code>{display_name}</code>: {old} → <b>{converted}</b>",
            parse_mode='HTML')
    
    def _parse_fee(self, value: str) -> float:
        """수수료 파서: 0.07 → 0.0007"""
        num = float(value)
        if num < 0 or num > 1:
            raise ValueError("0~1 범위")
        # 0.25(%) vs 0.0025(소수) 구분
        return num / 100 if num > 0.01 else num
    
    # ============================================================
    # TODO 완성: cmd_history
    # ============================================================
    
    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        📊 거래 히스토리 조회
        
        기존 [4] cmd_history 패턴 활용
        """
        user_id = await self._get_user_id(update)
        active_tickers = self.state.get_user_tickers(user_id)
        
        if not active_tickers:
            await self._safe_reply(update.effective_message,
                "❌ 설정된 종목이 없습니다.")
            return
        
        # 특정 종목 또는 전체
        target_ticker = None
        if context.args:
            target_ticker = context.args[0].upper()
            if target_ticker not in active_tickers:
                await self._safe_reply(update.effective_message,
                    f"❌ 미설정 종목: {html.escape(target_ticker)}")
                return
        
        status_msg = await self._safe_reply(
            update.effective_message,
            "⏳ <b>[거래 히스토리]</b>\n조회 중...",
            parse_mode='HTML'
        )
        
        tickers_to_check = [target_ticker] if target_ticker else active_tickers
        
        report = f"📊 <b>[ 거래 히스토리 ]</b>\n{'━'*20}\n\n"
        
        for ticker in tickers_to_check:
            st = self.state.get_state(user_id, ticker)
            history = st.get('history', [])
            
            report += f"<b>{html.escape(ticker)}</b> ({len(history)}건)\n"
            
            if not history:
                report += "├ <i>거래 내역 없음</i>\n\n"
                continue
            
            # 최근 10건
            recent = history[-10:]
            for h in recent:
                icon = "🟢" if h.get('action') == 'buy' else "🔴"
                report += (
                    f"├ {icon} <code>{h.get('date', 'N/A')}</code> | "
                    f"{h.get('action', 'N/A')} {h.get('qty', 0)}주 | "
                    f"${h.get('price', 0):.2f} | "
                    f"T:{h.get('T_before', 0):.2f}→{h.get('T_after', 0):.2f}\n"
                )
            
            report += "\n"
        
        # EOD 아카이브 링크 (선택)
        report += f"{'━'*20}\n"
        report += "<i>전체 내역: data/state/EOD_YYYYMMDD.jsonl</i>"
        
        keyboard = [
            [InlineKeyboardButton("📋 주문내역", callback_data="ORDERS:VIEW"),
             InlineKeyboardButton("🔄 관제탑", callback_data="SYNC:NOW")],
        ]
        
        await self._safe_edit(status_msg, report,
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    
    # ============================================================
    # 이전 완성된 명령어들
    # ============================================================
    
    async def cmd_fix(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """🔧 수동 보정 (기존 완성)"""
        args = context.args
        if len(args) < 4:
            await self._safe_reply(update.effective_message,
                "📝 <b>[수동 거래 보정]</b>\n\n"
                "사용법:\n"
                "<code>/fix TQQQ 20250624 10 150.50 buy</code>\n"
                "<code>/fix SOXL 20250624 5 25.30 sell</code>",
                parse_mode='HTML')
            return
        
        ticker, date_str, qty, price, *rest = args
        side = rest[0] if rest else 'buy'
        user_id = await self._get_user_id(update)
        
        # 오입력 차단 (±60%)
        last_close = await self._retry_api(
            self.kiwoom.get_last_close, ticker, default=0
        )
        input_price = self._safe_float(price)

        if last_close > 0 and input_price > 0:
            lower, upper = last_close * 0.4, last_close * 1.6
            if input_price < lower or input_price > upper:
                await self._safe_reply(update.effective_message,
                    f"🚨 <b>오입력 차단</b>\n"
                    f"직전 종가 ${last_close:.2f} 대비 ${input_price:.2f}는 ±60% 초과",
                    parse_mode='HTML')
                return
        
        result = self.state.add_manual_correction(user_id, ticker, {
            'date': date_str,
            'qty': int(qty),
            'price': input_price,
            'side': side,
            'type': 'MANUAL_FIX'
        })
        
        if result:
            await self._safe_reply(update.effective_message,
                f"✅ <b>[{html.escape(ticker)}] 수동 보정 완료</b>\n"
                f"├ 날짜: <code>{date_str}</code>\n"
                f"├ 구분: <code>{side}</code>\n"
                f"├ 수량: <code>{qty}</code>주\n"
                f"├ 가격: <code>${input_price:.2f}</code>\n\n"
                f"<i>다음 EOD 계산 시 반영</i>",
                parse_mode='HTML')
    
    async def cmd_force_calc(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """⚡ 강제 EOD 계산"""
        status_msg = await self._safe_reply(update.effective_message,
            "⏳ <b>[강제 EOD 계산]</b>\n미처리 체결 내역 처리 중...",
            parse_mode='HTML')
        
        # TODO: eod.calculator 호출 (user_id 전달)
        user_id = await self._get_user_id(update)
        
        await self._safe_edit(status_msg,
            "✅ <b>[EOD 계산 완료]</b>\n"
            "<i>결과: TODO - eod.calculator 연동 필요</i>",
            parse_mode='HTML')
    
    # ============================================================
    # 헬퍼
    # ============================================================
    
    def _get_phase(self, state: dict) -> str:
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
                return "[후반전 ⚠️소진임박]"
            return "[후반전]"
        return "[소진⚡→리버스]"
