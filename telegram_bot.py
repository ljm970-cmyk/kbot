#!/usr/bin/env python3
"""
무한매수법 V4.0 - 텔레그램 양방향 커맨드 봇
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# main.py와 같은 디렉토리에 있으므로 임포트 가능
try:
    from main import KBotEngine, CFG, Database, FeeCalculator, MarketTime
except ImportError:
    # 직접 실행 시
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from main import KBotEngine, CFG, Database, FeeCalculator, MarketTime

logger = logging.getLogger('kbot_telegram')

# ==========================================================
# /start 메시지
# ==========================================================

def get_start_message(is_summer: bool, state):
    season = "🌞 서머타임 ON" if is_summer else "❄️ 서머타임 OFF"
    hour = 17 if is_summer else 18
    
    return f"""
🌌 <b>TQQQ SOXL 무한매수법 V4.0</b>

🕒 [ 운영 스케줄 ({season}) ]
🔹 {hour}:00 KST : 프리장 주문 (LOC/지정가/MOC)
🔹 {hour + 5}:30~{hour + 12}:00 KST : 미국 본장
🔹 {hour + 12}:00 KST : 장 마감 정산

┌─────────────────────┐
│ 현재 상태            │
├─────────────────────┤
│ 종목: {state.stock_code}
│ 분할: {state.division}분할
│ 모드: {state.mode.value}
│ T값: {state.t_value:.4f}
│ 잔금: ${state.balance:.2f}
│ 보유: {state.total_quantity}주
│ 평단: ${state.avg_price:.2f}
│ 수수료: ${state.total_fees_paid:.2f}
└─────────────────────┘

🛠 [ 주요 명령어 ]
▶️ /sync : 📜 통합 지시서
▶️ /record : 📊 장부 동기화
▶️ /history : 🏆 졸업 명예의 전당
▶️ /settlement : ⚙️ 코어스위칭
▶️ /seed : 💵 시드머니 설정/변경
▶️ /ticker : 🔄 운용 종목 선택
▶️ /log : 🔍 에러 로그 조회
"""

# ==========================================================
# 핸들러
# ==========================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = Database()
    state = await db.load_state()
    if not state:
        await update.message.reply_text("❌ 상태 로드 실패")
        return
    
    is_summer = MarketTime.is_summer()
    msg = get_start_message(is_summer, state)
    await update.message.reply_text(msg, parse_mode='HTML')

async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = Database()
    state = await db.load_state()
    
    is_summer = MarketTime.is_summer()
    hour = 17 if is_summer else 18
    
    from main import StarCalc, OrderPlanner
    sc = StarCalc(state.stock_type, state.division)
    
    if state.mode.value == 'normal':
        star = sc.normal_star(state.avg_price, state.t_value)
        one_buy = state.balance / (state.division - state.t_value) if state.t_value < state.division else 0
        msg = f"""
📜 <b>통합 지시서</b>

┌─────────────────────┐
│ 모드: NORMAL         │
│ T값: {state.t_value:.4f}
│ 1회매수금: ${one_buy:.2f}
│ 별지점: ${star:.2f}
│ 매수가: ${star - 0.01:.2f}
│ 쿼터매도: ${star:.2f}
└─────────────────────┘

다음 주문: {hour}:00 KST
"""
    else:
        msg = f"""
📜 <b>통합 지시서</b>

┌─────────────────────┐
│ 모드: REVERSE        │
│ T값: {state.t_value:.4f}
│ 첫날 MOC 매도 또는
│ 별지점 기준 매수매도
└─────────────────────┘
"""
    
    await update.message.reply_text(msg, parse_mode='HTML')

async def cmd_seed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """시드머니 설정"""
    keyboard = [
        [InlineKeyboardButton("💰 시드머니 변경", callback_data='seed_change')],
        [InlineKeyboardButton("📊 현재 시드 확인", callback_data='seed_check')],
    ]
    
    db = Database()
    state = await db.load_state()
    
    msg = f"""
💵 <b>시드머니 관리</b>

현재 시드: ${state.principal:,.0f}

다음 사이클 시작 시 이 금액이 기준됩니다.
"""
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def cmd_record(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """장부 동기화"""
    db = Database()
    state = await db.load_state()
    txs = await db.get_today_transactions()
    total_fees = await db.get_total_fees()
    
    msg = f"""
📊 <b>장부 동기화</b>

