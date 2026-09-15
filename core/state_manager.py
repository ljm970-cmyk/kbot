"""
================================================================
상태 저장소

종목별 설정(TickerConfig)과 실행 상태(PositionState)를 JSON 파일로
영속화한다.

기존 구현의 문제 두 가지를 고쳤다.

  1. 경로 하드코딩
     /opt/kbot/data 를 상수로 박아둬서 ~/kbot 배포에서 권한 오류로 죽었다.
     이제 생성자에서 경로를 받는다 (config.paths.data).

  2. 동시 쓰기
     EOD 계산과 텔레그램 명령이 같은 state.json 을 동시에 쓰면 한쪽이
     통째로 날아간다. 임시파일 → fsync → os.replace 로 원자적 교체하고,
     별도 락 파일로 읽기-수정-쓰기 구간을 보호한다.
================================================================
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from modes.base_mode import PositionState

logger = logging.getLogger("kbot.state")

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:      # Windows
    _HAS_FCNTL = False


# ================================================================
# 원자적 파일 IO
# ================================================================

def atomic_write_json(path: Path, data: dict) -> None:
    """임시파일에 쓰고 fsync 후 교체.

    중간에 프로세스가 죽어도 기존 파일이 남거나 새 파일이 온전히 남는다.
    반쯤 쓰인 JSON 이 남는 경우는 없다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextmanager
def file_lock(lock_path: Path) -> Iterator[None]:
    """읽기-수정-쓰기 구간 보호.

    fcntl 이 없는 환경(Windows)에서는 락 없이 통과한다.
    """
    if not _HAS_FCNTL:
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        logger.error("상태 파일이 손상되었습니다: %s (%s)", path, e)
        return None


# ================================================================
# 저장소
# ================================================================

