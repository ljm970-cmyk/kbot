"""
================================================================
버튼식 수동 보정 (/fix)

기존 /fix 는 인자를 전부 타이핑해야 했다.

    /fix TQQQ 20260915 3 68.10 buy

정지가 걸린 상황은 대개 급하고, 숫자가 하나만 틀려도 장부가 더
어긋난다. 그래서 단계별 버튼으로 받고, 마지막에 **적용 전후를
보여주고 확인을 받는다**.

    종목 선택 → 매수/매도 → 수량 → 가격 입력 → 미리보기 → 확인

수량은 증권사 잔고와의 차이를 계산해 버튼으로 먼저 제시한다.
대부분의 보정은 "장부가 3주 모자람" 같은 형태이므로 한 번에 끝난다.
================================================================
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("kbot.fix")

#: 콜백 데이터 접두사
CB = "fix"

#: 수량 버튼으로 제시할 기본 후보
QTY_PRESETS = (1, 2, 3, 5, 10)


@dataclass
class FixDraft:
    """작성 중인 보정 내용"""
    ticker: str = ""
    side: str = ""            # buy / sell / setT
    qty: int = 0
    price: float = 0.0
    new_T: float = 0.0
    step: str = "ticker"      # ticker → side → qty → price → confirm

    def summary(self) -> str:
        if self.side == "setT":
            return f"{self.ticker} T값 → {self.new_T:.4f}"
        side = {"buy": "매수", "sell": "매도"}.get(self.side, "?")
        return f"{self.ticker} {side} {self.qty}주 @${self.price:.2f}"


@dataclass
class FixSession:
    """사용자별 진행 중인 보정 (메모리 보관)"""
    drafts: dict[str, FixDraft] = field(default_factory=dict)

    def get(self, user_id) -> FixDraft:
        return self.drafts.setdefault(str(user_id), FixDraft())

    def reset(self, user_id) -> None:
        self.drafts.pop(str(user_id), None)


# ================================================================
# 화면 구성
# ================================================================

def suggest_quantities(state, broker_position: Optional[dict]) -> list[tuple[int, str]]:
    """수량 후보. 증권사 잔고와의 차이를 최우선으로 제시한다.

    Returns:
        [(수량, 라벨), ...]
    """
    out: list[tuple[int, str]] = []
    if broker_position:
        bq = int(broker_position.get("poss_qty") or broker_position.get("qty") or 0)
        gap = bq - state.holdings
        if gap:
            out.append((abs(gap), f"차이 {abs(gap)}주 ({'부족' if gap > 0 else '초과'})"))
    for q in QTY_PRESETS:
        if all(q != x for x, _ in out):
            out.append((q, f"{q}주"))
    return out[:6]


def gap_hint(state, broker_position: Optional[dict]) -> tuple[int, str]:
    """장부와 증권사의 수량 차이와 안내 문구.

    Returns:
        (차이, 안내문). 차이가 0이면 수량 보정이 불필요하다는 안내.
    """
    if not broker_position:
        return 0, "증권사 잔고를 조회하지 못했습니다. 수량은 직접 확인하세요."
    bq = int(broker_position.get("poss_qty") or broker_position.get("qty") or 0)
    gap = bq - state.holdings
    if gap == 0:
        return 0, (f"장부 {state.holdings}주 = 증권사 {bq}주 — 수량은 이미 맞습니다.\n"
                   f"정지 직후라면 대조에서 자동 교정된 상태입니다. "
                   f"남은 문제는 T값일 가능성이 높습니다.")
    return gap, (f"장부 {state.holdings}주 / 증권사 {bq}주 — "
                 f"{abs(gap)}주 {'부족' if gap > 0 else '초과'}")


def preview_text(state, draft: FixDraft, fee_rate: float) -> str:
    """적용 전후 미리보기. 확인 버튼을 누르기 전에 보여준다."""
    from config.fees import calculate_fee, policy_from_rate

    policy = policy_from_rate(fee_rate)
    amount = draft.qty * draft.price
    fee = calculate_fee(amount, draft.qty, is_sell=(draft.side == "sell"), policy=policy)

    if draft.side == "buy":
        new_h = state.holdings + draft.qty
        new_avg = (state.avg_price * state.holdings + amount) / new_h if new_h else 0.0
        new_cash = state.cash - amount - fee
    else:
        new_h = max(0, state.holdings - draft.qty)
        new_avg = 0.0 if new_h == 0 else state.avg_price
        new_cash = state.cash + amount - fee

    lines = [
        f"보정 미리보기 — {draft.summary()}",
        "",
        f"  보유   {state.holdings}주 → {new_h}주",
        f"  평단   ${state.avg_price:.4f} → ${new_avg:.4f}",
        f"  잔금   ${state.cash:,.2f} → ${new_cash:,.2f}",
        f"  수수료 ${fee:.2f}",
        "",
        "  T값은 바뀌지 않습니다.",
    ]
    if draft.side == "sell" and draft.qty > state.holdings:
        lines += ["", f"  ⚠ 보유 {state.holdings}주보다 많습니다. 적용되지 않습니다."]
    return "\n".join(lines)


# ================================================================
# 콜백 데이터 (telegram callback_data 는 64바이트 제한)
# ================================================================

def cb(*parts) -> str:
    return ":".join([CB, *[str(p) for p in parts]])


def parse_cb(data: str) -> list[str]:
    return data.split(":")[1:] if data.startswith(CB + ":") else []


def keyboard_tickers(tickers: list[str]):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = [[InlineKeyboardButton(t, callback_data=cb("t", t))] for t in tickers]
    rows.append([InlineKeyboardButton("취소", callback_data=cb("x"))])
    return InlineKeyboardMarkup(rows)


def keyboard_side(gap: int = 0):
    """보정 종류 선택.

    회로차단기가 정지할 때 이미 수량·평단을 증권사 기준으로 교정한다.
    그래서 정지 직후에는 수량 차이가 0이고, 남는 문제는 T값이다.
    어떤 체결을 놓쳤는지 모르니 T만 틀어진 채로 남기 때문이다.
    차이가 없으면 T 조정을 먼저 보여준다.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    trade = [InlineKeyboardButton("매수 추가", callback_data=cb("s", "buy")),
             InlineKeyboardButton("매도 추가", callback_data=cb("s", "sell"))]
    t_btn = [InlineKeyboardButton("T값 조정", callback_data=cb("s", "setT"))]
    rows = [t_btn, trade] if gap == 0 else [trade, t_btn]
    rows.append([InlineKeyboardButton("취소", callback_data=cb("x"))])
    return InlineKeyboardMarkup(rows)