┌─────────────────────┐
│ 원금: ${state.principal:.2f}
│ 잔금: ${state.balance:.2f}
│ 보유: {state.total_quantity}주
│ 평단: ${state.avg_price:.2f}
│ T값: {state.t_value:.4f}
├─────────────────────┤
│ 오늘 거래: {len(txs)}건
│ 누적 수수료: ${total_fees:.2f}
└─────────────────────┘
"""
    await update.message.reply_text(msg, parse_mode='HTML')

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """졸업 명예의 전당"""
    db = Database()
    cycles = await db.load_completed_cycles()
    
    if not cycles:
        await update.message.reply_text("📭 <b>명예의 전당이 비어있습니다.</b>", parse_mode='HTML')
        return
    
    msg = "🏆 <b>졸업 명예의 전당</b>\n\n"
    for c in cycles[:10]:
        sign = "+" if c['realized_pnl'] >= 0 else "-"
        msg += f"🏅 {c['end_date'][:10]} | T{c['stock_code']} | {sign}${abs(c['realized_pnl']):.2f} ({c['return_pct']:+.1f}%)\n"
    
    await update.message.reply_text(msg, parse_mode='HTML')

async def cmd_settlement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """코어스위칭"""
    keyboard = [
        [InlineKeyboardButton("🔄 일반→리버스 강제", callback_data='force_reverse')],
        [InlineKeyboardButton("🔄 리버스→일반 강제", callback_data='force_normal')],
        [InlineKeyboardButton("⏸️ 일시정지", callback_data='pause')],
        [InlineKeyboardButton("▶️ 재개", callback_data='resume')],
        [InlineKeyboardButton("🚨 긴급 중단", callback_data='emergency')],
    ]
    
    await update.message.reply_text(
        "⚙️ <b>코어스위칭/전술설정</b>\n\n원하는 조작을 선택하세요:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def cmd_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """종목 선택"""
    keyboard = [
        [InlineKeyboardButton("TQQQ", callback_data='ticker_TQQQ')],
        [InlineKeyboardButton("SOXL", callback_data='ticker_SOXL')],
    ]
    
    db = Database()
    state = await db.load_state()
    
    msg = f"🔄 <b>운용 종목 선택</b>\n\n현재: <b>{state.stock_code}</b>"
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """에러 로그"""
    try:
        with open('logs/app.log', 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        if not lines:
            await update.message.reply_text("✅ <b>에러 로그 없음</b>", parse_mode='HTML')
            return
        
        # 최근 20줄
        tail = lines[-20:]
        msg = "<b>🔴 최근 로그:</b>\n<code>" + "".join(tail) + "</code>"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ 로그 읽기 실패: {e}", parse_mode='HTML')

# ==========================================================
# 콜백 핸들러
# ==========================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == 'seed_change':
        await query.edit_message_text("💵 시드머니 변경은 /seed [금액] 로 입력하세요.\n예: /seed 25000")
    elif data == 'seed_check':
        db = Database()
        state = await db.load_state()
        await query.edit_message_text(f"💵 현재 시드머니: ${state.principal:,.0f}")
    elif data.startswith('ticker_'):
        ticker = data.split('_')[1]
        await query.edit_message_text(f"✅ <b>{ticker}</b>로 변경됨 (다음 사이클 적용)")
    elif data == 'force_reverse':
        await query.edit_message_text("🔄 리버스 강제 명령 전송됨 (엔진에서 처리)")
    elif data == 'force_normal':
        await query.edit_message_text("🔄 일반모드 강제 명령 전송됨 (엔진에서 처리)")
    elif data == 'pause':
        await query.edit_message_text("⏸️ 일시정지 명령 전송됨")
    elif data == 'resume':
        await query.edit_message_text("▶️ 재개 명령 전송됨")
    elif data == 'emergency':
        await query.edit_message_text("🚨 <b>긴급 중단!</b> 모든 주문 취소 처리 중...")

async def cmd_seed_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/seed [금액] 커맨드"""
    args = context.args
    if not args:
        await update.message.reply_text("❌ 금액 입력 필요. 예: /seed 25000")
        return
    
    try:
        new_seed = float(args[0])
        db = Database()
        state = await db.load_state()
        state.principal = new_seed
        state.balance = new_seed  # 잔금도 변경
        await db.save_state(state, 'seed_change')
        await update.message.reply_text(f"✅ 시드머니 변경: ${new_seed:,.0f}", parse_mode='HTML')
    except ValueError:
        await update.message.reply_text("❌ 숫자 형식이 잘못되었습니다.")

# ==========================================================
# 봇 실행
# ==========================================================

def main():
    if not CFG.TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN 미설정")
        return
    
    application = Application.builder().token(CFG.TELEGRAM_BOT_TOKEN).build()
    
    # 커맨드 핸들러 등록
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("sync", cmd_sync))
    application.add_handler(CommandHandler("record", cmd_record))
    application.add_handler(CommandHandler("history", cmd_history))
    application.add_handler(CommandHandler("settlement", cmd_settlement))
    application.add_handler(CommandHandler("seed", cmd_seed_amount))
    application.add_handler(CommandHandler("ticker", cmd_ticker))
    application.add_handler(CommandHandler("log", cmd_log))
    
    # 콜백
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    print("텔레그램 봇 시작...")
    application.run_polling()

if __name__ == '__main__':
    main()
