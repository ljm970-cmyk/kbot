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
import logging
import html
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from core.star_point import StarPointCalculator
from core.timezone_handler import USMarketTimezone


logger = logging.getLogger("kbot.telegram")


class CommandsHandler:
    """명령어 처리 (COMPLETE)"""

    def __init__(self, state_manager, kiwoom_api, config):
        self.state = state_manager
        self.kiwoom = kiwoom_api
        self.config = config
        self.tz = USMarketTimezone()
        # 봇이 낸 주문과 사용자가 직접 낸 주문을 구분하려고 원장을 쓴다.
        # 스케줄러가 만든 원장을 봇이 연결해준다 (없으면 구분 없이 표시).
        self.registry = None
    
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
            except Exception:
                # bare except 는 KeyboardInterrupt·SystemExit 까지 삼켜
                # 종료 신호를 막는다.
                return None
    
    async def _retry_api(self, func, *args, default=None, retries=3, **kwargs):
        """동기·비동기 함수를 모두 받아 재시도한다.

        기존 구현은 무조건 await 했는데, state_manager 의 메서드는 동기라
        TypeError 가 나고 3회 재시도 후 조용히 default 를 돌려줬다.
        그래서 /status 가 모든 종목을 '상태 없음' 으로 표시했다.

        프로그래밍 오류(TypeError/AttributeError 등)는 재시도해도 같은
        결과이므로 즉시 로그를 남기고 중단한다. 재시도는 네트워크·일시
        오류에만 의미가 있다.
        """
        import inspect

        for attempt in range(retries):
            try:
                result = func(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                return result
            except (TypeError, AttributeError, KeyError, ValueError) as e:
                logger.error("API 호출 오류 (재시도 안 함) %s: %s",
                             getattr(func, "__name__", func), e)
                return default
            except Exception as e:
                if attempt == retries - 1:
                    logger.warning("API 호출 %d회 실패 %s: %s",
                                   retries, getattr(func, "__name__", func), e)
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
            
            mode_icon = "📈" if st['mode'] == 'normal' else "🔄"
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
            #
            # 원본은 f-string 두 줄을 이어 썼는데, 둘째 줄은 report 에
            # 더해지지 않고 버려지는 식(no-op)이었다. 매수가·회복가가
            # 화면에 아예 나오지 않았다.
            #
            # 그리고 리버스 별지점은 직전 5거래일 종가 평균이라 평단·T로는
            # 계산할 수 없다. 모드를 가리지 않고 calculate(avg, T)를 부르면
            # 리버스 종목이 있을 때 /status 전체가 실패한다.
            if st['avg_price'] > 0:
                if st['mode'] == 'normal':
                    from core.star_point import star_pct
                    star = StarPointCalculator(ticker, st['division'], 'normal')
                    sc = star.calculate(st['avg_price'], st['T'])
                    try:
                        pct = star_pct(ticker, st['division'], st['T'])
                        pct_txt = f" <code>({pct:+.2f}%)</code>"
                    except ValueError:
                        pct_txt = ""
                    # 속성명은 star 다. star_point 로 쓰면 AttributeError 가 나고
                    # cmd_status 가 "스캔 중..." 에서 멈춘다. 보유가 0이면 이
                    # 블록을 건너뛰어서, 첫 매수가 체결된 날에야 드러난다.
                    report += (
                        f"├ ⭐ 별지점: <code>${sc.star:.2f}</code>{pct_txt}\n"
                        f"├ 　 매수 <code>${sc.buy_price:.2f}</code> · "
                        f"매도 <code>${sc.sell_price:.2f}</code>\n"
                    )
                else:
                    # 리버스 종료 기준가. 구버전 상수(STAR_PCT_REVERSE)는
                    # 더 이상 없다 — 이 줄 때문에 리버스로 전환되는 날
                    # /status 가 통째로 멈췄을 것이다.
                    recover = StarPointCalculator.recover_price(
                        ticker, st['avg_price'])
                    ma5 = st.get('last_star_point', 0) or 0
                    if ma5 > 0:
                        report += (
                            f"├ ⭐ 별지점(MA5): <code>${ma5:.2f}</code> "
                            f"(회복: <code>${recover:.2f}</code>)\n"
                        )
                    else:
                        report += (
                            f"├ ⭐ 별지점: <i>MA5 대기중</i> "
                            f"(회복: <code>${recover:.2f}</code>)\n"
                        )
            
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
    
    #: 주문 태그 → 사람이 읽을 이름
    _TAG_NAMES = {
        "entry_buy": "처음매수", "star_buy": "별지점매수", "avg_buy": "평단매수",
        "half_buy": "후반전매수", "merged_buy": "병합매수", "guard_buy": "대체매수",
        "crash_buy": "폭락대비", "quarter_sell": "쿼터매도", "target_sell": "목표매도",
        "reverse_sell": "리버스매도", "reverse_moc": "리버스MOC",
        "reverse_quarter_buy": "쿼터매수",
    }

    async def cmd_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """📋 주문 내역 — 실시간 미체결 + 예약주문.

        원본은 예약주문(ust21205)만 조회했다. 지정가·LOC 를 프리장에 실시간
        으로 내게 바꾼 뒤로는 미체결에 주문이 있어도 "예약주문 없음" 으로
        나왔다. 필드명(ord_gubun, slby_gubun)도 실제 응답과 달랐다.
        """
        from kiwoom.constants import exchange_of

        user_id = await self._get_user_id(update)
        active = self.state.get_user_tickers(user_id)
        if not active:
            await self._safe_reply(update.effective_message, "❌ 설정된 종목이 없습니다.")
            return

        targets = active
        if context.args:
            t = context.args[0].upper()
            if t not in active:
                await self._safe_reply(update.effective_message,
                                       f"❌ 미설정 종목: {html.escape(t)}")
                return
            targets = [t]

        L = ["📋 <b>[ 주문 내역 ]</b>", ""]
        for t in targets:
            ex = exchange_of(t)
            live = await self._retry_api(self.kiwoom.get_open_orders, t, ex, default=None)
            rsv = await self._retry_api(self.kiwoom.get_reserved_orders,
                                        ticker=t, exchange=ex, default=None)

            ours_live, ours_rsv = {}, {}
            if self.registry is not None:
                ours_live = self.registry.bot_live_orders(t)
                ours_rsv = self.registry.bot_reserved_orders(t)

            # ── 실시간 미체결 ──
            if live is None:
                L.append(f"<b>{t}</b> 미체결 — 조회 실패")
            else:
                L.append(f"<b>{t}</b> 미체결 {len(live)}건")
                for o in sorted(live, key=lambda x: -(x.get("ord_uv") or 0)):
                    rec = ours_live.get(str(o.get("ord_no")))
                    who = "🤖" if rec else "👤"
                    name = self._TAG_NAMES.get(rec.tag, rec.tag) if rec else "직접 주문"
                    side = "🔴매수" if o.get("side") == "buy" else "🔵매도"
                    qty = int(o.get("remain_qty") or o.get("ord_qty") or 0)
                    kind = str(o.get("trade_type_nm", ""))
                    kind = "LOC" if "On Close" in kind and "Limit" in kind else \
                           "MOC" if "On Close" in kind else kind
                    L.append(f"  {who} {side} {kind} "
                             f"{qty}주 @{float(o.get('ord_uv') or 0):.2f}  {name}")
                if not live:
                    L.append("  <i>없음</i>")

            # ── 예약주문 (MOC 등) ──
            if rsv:
                L.append(f"<b>{t}</b> 예약주문 {len(rsv)}건")
                for o in rsv:
                    rec = ours_rsv.get(str(o.get("rsrv_ord_no")))
                    who = "🤖" if rec else "👤"
                    L.append(f"  {who} {o.get('slby_tp', '')} {o.get('trde_nm', '')} "
                             f"{int(o.get('ord_qty') or 0)}주 @{o.get('ord_uv', '')}  "
                             f"{o.get('proc_tp', '')}")
            L.append("")

        L.append("🤖 봇 주문  👤 직접 주문")
        keyboard = [[InlineKeyboardButton("🔄 새로고침", callback_data="ORDERS:REFRESH"),
                     InlineKeyboardButton("🚨 긴급정지", callback_data="ORDER:CANCEL_ALL")]]
        await self._safe_reply(update.effective_message, "\n".join(L),
                               parse_mode='HTML',
                               reply_markup=InlineKeyboardMarkup(keyboard))

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
            # 수수료는 실제로 쓰이는 fee_rate 에서 계산한다.
            # fee_display 는 마법사로 설정할 때만 저장돼서, 다른 경로로
            # 바꾸면 기본값 0.25% 가 그대로 보였다.
            fee_display = f"{float(cfg.get('fee_rate', 0.0007)) * 100:.3f}".rstrip("0").rstrip(".")

            # "자동주문 ON" 은 실제 동작과 연결돼 있지 않았다.
            # 주문이 실제로 나가는지는 회로차단기 상태가 결정한다.
            st = self.state.get_state(ticker)
            if st is not None and getattr(st, "halted", False):
                auto = f"정지 ({st.halt_reason[:30]})"
            else:
                auto = "가동"
            
            msg += (
                f"💎 <b>{html.escape(ticker)}</b>\n"
                f"├ 분할: <code>{division}</code>\n"
                f"├ 원금: <code>${principal:,.0f}</code>\n"
                f"├ 수수료: <code>{fee_display}%</code>\n"
                f"└ 주문: <code>{auto}</code>\n\n"
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
            # 주문 정지/해제만 남긴다.
            # 샌드박스(모의투자) 전환은 실행 중에 바꾸면 위험해서 뺐다 —
            # 모드는 서비스 설정(KIWOOM_MOCK)으로만 바꾼다.
            # 알림 토글은 구현이 없어서 뺐다.
            [InlineKeyboardButton("⏸ 주문 정지", callback_data="CONFIG:HALT"),
             InlineKeyboardButton("▶ 정지 해제", callback_data="CONFIG:UNHALT")],
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
        
        # 구버전은 여기서 cfg.get('T', 0) 으로 진행 여부를 판단했는데,
        # T 는 설정이 아니라 상태 파일에 있어 항상 0 으로 읽혔다. 게다가
        # cfg['cash'] 를 써도 아무도 읽지 않아 변경이 반영되지 않았다.
        # 저장 후 apply_config 가 실제 상태에 반영하고 제약을 판단한다.
        self.state.save_ticker_config(user_id, ticker, cfg)
        res = self.state.apply_config(ticker)

        # 수수료는 퍼센트로 보여준다. 부동소수점 그대로 찍으면
        # 0.00070000000000000001 처럼 나온다.
        def _fmt(v):
            if real_key == 'fee_rate':
                try:
                    return f"{float(v) * 100:.3f}".rstrip("0").rstrip(".") + "%"
                except (TypeError, ValueError):
                    return str(v)
            if real_key == 'principal':
                try:
                    return f"${float(v):,.0f}"
                except (TypeError, ValueError):
                    return str(v)
            return str(v)

        lines = [f"✅ <b>{html.escape(ticker)}</b> 설정 변경:",
                 f"<code>{display_name}</code>: {_fmt(old)} → <b>{_fmt(converted)}</b>"]
        if res.get('changed'):
            lines.append("")
            lines.append("<b>실행 상태 반영</b>")
            lines += [f"├ {html.escape(c)}" for c in res['changed']]
        if res.get('blocked'):
            lines.append("")
            lines.append("<b>⚠️ 반영되지 않음</b>")
            lines += [f"├ {html.escape(b)}" for b in res['blocked']]
        if res.get('error'):
            lines.append(f"⚠️ {html.escape(res['error'])}")

        await self._safe_reply(update.effective_message, "\n".join(lines),
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
        current_price = await self._retry_api(
            self.kiwoom.get_current_price, ticker, default=0
        )
        input_price = self._safe_float(price)
        
        if current_price > 0 and input_price > 0:
            lower, upper = current_price * 0.4, current_price * 1.6
            if input_price < lower or input_price > upper:
                await self._safe_reply(update.effective_message,
                    f"🚨 <b>오입력 차단</b>\n"
                    f"현재가 ${current_price:.2f} 대비 ${input_price:.2f}는 ±60% 초과",
                    parse_mode='HTML')
                return
        
        result = self.state.add_manual_correction(user_id, ticker, {
            'date': date_str,
            'qty': int(qty),
            'price': input_price,
            'side': side,
            'type': 'MANUAL_FIX'
        })

        # 구버전은 반환값을 확인하지 않아, 실패해도 사용자에게 아무 응답이
        # 가지 않았다. 게다가 기록만 하고 장부에 반영되지 않았다.
        if not result or not result.get('ok'):
            reason = (result or {}).get('error', '알 수 없는 오류')
            await self._safe_reply(update.effective_message,
                f"⚠️ <b>[{html.escape(ticker)}] 보정 실패</b>\n{html.escape(str(reason))}",
                parse_mode='HTML')
            return

        b, a = result['before'], result['after']
        await self._safe_reply(update.effective_message,
            f"✅ <b>[{html.escape(ticker)}] 수동 보정 완료</b>\n"
            f"├ 날짜: <code>{date_str}</code>\n"
            f"├ 구분: <code>{side}</code>\n"
            f"├ 수량: <code>{qty}</code>주\n"
            f"├ 가격: <code>${input_price:.2f}</code>\n"
            f"├ 수수료: <code>${result.get('fee', 0):.2f}</code>\n\n"
            f"<b>장부 변화</b>\n"
            f"├ 보유: <code>{b['holdings']} → {a['holdings']}</code>주\n"
            f"├ 평단: <code>${b['avg_price']:.4f} → ${a['avg_price']:.4f}</code>\n"
            f"└ 잔금: <code>${b['cash']:,.2f} → ${a['cash']:,.2f}</code>\n\n"
            f"<i>T값은 바뀌지 않습니다. 필요하면 /status 로 확인 후 조정하세요.</i>",
            parse_mode='HTML')
    
    async def cmd_force_calc(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """구버전 진입점.

        실제 EOD 는 스케줄러가 돌린다(bot.py 의 _cmd_force_eod).
        여기로 들어오면 연결이 끊긴 것이므로 그 사실을 알린다.
        예전처럼 '계산 완료' 라고 응답해서 정산이 끝난 것처럼 보이면 안 된다.
        """
        await self._safe_reply(update.effective_message,
            "⚠️ <b>[EOD 계산]</b>\n"
            "스케줄러 연결이 필요합니다. <code>/run eod</code> 를 사용하세요.",
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