class StateManager:
    """종목별 설정 · 상태 저장소"""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        for sub in ("config", "state", "orders", "fills", "archive", "locks"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------
    # 경로
    # ------------------------------------------------------------

    def _config_path(self, ticker: str) -> Path:
        return self.data_dir / "config" / f"{ticker.upper()}.json"

    def _state_path(self, ticker: str) -> Path:
        return self.data_dir / "state" / f"{ticker.upper()}.json"

    def _lock_path(self, ticker: str) -> Path:
        return self.data_dir / "locks" / f"{ticker.upper()}.lock"

    # ------------------------------------------------------------
    # 설정
    # ------------------------------------------------------------

    def save_config(self, ticker: str, config: dict) -> None:
        """종목 설정 저장 (원금·분할수·수수료율 등)"""
        ticker = ticker.upper()
        payload = dict(config)
        payload["ticker"] = ticker
        payload.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
        payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
        atomic_write_json(self._config_path(ticker), payload)

    def get_config(self, ticker: str) -> Optional[dict]:
        return read_json(self._config_path(ticker))

    def list_tickers(self, active_only: bool = True) -> list[str]:
        out = []
        for p in sorted((self.data_dir / "config").glob("*.json")):
            cfg = read_json(p)
            if not cfg:
                continue
            if active_only and not cfg.get("is_active", True):
                continue
            out.append(cfg.get("ticker", p.stem))
        return out

    def deactivate(self, ticker: str) -> None:
        cfg = self.get_config(ticker) or {}
        cfg["is_active"] = False
        self.save_config(ticker, cfg)

    # ------------------------------------------------------------
    # 상태
    # ------------------------------------------------------------

    def get_state(self, *args: str) -> Optional[PositionState]:
        """현재 상태. 없으면 설정 기반으로 초기 상태를 만든다.

        두 형태를 모두 받는다.
            get_state("TQQQ")                신규
            get_state(user_id, "TQQQ")       구버전 (user_id 는 무시)

        돌려주는 PositionState 는 state["T"] 같은 dict 접근도 지원한다.
        """
        if not args:
            raise TypeError("ticker 가 필요합니다")
        ticker = str(args[-1]).upper()
        raw = read_json(self._state_path(ticker))
        if raw:
            return PositionState.from_dict(raw)

        cfg = self.get_config(ticker)
        if not cfg:
            return None

        state = PositionState(
            ticker=ticker,
            division=int(cfg["division"]),
            principal=float(cfg["principal"]),
            fee_rate=float(cfg.get("fee_rate", 0.0007)),
            cash=float(cfg["principal"]),
        )
        self.save_state(state)
        return state

    def save_state(self, *args) -> None:
        """상태 저장.

            save_state(position_state)               신규
            save_state(user_id, ticker, state_dict)  구버전
        """
        if len(args) == 1:
            state = args[0]
        elif len(args) == 3:
            raw = dict(args[2])
            raw.setdefault("ticker", args[1])
            state = PositionState.from_dict(raw)
        else:
            raise TypeError("save_state(state) 또는 save_state(user_id, ticker, dict)")

        payload = state.to_dict()
        payload["saved_at"] = datetime.now().isoformat(timespec="seconds")
        atomic_write_json(self._state_path(state.ticker), payload)

    @contextmanager
    def edit_state(self, ticker: str) -> Iterator[PositionState]:
        """읽기-수정-쓰기를 락으로 감싼다.

        EOD 계산과 텔레그램 명령이 겹쳐도 한쪽 변경이 사라지지 않는다.

            with state_mgr.edit_state("TQQQ") as st:
                st.cash += 100
            # 블록을 빠져나올 때 저장된다
        """
        with file_lock(self._lock_path(ticker)):
            state = self.get_state(ticker)
            if state is None:
                raise KeyError(f"{ticker} 설정이 없습니다. 먼저 save_config 를 호출하세요.")
            yield state
            self.save_state(state)

    def reset_state(self, *args: str) -> Optional[PositionState]:
        """상태를 설정 기준으로 초기화 (수동 복구용).

        주의: T값·평단·보유가 모두 날아간다.
        """
        ticker = str(args[-1])
        self._state_path(ticker).unlink(missing_ok=True)
        return self.get_state(ticker)

    # ------------------------------------------------------------
    # 아카이브
    # ------------------------------------------------------------

    def archive_eod(self, ticker: str, trade_date: str, record: dict) -> None:
        """일별 정산 결과를 append-only 로 남긴다.

        상태 파일이 손상됐을 때 여기서 재구성할 수 있다.
        """
        path = self.data_dir / "archive" / f"{trade_date[:6]}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "ticker": ticker.upper(),
            "trade_date": trade_date,
            "archived_at": datetime.now().isoformat(timespec="seconds"),
            **record,
        }, ensure_ascii=False, default=str)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    def read_archive(self, year_month: str, ticker: str = "") -> list[dict]:
        path = self.data_dir / "archive" / f"{year_month}.jsonl"
        if not path.exists():
            return []
        rows = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ticker and rec.get("ticker") != ticker.upper():
                    continue
                rows.append(rec)
        return rows

    # ============================================================
    # 구버전 호환 (tg_bot)
    #
    # 기존 텔레그램 코드는 get_state(user_id, ticker) 처럼 사용자별
    # 상태를 전제로 짜여 있다. 하지만 이 봇은 키움 계좌 하나로 매매하므로
    # (앱키가 하나다) 사용자를 나누는 것은 의미가 없다. 여러 사용자가
    # 있어도 결국 같은 계좌를 건드리게 된다.
    #
    # 그래서 단일 계좌 모델을 유지하되, user_id 를 받아서 무시하는
    # 호환 메서드를 둔다. 텔레그램 코드 36KB 를 다시 쓰지 않아도 된다.
    # 접근 권한은 TELEGRAM_ADMIN_ID 로 통제한다.
    # ============================================================

    def save_ticker_config(self, user_id: str, ticker: str, config: dict) -> None:
        """구버전 호환 → save_config(ticker, config)"""
        self.save_config(ticker, config)

    def get_ticker_config(self, user_id: str, ticker: str) -> Optional[dict]:
        """구버전 호환 → get_config(ticker)"""
        return self.get_config(ticker)

    def get_user_tickers(self, user_id: str) -> list[str]:
        """구버전 호환 → list_tickers()"""
        return self.list_tickers()

    def get_principal(self, user_id: str, ticker: str) -> float:
        cfg = self.get_config(ticker)
        return float(cfg.get("principal", 0.0)) if cfg else 0.0

    def deactivate_ticker(self, user_id: str, ticker: str) -> None:
        """구버전 호환 → deactivate(ticker)"""
        self.deactivate(ticker)

    def apply_config(self, ticker: str) -> dict:
        """설정(분할수·원금·수수료율)을 실행 상태에 반영한다.

        get_state() 는 상태 파일이 있으면 그대로 돌려주므로, 설정만
        저장하면 분할수·원금 변경이 조용히 무시된다. 마법사가 "설정 완료"
        라고 알려도 실제로는 아무것도 바뀌지 않는 문제가 있었다.

        진행 중 사이클(보유 > 0)에서 분할수를 바꾸면 T 의 의미가 달라진다.
        20분할의 T=8 과 40분할의 T=8 은 전혀 다른 상태다. 그래서 보유가
        있으면 분할수 변경을 거부한다.

        Returns:
            {'ok': bool, 'changed': [...], 'blocked': [...], 'error': str}
        """
        ticker = ticker.upper()
        cfg = self.get_config(ticker)
        if not cfg:
            return {"ok": False, "error": f"{ticker} 설정이 없습니다.", "changed": [], "blocked": []}

        changed, blocked = [], []
        with file_lock(self._lock_path(ticker)):
            state = self.get_state(ticker)
            if state is None:
                return {"ok": False, "error": "상태를 만들 수 없습니다.",
                        "changed": [], "blocked": []}

            new_div = int(cfg.get("division", state.division))
            if new_div != state.division:
                if state.holdings > 0:
                    blocked.append(
                        f"분할수 {state.division} → {new_div}: 보유 {state.holdings}주가 "
                        f"있어 변경할 수 없습니다. T={state.T:.4f} 의 의미가 달라집니다. "
                        f"사이클이 끝난 뒤 변경하세요.")
                else:
                    changed.append(f"분할수 {state.division} → {new_div}")
                    state.division = new_div

            new_principal = float(cfg.get("principal", state.principal))
            if abs(new_principal - state.principal) > 1e-9:
                changed.append(f"원금 ${state.principal:,.0f} → ${new_principal:,.0f}")
                # 진행 중이면 잔금은 건드리지 않는다. 다음 사이클부터 적용된다.
                if state.holdings == 0:
                    state.cash = new_principal
                    changed.append(f"잔금을 ${new_principal:,.0f} 로 재설정")
                else:
                    blocked.append(
                        f"보유 {state.holdings}주가 있어 잔금은 유지합니다. "
                        f"원금 변경은 다음 사이클부터 반영됩니다.")
                state.principal = new_principal

            new_fee = float(cfg.get("fee_rate", state.fee_rate))
            if abs(new_fee - state.fee_rate) > 1e-12:
                changed.append(f"수수료율 {state.fee_rate:.4%} → {new_fee:.4%}")
                state.fee_rate = new_fee

            if changed:
                self.save_state(state)

        return {"ok": True, "changed": changed, "blocked": blocked, "error": ""}

    def add_manual_correction(self, user_id: str, ticker: str, correction: dict) -> dict:
        """수동 거래 보정을 '즉시' 장부에 반영한다.

        기록만 남기고 끝내면 안 된다. 정지가 걸렸을 때 사람이 쓸 유일한
        복구 수단이므로, 실제로 보유수량·평단·잔금이 바뀌어야 한다.

        Args:
            correction: {'qty': int, 'price': float, 'side': 'buy'|'sell', 'date': str}
        Returns:
            {'ok': bool, 'before': {...}, 'after': {...}, 'error': str}
        """
        from config.fees import apply_buy, apply_sell, calculate_fee, policy_from_rate

        ticker = ticker.upper()
        qty = int(correction.get("qty") or 0)
        price = float(correction.get("price") or 0)
        side = str(correction.get("side", "buy")).lower()

        if qty <= 0 or price <= 0 or side not in ("buy", "sell"):
            return {"ok": False, "error": "수량·가격·구분을 확인하세요."}

        with file_lock(self._lock_path(ticker)):
            state = self.get_state(ticker)
            if state is None:
                return {"ok": False, "error": f"{ticker} 설정이 없습니다."}

            before = {"holdings": state.holdings, "avg_price": state.avg_price,
                      "cash": state.cash}

            if side == "sell" and qty > state.holdings:
                return {"ok": False,
                        "error": f"보유 {state.holdings}주보다 많은 {qty}주 매도는 반영할 수 없습니다."}

            policy = policy_from_rate(state.fee_rate)
            amount = qty * price
            fee = calculate_fee(amount, qty, is_sell=(side == "sell"), policy=policy)

            if side == "buy":
                prev_value = state.avg_price * state.holdings
                state.holdings += qty
                state.avg_price = (prev_value + amount) / state.holdings
                state.cash = apply_buy(state.cash, amount, fee)
            else:
                state.holdings -= qty
                state.cash = apply_sell(state.cash, amount, fee)
                if state.holdings == 0:
                    state.avg_price = 0.0
                    state.T = 0.0

            after = {"holdings": state.holdings, "avg_price": state.avg_price,
                     "cash": state.cash}
            self.save_state(state)

        self.record_correction(ticker, {**correction, "source": "manual_fix",
                                        "fee": fee, "before": before, "after": after})
        return {"ok": True, "before": before, "after": after, "fee": fee}

    # ------------------------------------------------------------
    # 수동 보정
    # ------------------------------------------------------------

    def record_correction(self, ticker: str, correction: dict) -> None:
        """수동 거래 보정 이력.

        상태를 직접 고칠 때(누락 체결 반영 등) 무엇을 왜 바꿨는지 남긴다.
        """
        path = self.data_dir / "archive" / f"corrections_{ticker.upper()}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                **correction,
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
            }, ensure_ascii=False, default=str) + "\n")

    def corrections(self, ticker: str) -> list[dict]:
        path = self.data_dir / "archive" / f"corrections_{ticker.upper()}.jsonl"
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
