"""
================================================================
T값 글자 그래프 — 텔레그램 메시지 안에서 바로 보이는 그림

  진행 막대   40분할 중 어디쯤인지. 가운데 ┃ 가 전반·후반 경계
  T 흐름      이번 사이클 동안의 T 변화. 쿼터매도로 줄어든 날은 움푹 들어간다

이미지 대신 글자로 그리는 이유: 따로 파일을 보내지 않아도 되고, 폰
알림 미리보기에서도 보이며, 서버에 그래프 라이브러리가 필요 없다.

T 기록은 매일 정산 때 data/archive/YYYYMM.jsonl 에 쌓이는 값을 쓴다.
================================================================
"""

from __future__ import annotations

from datetime import datetime, timedelta

FILL, EMPTY, HALF_MARK = "▰", "▱", "┃"
SPARK = "▁▂▃▄▅▆▇█"


def phase_of(T: float, division: int, mode: str) -> str:
    if mode == "reverse":
        return "리버스"
    if T > division - 1:
        return "소진"
    if T >= division / 2:
        return "후반전"
    return "전반전"


def progress_bar(T: float, division: int, mode: str = "normal", width: int = 20) -> str:
    """▰▰▰▰▱▱▱▱▱▱┃▱▱▱▱▱▱▱▱▱▱ 18% · 전반전 (후반까지 13.0)"""
    T = max(0.0, float(T))
    ratio = min(T / division, 1.0) if division else 0.0
    filled = round(ratio * width)
    if T > 0 and filled == 0:
        filled = 1                      # 시작했으면 한 칸은 보이게
    cells = [FILL if i < filled else EMPTY for i in range(width)]
    half = width // 2
    bar = "".join(cells[:half]) + HALF_MARK + "".join(cells[half:])

    phase = phase_of(T, division, mode)
    tail = f"{ratio * 100:.0f}% · {phase}"
    if phase == "전반전":
        tail += f" (후반까지 {division / 2 - T:.1f})"
    elif phase == "후반전":
        tail += f" (소진까지 {max(division - 1 - T, 0):.1f})"
    return f"{bar} {tail}"


def sparkline(values: list[float]) -> str:
    """값의 상대적인 오르내림을 ▁▂▃▄▅▆▇█ 로 그린다"""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return SPARK[len(SPARK) // 2] * len(values)
    top = len(SPARK) - 1
    return "".join(SPARK[round((v - lo) / (hi - lo) * top)] for v in values)


def _months_back(n: int) -> list[str]:
    d = datetime.now().replace(day=1)
    out = []
    for _ in range(n):
        out.append(d.strftime("%Y%m"))
        d = (d - timedelta(days=1)).replace(day=1)
    return list(reversed(out))


def cycle_t_history(state_mgr, ticker: str, max_points: int = 20,
                    months: int = 3) -> list[tuple[str, float]]:
    """이번 사이클의 (거래일, T) 기록. 오래된 것부터.

    아카이브를 거꾸로 훑다가 보유 0 인 날(직전 사이클 종료 또는 시작 전)을
    만나면 거기서 끊는다. 같은 날이 두 번 기록됐으면 나중 것을 쓴다.
    """
    by_day: dict[str, dict] = {}
    for ym in _months_back(months):
        for rec in state_mgr.read_archive(ym, ticker):
            day = str(rec.get("trade_date", ""))
            if day:
                by_day[day] = rec

    hist: list[tuple[str, float]] = []
    for day in sorted(by_day, reverse=True):
        rec = by_day[day]
        if int(rec.get("holdings", 0) or 0) <= 0:
            break
        hist.append((day, float(rec.get("T", 0.0) or 0.0)))
    hist.reverse()
    return hist[-max_points:]


def t_flow_line(hist: list[tuple[str, float]]) -> str:
    """T 흐름 ▁▂▃▅▆█  (9/22 1.0 → 10/02 7.0)"""
    if len(hist) < 2:
        return ""
    (d0, t0), (d1, t1) = hist[0], hist[-1]

    def md(d: str) -> str:
        return f"{int(d[4:6])}/{int(d[6:8])}"

    return f"T 흐름 {sparkline([t for _, t in hist])}  ({md(d0)} {t0:.1f} → {md(d1)} {t1:.1f})"


def t_chart_lines(state_mgr, ticker: str, state) -> list[str]:
    """아침 요약·관제탑에 넣을 두 줄"""
    lines = [progress_bar(state.T, state.division, getattr(state, "mode", "normal"))]
    try:
        flow = t_flow_line(cycle_t_history(state_mgr, ticker))
    except Exception:
        flow = ""
    if flow:
        lines.append(flow)
    return lines