def keyboard_t(current: float, derived: Optional[float]):
    """T값 후보. 잔고 역산 T 를 먼저 제시한다."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = []
    if derived is not None and abs(derived - current) > 1e-6:
        rows.append([InlineKeyboardButton(
            f"역산값 {derived:.4f} 로 설정", callback_data=cb("T", f"{derived:.4f}"))])
    rows.append([InlineKeyboardButton("직접 입력", callback_data=cb("T", "manual"))])
    rows.append([InlineKeyboardButton("취소", callback_data=cb("x"))])
    return InlineKeyboardMarkup(rows)


def t_preview_text(state, new_T: float, derived: Optional[float]) -> str:
    """T 조정 미리보기.

    T 는 1회매수액(잔금÷(분할수−T))과 별지점(별%가 T로 결정됨)을
    동시에 움직이므로, 바뀐 뒤 무엇이 달라지는지 같이 보여준다.
    """
    from core.star_point import normal_star_point, star_pct

    L = [f"T값 조정 미리보기 — {state.ticker}", "",
         f"  T값   {state.T:.4f} → {new_T:.4f}"]
    if derived is not None:
        L.append(f"  (잔고 역산 T = {derived:.4f})")

    for label, t in (("현재", state.T), ("변경", new_T)):
        denom = state.division - t
        unit = state.cash / denom if denom > 0 else 0.0
        try:
            pct = star_pct(state.ticker, state.division, t)
            star = (normal_star_point(state.ticker, state.division, state.avg_price, t).star
                    if state.avg_price > 0 else 0.0)
            L.append(f"  {label}: 1회매수액 ${unit:,.2f} · 별% {pct:+.3f}% · 별지점 {star:.2f}")
        except ValueError:
            L.append(f"  {label}: 1회매수액 ${unit:,.2f}")

    if new_T > state.division - 1:
        L += ["", "  ⚠ 소진 기준을 넘습니다. 다음 거래일 리버스모드로 전환됩니다."]
    return "\n".join(L)


def keyboard_qty(options: list[tuple[int, str]]):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows, row = [], []
    for q, label in options:
        row.append(InlineKeyboardButton(label, callback_data=cb("q", q)))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("직접 입력", callback_data=cb("q", "manual"))])
    rows.append([InlineKeyboardButton("취소", callback_data=cb("x"))])
    return InlineKeyboardMarkup(rows)


def keyboard_confirm():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("적용", callback_data=cb("ok")),
         InlineKeyboardButton("취소", callback_data=cb("x"))],
    ])
