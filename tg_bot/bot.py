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
    ApplicationHandlerStop,
    TypeHandler,
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


class TelegramNotifier:
    """스케줄러·EOD 가 관리자에게 알림을 보내는 채널.

    Notifier 프로토콜(async send(text))을 만족한다.
    전송 실패가 매매 흐름을 막으면 안 되므로 예외를 삼키고 로그만 남긴다.
    """

    MAX_LEN = 4000      # 텔레그램 메시지 길이 제한 (4096) 여유분

    def __init__(self, application, telegram_config):
        self.application = application
        self.telegram = telegram_config
        self._log = logging.getLogger("kbot.notify")

    async def send(self, text: str) -> None:
        if not text:
            return
        for chunk in self._split(text):
            for admin_id in self.telegram.admin_ids:
                try:
                    await self.application.bot.send_message(chat_id=admin_id, text=chunk)
                except Exception as e:
                    self._log.warning("알림 전송 실패 (%s): %s", admin_id, e)

    def _split(self, text: str):
        while len(text) > self.MAX_LEN:
            cut = text.rfind("\n", 0, self.MAX_LEN)
            cut = cut if cut > 0 else self.MAX_LEN
            yield text[:cut]
            text = text[cut:].lstrip("\n")
        if text:
            yield text


class KbotTelegramBot:
    """
    텔레그램 봇 메인 컨트롤러
    
    사용법 (main.py에서):
        from tg_bot.bot import KbotTelegramBot
        from core.state_manager import StateManager
        from kiwoom.api_client import KiwoomAPIClient
        
        state_mgr = StateManager(config.paths.data)
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
        
        self.notifier = TelegramNotifier(self.application, config.telegram)
        from tg_bot.fix_ui import FixSession
        self.fix_session = FixSession()

        self._setup_handlers()

    def _setup_handlers(self):
        """핸들러 등록"""
        app = self.application
        
        # ========== 관리자 인증 (반드시 최우선) ==========
        # group=-1 은 다른 모든 핸들러보다 먼저 돈다. 여기서 막히면
        # ConversationHandler 와 콜백까지 전부 차단된다.
        #
        # 이 가드가 없으면 봇 사용자명을 아는 누구나
        #   /start   원금·분할수 변경
        #   /fix     가짜 체결 주입 (T값·평단 조작)
        #   /calceod 강제 정산
        # 을 실행할 수 있다. 실제 돈이 걸린 계좌다.
        app.add_handler(TypeHandler(Update, self._require_admin), group=-1)

        # ========== 핵심 명령어 ==========
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("status", self.cmd_handler.cmd_status))
        app.add_handler(CommandHandler("st", self.cmd_handler.cmd_status))
        app.add_handler(CommandHandler("orders", self.cmd_handler.cmd_orders))
        app.add_handler(CommandHandler("history", self.cmd_handler.cmd_history))
        app.add_handler(CommandHandler("hist", self.cmd_handler.cmd_history))
        app.add_handler(CommandHandler("config", self.cmd_handler.cmd_config))
        app.add_handler(CommandHandler("fix", self._cmd_fix_ui))
        app.add_handler(CallbackQueryHandler(
            self._on_fix_callback, pattern=r"^fix:"))
        app.add_handler(CommandHandler("calceod", self._cmd_force_eod))
        
        # 기존 호환
        app.add_handler(CommandHandler("sync", self.cmd_handler.cmd_status))
        app.add_handler(CommandHandler("record", self.cmd_handler.cmd_history))
        
        # ========== 스케줄러 제어 ==========
        app.add_handler(CommandHandler("next", self._cmd_next))
        app.add_handler(CommandHandler("pause", self._cmd_pause))
        app.add_handler(CommandHandler("resume", self._cmd_resume))
        app.add_handler(CommandHandler("run", self._cmd_run))
        app.add_handler(CommandHandler("halt", self._cmd_halt))
        app.add_handler(CommandHandler("unhalt", self._cmd_unhalt))
        app.add_handler(CommandHandler("report", self._cmd_report))
        app.add_handler(CommandHandler("stats", self._cmd_stats))
        app.add_handler(CommandHandler("health", self._cmd_health))
        app.add_handler(CommandHandler("panic", self._cmd_panic))
        app.add_handler(CallbackQueryHandler(
            self._on_panic_callback, pattern=r"^panic:"))

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
    # 관리자 인증
    # ============================================================

    async def _require_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """관리자가 아니면 모든 처리를 중단한다.

        ApplicationHandlerStop 을 올리면 이후 핸들러가 실행되지 않는다.
        거부 사실을 로그에 남겨 누가 접근했는지 확인할 수 있게 한다.
        """
        user = update.effective_user
        if user is not None and self.config.telegram.is_admin(user.id):
            return

        uid = getattr(user, "id", "unknown")
        uname = getattr(user, "username", "")
        logger.warning("관리자가 아닌 접근 차단: id=%s username=%s", uid, uname)

        msg = update.effective_message
        if msg is not None:
            try:
                await msg.reply_text(
                    f"이 봇은 관리자 전용입니다.\n"
                    f"본인 계정이라면 .env 의 TELEGRAM_ADMIN_ID 에 {uid} 를 추가하세요."
                )
            except Exception:
                pass
        raise ApplicationHandlerStop

    # ============================================================
    # 스케줄러 제어
    # ============================================================

    async def _cmd_next(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """다음 거래일 일정"""
        if self.scheduler is None:
            return await self._safe_send(update, "스케줄러가 연결되지 않았습니다.")
        await self._safe_send(update, self.scheduler.status())

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """주문 접수 일시정지. 보유 물량은 그대로 두고 신규 주문만 멈춘다."""
        if self.scheduler is None:
            return await self._safe_send(update, "스케줄러가 연결되지 않았습니다.")
        self.scheduler.scheduler.pause()
        await self._safe_send(
            update,
            "스케줄러를 일시정지했습니다.\n"
            "예약된 주문 접수와 EOD 정산이 멈춥니다.\n"
            "이미 증권사에 접수된 주문은 그대로 살아 있으니, "
            "취소가 필요하면 증권사 앱에서 직접 처리하세요.\n"
            "/resume 으로 재개합니다."
        )

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.scheduler is None:
            return await self._safe_send(update, "스케줄러가 연결되지 않았습니다.")
        self.scheduler.scheduler.resume()
        await self._safe_send(update, "스케줄러를 재개했습니다.\n\n"
                                      + self.scheduler.status())

    async def _cmd_run(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """작업 즉시 실행: /run plan|submit|verify|eod"""
        if self.scheduler is None:
            return await self._safe_send(update, "스케줄러가 연결되지 않았습니다.")
        args = context.args or []
        if not args:
            return await self._safe_send(
                update,
                "사용법: /run <작업>\n\n"
                "  plan    다음 거래일 일정 재계산\n"
                "  submit  전체 주문 즉시 접수 (목표매도 + LOC)\n"
                "  target  목표 지정가매도만\n"
                "  loc     LOC 예약주문만\n"
                "  verify  예약주문 생존 확인\n"
                "  eod     정산 즉시 실행\n\n"
                "submit 은 실제로 주문을 냅니다."
            )
        try:
            result = await self.scheduler.run_now(args[0].lower())
        except Exception as e:
            logger.exception("수동 실행 실패")
            result = f"실행 실패: {e}"
        await self._safe_send(update, result)

    # ============================================================
    # 회로차단기
    # ============================================================

    async def _cmd_halt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """긴급 정지: /halt [종목] — 신규 주문 접수를 막는다."""
        from core.reconciler import CircuitBreaker

        args = context.args or []
        tickers = [args[0].upper()] if args else self.state.list_tickers()
        if not tickers:
            return await self._safe_send(update, "설정된 종목이 없습니다.")

        done = []
        for t in tickers:
            try:
                with self.state.edit_state(t) as st:
                    CircuitBreaker.halt(st, "사용자가 /halt 로 정지")
                done.append(t)
            except Exception as e:
                logger.exception("정지 실패 %s", t)
                return await self._safe_send(update, f"{t} 정지 실패: {e}")

        await self._safe_send(
            update,
            f"신규 주문을 정지했습니다: {', '.join(done)}\n\n"
            "이미 증권사에 접수된 주문은 그대로 살아 있습니다. "
            "취소가 필요하면 증권사 앱에서 직접 처리하세요.\n"
            "/unhalt 로 해제합니다."
        )

    async def _cmd_unhalt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """정지 해제: /unhalt <종목>

        자동 해제는 하지 않는다. 원인을 확인하지 않은 채 다시 주문이
        나가는 것을 막기 위해서다.
        """
        from core.reconciler import CircuitBreaker

        args = context.args or []
        if not args:
            lines = ["사용법: /unhalt <종목>", ""]
            for t in self.state.list_tickers():
                st = self.state.get_state(t)
                if st and CircuitBreaker.is_halted(st):
                    lines.append(f"{t} — 정지 중")
                    lines.append(f"  사유: {st.halt_reason}")
                    lines.append(f"  시각: {st.halted_at}")
            if len(lines) == 2:
                lines.append("정지된 종목이 없습니다.")
            return await self._safe_send(update, "\n".join(lines))

        ticker = args[0].upper()
        try:
            with self.state.edit_state(ticker) as st:
                released = CircuitBreaker.release(st)
        except Exception as e:
            return await self._safe_send(update, f"{ticker} 해제 실패: {e}")

        if released:
            await self._safe_send(
                update,
                f"{ticker} 주문 정지를 해제했습니다.\n"
                "다음 거래일부터 정상 접수됩니다.\n"
                "장부가 실제 잔고와 맞는지 /status 로 한 번 더 확인하세요."
            )
        else:
            await self._safe_send(update, f"{ticker} 는 정지 상태가 아닙니다.")

    async def _cmd_force_eod(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """강제 EOD 정산.

        구버전 cmd_force_calc 는 TODO 상태로 '계산 완료' 만 응답했다.
        실제로는 아무것도 돌지 않아, 사용자가 정산이 끝난 줄 알게 된다.
        스케줄러의 실제 EOD 경로로 연결한다.
        """
        if self.scheduler is None:
            return await self._safe_send(update, "스케줄러가 연결되지 않아 EOD 를 실행할 수 없습니다.")

        await self._safe_send(update, "EOD 정산을 실행합니다. 잠시만 기다려주세요.")
        try:
            await self.scheduler.run_now("eod")
        except Exception as e:
            logger.exception("강제 EOD 실패")
            return await self._safe_send(update, f"EOD 정산 실패: {e}")
        await self._safe_send(update, "EOD 정산을 마쳤습니다. 결과는 위 리포트를 확인하세요.")

    # ============================================================
    # 운영 상태 · 긴급 정지
    # ============================================================

    async def _cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """운영 상태: /health"""
        from core.ops import health_report
        try:
            text = health_report(self.config, self.kiwoom, self.state,
                                 self.scheduler, self.ws)
        except Exception as e:
            logger.exception("헬스체크 실패")
            text = f"상태 확인 실패: {e}"
        await self._safe_send(update, text)

    async def _cmd_panic(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """긴급 정지: /panic

        되돌리기 어려운 동작이므로 한 번 더 확인을 받는다.
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        tickers = self.state.list_tickers()
        if not tickers:
            return await self._safe_send(update, "설정된 종목이 없습니다.")

        can_cancel = self.kiwoom.can_cancel_now()
        lines = [
            "긴급 정지를 실행할까요?",
            "",
            f"  대상 종목  {', '.join(tickers)}",
            "  신규 주문을 멈추고,",
            "  봇이 낸 예약주문을 취소합니다.",
            "",
            "  직접 거신 주문은 건드리지 않습니다.",
        ]
        if not can_cancel:
            lines += ["",
                      "  ⚠ 지금은 취소 가능 시간(08:00~22:25 KST)이 아닙니다.",
                      "    정지는 되지만 취소는 실패합니다."]

        await update.effective_message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("실행", callback_data="panic:go"),
                 InlineKeyboardButton("취소", callback_data="panic:no")],
            ]))

    async def _on_panic_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        from core.ops import panic_stop
        from kiwoom.constants import exchange_of

        query = update.callback_query
        await query.answer()
        if query.data != "panic:go":
            return await query.edit_message_text("긴급 정지를 취소했습니다.")

        registry = getattr(self.scheduler, "registry", None)
        if registry is None:
            return await query.edit_message_text(
                "주문 원장에 접근할 수 없어 취소를 진행할 수 없습니다.")

        await query.edit_message_text("긴급 정지 실행 중...")
        try:
            res = await panic_stop(self.kiwoom, self.state, registry, exchange_of)
        except Exception as e:
            logger.exception("긴급 정지 실패")
            return await query.edit_message_text(f"긴급 정지 실패: {e}")
        await query.edit_message_text(res.report())

    # ============================================================
    # 누적 통계
    # ============================================================

    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """누적 성과: /stats [종목]"""
        from eod.stats import collect, format_stats

        args = context.args or []
        tickers = [args[0].upper()] if args else self.state.list_tickers()
        if not tickers:
            return await self._safe_send(update, "설정된 종목이 없습니다.")
        for t in tickers:
            try:
                await self._safe_send(update, format_stats(collect(self.state, t)))
            except Exception as e:
                logger.exception("통계 집계 실패 %s", t)
                await self._safe_send(update, f"[{t}] 통계 집계 실패: {e}")

    # ============================================================
    # 버튼식 수동 보정
    # ============================================================

    async def _cmd_fix_ui(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """버튼식 보정 시작: /fix (인자를 주면 기존 텍스트 방식)"""
        from tg_bot.fix_ui import keyboard_tickers

        if context.args:
            # 기존 텍스트 방식 유지 (/fix TQQQ 20260915 3 68.10 buy)
            return await self.cmd_handler.cmd_fix(update, context)

        tickers = self.state.list_tickers()
        if not tickers:
            return await self._safe_send(update, "설정된 종목이 없습니다.")

        self.fix_session.reset(update.effective_user.id)
        await update.effective_message.reply_text(
            "수동 보정 — 보정할 종목을 고르세요.\n"
            "증권사 실제 잔고와 장부를 맞추는 기능입니다.",
            reply_markup=keyboard_tickers(tickers))

    async def _on_fix_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """보정 버튼 처리"""
        from tg_bot.fix_ui import (
            gap_hint, keyboard_confirm, keyboard_qty, keyboard_side,
            keyboard_t, parse_cb, preview_text, suggest_quantities,
            t_preview_text,
        )
        from core.reconciler import derive_t
        from kiwoom.constants import exchange_of

        query = update.callback_query
        await query.answer()
        parts = parse_cb(query.data)
        if not parts:
            return

        uid = update.effective_user.id
        draft = self.fix_session.get(uid)
        kind = parts[0]

        if kind == "x":
            self.fix_session.reset(uid)
            return await query.edit_message_text("보정을 취소했습니다.")

        if kind == "t":
            draft.ticker = parts[1]
            draft.step = "side"
            state = self.state.get_state(draft.ticker)
            position = await self._safe_position(draft.ticker)
            gap, hint = gap_hint(state, position)
            return await query.edit_message_text(
                f"{draft.ticker} — 무엇을 보정할까요?\n\n{hint}",
                reply_markup=keyboard_side(gap))

        if kind == "s":
            draft.side = parts[1]
            state = self.state.get_state(draft.ticker)

            if draft.side == "setT":
                draft.step = "T"
                derived = (derive_t(state.holdings, state.avg_price,
                                    state.principal, state.division)
                           if state.holdings > 0 and state.avg_price > 0 else None)
                head = (f"{draft.ticker} T값 조정\n\n"
                        f"  현재 T {state.T:.4f} ({state.division}분할)")
                if derived is not None:
                    head += f"\n  잔고 역산 T {derived:.4f}"
                head += ("\n\n역산 T 는 보유원가 ÷ (원금÷분할수) 입니다. "
                         "폭락대비 매수가 많았다면 장부 T 보다 크게 나옵니다.")
                return await query.edit_message_text(
                    head, reply_markup=keyboard_t(state.T, derived))

            draft.step = "qty"
            position = await self._safe_position(draft.ticker)
            opts = suggest_quantities(state, position)
            head = f"{draft.ticker} {'매수' if draft.side == 'buy' else '매도'} 추가 — 수량"
            if position:
                bq = int(position.get("poss_qty") or position.get("qty") or 0)
                head += f"\n  장부 {state.holdings}주 / 증권사 {bq}주"
            return await query.edit_message_text(head, reply_markup=keyboard_qty(opts))

        if kind == "q":
            if parts[1] == "manual":
                draft.step = "qty_input"
                return await query.edit_message_text(
                    f"{draft.ticker} — 수량을 숫자로 보내주세요. (예: 7)")
            draft.qty = int(parts[1])
            draft.step = "price"
            return await query.edit_message_text(
                f"{draft.ticker} {draft.qty}주 — 체결가를 보내주세요. (예: 68.10)")

        if kind == "T":
            if parts[1] == "manual":
                draft.step = "T_input"
                return await query.edit_message_text(
                    f"{draft.ticker} — 새 T값을 보내주세요. (예: 8.25)")
            draft.new_T = float(parts[1])
            draft.step = "confirm"
            state = self.state.get_state(draft.ticker)
            derived = (derive_t(state.holdings, state.avg_price,
                                state.principal, state.division)
                       if state.holdings > 0 and state.avg_price > 0 else None)
            return await query.edit_message_text(
                t_preview_text(state, draft.new_T, derived),
                reply_markup=keyboard_confirm())

        if kind == "ok":
            return await self._apply_fix(query, uid, draft)

    async def _on_fix_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """보정 진행 중 숫자 입력 처리. 처리했으면 True."""
        from tg_bot.fix_ui import keyboard_confirm, preview_text

        uid = update.effective_user.id
        draft = self.fix_session.drafts.get(str(uid))
        if draft is None or draft.step not in ("qty_input", "price", "T_input"):
            return False

        text = (update.effective_message.text or "").strip().replace(",", "")
        try:
            value = float(text)
        except ValueError:
            await self._safe_send(update, "숫자로 보내주세요.")
            return True

        if draft.step == "T_input":
            from core.reconciler import derive_t
            from tg_bot.fix_ui import keyboard_confirm, t_preview_text

            if value < 0:
                await self._safe_send(update, "0 이상이어야 합니다.")
                return True
            state = self.state.get_state(draft.ticker)
            if value > state.division:
                await self._safe_send(update,
                    f"T는 분할수({state.division})를 넘을 수 없습니다.")
                return True
            draft.new_T = value
            draft.step = "confirm"
            derived = (derive_t(state.holdings, state.avg_price,
                                state.principal, state.division)
                       if state.holdings > 0 and state.avg_price > 0 else None)
            await update.effective_message.reply_text(
                t_preview_text(state, value, derived),
                reply_markup=keyboard_confirm())
            return True

        if draft.step == "qty_input":
            if value < 1:
                await self._safe_send(update, "1 이상이어야 합니다.")
                return True
            draft.qty = int(value)
            draft.step = "price"
            await self._safe_send(update,
                f"{draft.ticker} {draft.qty}주 — 체결가를 보내주세요. (예: 68.10)")
            return True

        if value <= 0:
            await self._safe_send(update, "0보다 커야 합니다.")
            return True
        draft.price = value
        draft.step = "confirm"

        state = self.state.get_state(draft.ticker)
        await update.effective_message.reply_text(
            preview_text(state, draft, state.fee_rate),
            reply_markup=keyboard_confirm())
        return True

    async def _safe_position(self, ticker: str):
        """증권사 잔고 조회. 실패해도 흐름을 막지 않는다."""
        from kiwoom.constants import exchange_of
        try:
            return await self.kiwoom.get_position(ticker, exchange_of(ticker))
        except Exception as e:
            logger.warning("[%s] 잔고 조회 실패: %s", ticker, e)
            return None

    async def _apply_fix(self, query, uid, draft) -> None:
        """확인 버튼 → 실제 장부 반영"""
        if draft.side == "setT":
            return await self._apply_set_t(query, uid, draft)

        res = self.state.add_manual_correction(
            str(uid), draft.ticker,
            {"qty": draft.qty, "price": draft.price, "side": draft.side,
             "type": "MANUAL_FIX_UI"})
        self.fix_session.reset(uid)

        if not res or not res.get("ok"):
            return await query.edit_message_text(
                f"보정 실패: {(res or {}).get('error', '알 수 없는 오류')}")

        b, a = res["before"], res["after"]
        await query.edit_message_text(
            f"보정 완료 — {draft.summary()}\n\n"
            f"  보유   {b['holdings']}주 → {a['holdings']}주\n"
            f"  평단   ${b['avg_price']:.4f} → ${a['avg_price']:.4f}\n"
            f"  잔금   ${b['cash']:,.2f} → ${a['cash']:,.2f}\n\n"
            f"장부가 맞는지 /status 로 확인한 뒤,\n"
            f"정지 상태라면 /unhalt {draft.ticker} 로 해제하세요.")

    async def _apply_set_t(self, query, uid, draft) -> None:
        """T값만 직접 설정한다.

        수량·평단은 대조가 자동으로 맞추지만 T 는 체결의 순서와 종류로
        결정되므로 잔고에서 복원할 수 없다. 정지 후 남는 마지막 조각이다.
        """
        try:
            with self.state.edit_state(draft.ticker) as st:
                before = st.T
                st.T = draft.new_T
        except Exception as e:
            self.fix_session.reset(uid)
            return await query.edit_message_text(f"T값 조정 실패: {e}")

        self.state.record_correction(draft.ticker, {
            "source": "manual_fix_ui", "field": "T",
            "before": before, "after": draft.new_T, "reason": "사용자 수동 조정",
        })
        self.fix_session.reset(uid)
        await query.edit_message_text(
            f"T값을 조정했습니다 — {draft.ticker}\n\n"
            f"  T {before:.4f} → {draft.new_T:.4f}\n\n"
            f"다음 거래일 주문은 이 값으로 계산됩니다.\n"
            f"/status 로 확인한 뒤 정지 상태라면 /unhalt {draft.ticker} 로 해제하세요.")

    async def _cmd_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """장 마감 리포트를 지금 다시 보기: /report [종목]"""
        from eod.report import daily_report

        args = context.args or []
        tickers = [args[0].upper()] if args else self.state.list_tickers()
        if not tickers:
            return await self._safe_send(update, "설정된 종목이 없습니다.")

        registry = getattr(self.scheduler, "registry", None)
        if registry is None:
            return await self._safe_send(update, "주문 원장에 접근할 수 없습니다.")

        for t in tickers:
            st = self.state.get_state(t)
            if st is None:
                continue
            try:
                from eod.stats import collect
                stats = collect(self.state, t)
            except Exception:
                stats = None
            await self._safe_send(update, daily_report(st, registry, stats=stats))

    async def _safe_send(self, update: Update, text: str) -> None:
        msg = update.effective_message
        if msg is None:
            return
        try:
            await msg.reply_text(text)
        except Exception:
            logger.exception("메시지 전송 실패")

    # ============================================================
    # 메시지 라우팅 (기존 [5] 확장)
    # ============================================================
    
    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """한글 자연어 명령 라우팅"""
        if not update.effective_message or not update.effective_message.text:
            return
        
        text = update.effective_message.text.strip()

        # 설정 값 입력 대기 중이면 먼저 소비한다.
        if await self._on_config_text(update, context):
            return

        # 보정 진행 중이면 숫자 입력을 먼저 소비한다.
        # (수량·가격을 받는 단계에서 한글 라우팅으로 새면 안 된다)
        if await self._on_fix_text(update, context):
            return

        # 무한매수법 핵심
        if "상태" in text or "현황" in text or "관제탑" in text:
            return await self.cmd_handler.cmd_status(update, context)
        elif "주문내역" in text or "예약주문" in text:
            return await self.cmd_handler.cmd_orders(update, context)
        elif "통계" in text or "성과" in text or "누적" in text:
            return await self._cmd_stats(update, context)
        elif "보정" in text or "수정" in text:
            return await self._cmd_fix_ui(update, context)
        elif "리포트" in text or "이력" in text:
            return await self._cmd_report(update, context)
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
        elif "일정" in text or "스케줄" in text or "다음장" in text:
            return await self._cmd_next(update, context)
        elif "상태점검" in text or "헬스" in text:
            return await self._cmd_health(update, context)
        elif "패닉" in text or "비상" in text:
            return await self._cmd_panic(update, context)
        elif "긴급정지" in text or "주문정지" in text:
            return await self._cmd_halt(update, context)
        elif "정지해제" in text or "해제" in text:
            return await self._cmd_unhalt(update, context)
        elif "일시정지" in text or "중단" in text:
            return await self._cmd_pause(update, context)
        elif "재개" in text or "다시시작" in text:
            return await self._cmd_resume(update, context)
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
        """InlineKeyboardButton 콜백.

        원본은 대부분의 버튼이 같은 화면을 다시 띄우거나 "처리: ..." 만
        출력했다. 버튼마다 실제 동작을 연결한다.
        """
        query = update.callback_query
        data = query.data or ""
        logger.info("[콜백수신] data=%s", data)
        await query.answer()

        parts = data.split(":")
        head = parts[0]
        tickers = self.state.list_tickers()

        # ── 동기화 · 조회 ──
        if head == "SYNC":
            return await self.cmd_handler.cmd_status(update, context)

        if head == "ORDERS":
            # ORDERS:SOXL 이면 종목 지정, ORDERS:REFRESH/VIEW 는 전체
            arg = parts[1] if len(parts) > 1 else ""
            context.args = [arg] if arg in tickers else []
            return await self.cmd_handler.cmd_orders(update, context)

        if head == "HIST":
            return await self.cmd_handler.cmd_history(update, context)

        if data == "CALC:FORCE":
            return await self._cmd_force_eod(update, context)

        if data == "TICKER:ADD":
            return await self._safe_send(
                update, "종목 추가는 /start 로 진행합니다.\n"
                        "개인 보유 종목과 겹치지 않는지 먼저 확인하세요.")

        # ── 주문 취소 ──
        if head == "ORDER":
            if data == "ORDER:CANCEL_ALL":
                # 일괄 취소는 되돌리기 어려우므로 /panic 의 확인 절차를 탄다
                return await self._cmd_panic(update, context)
            return await self._safe_send(
                update, "개별 주문 취소는 증권사 앱에서 해주세요.\n"
                        "봇이 낸 주문을 모두 거두려면 /panic 을 쓰세요.")

        # ── 설정 ──
        if head == "CONFIG":
            action = parts[1] if len(parts) > 1 else ""
            ticker = parts[2] if len(parts) > 2 else ""

            if action in ("DIV", "SEED", "FEE") and ticker:
                return await self._prompt_config_edit(update, context, ticker, action)

            if action == "HALT":
                context.args = []
                return await self._cmd_halt(update, context)

            if action == "UNHALT":
                if len(tickers) == 1:
                    context.args = tickers
                else:
                    context.args = []
                return await self._cmd_unhalt(update, context)

            # EDIT 등 나머지는 설정 메뉴로
            context.args = []
            return await self.cmd_handler.cmd_config(update, context)

        if data == "RESET:CANCEL":
            return await query.edit_message_text("취소했습니다.")

        logger.warning("처리하지 않는 버튼: %s", data)
        await self._safe_send(update, f"이 버튼은 지원하지 않습니다 ({data}).")

    # ── 설정 값 입력 ────────────────────────────────────────────

    _CONFIG_KEYS = {
        "DIV":  ("division",  "분할수", "20 또는 40"),
        "SEED": ("principal", "원금",   "달러 금액 (예: 3500)"),
        "FEE":  ("fee",       "수수료", "퍼센트 (예: 0.07)"),
    }

    async def _prompt_config_edit(self, update, context, ticker: str, action: str):
        key, label, example = self._CONFIG_KEYS[action]
        context.user_data["config_edit"] = (ticker, key, label)
        await self._safe_send(
            update,
            f"{ticker} {label}을(를) 보내주세요.\n"
            f"  형식: {example}\n\n"
            f"취소하려면 '취소' 라고 보내세요.")

    async def _on_config_text(self, update, context) -> bool:
        """설정 값 입력 대기 중이면 처리한다. 처리했으면 True."""
        pending = context.user_data.get("config_edit")
        if not pending:
            return False

        text = (update.effective_message.text or "").strip()
        context.user_data.pop("config_edit", None)
        if text in ("취소", "cancel"):
            await self._safe_send(update, "설정 변경을 취소했습니다.")
            return True

        ticker, key, _ = pending
        context.args = [ticker, key, text.replace(",", "").replace("$", "")]
        await self.cmd_handler.cmd_config(update, context)
        return True

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
            "<b>━━ 스케줄 ━━</b>\n"
            "/next — 다음 거래일 일정\n"
            "/pause — 주문 접수 일시정지\n"
            "/resume — 재개\n"
            "/run &lt;작업&gt; — 즉시 실행 (plan/submit/verify/eod)\n"
            "/health — 운영 상태 점검\n"
            "/panic — 긴급 정지 (정지 + 예약주문 취소)\n"
            "/halt — 신규 주문만 정지\n"
            "/unhalt &lt;종목&gt; — 정지 해제\n"
            "/report [종목] — 매매 이력 리포트\n"
            "/stats [종목] — 누적 성과 (사이클·승률·손익)\n\n"
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
        state_mgr = StateManager(config.paths.data)
        kiwoom = KiwoomAPIClient(config.kiwoom)
        
        bot = await create_telegram_bot(config, state_mgr, kiwoom)
        await bot.run()
    """
    if state_manager is None:
        from core.state_manager import StateManager
        state_manager = StateManager(config.paths.data)
    
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
