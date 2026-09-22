"""
================================================================
장부 대조 · 회로차단기 테스트

봇이 어긋난 상태를 스스로 알아차리고 멈추는지 검증한다.
이 동작이 깨지면 잘못된 평단 기준으로 매일 주문이 나간다.

실행:  python tests/test_reconciler.py
================================================================
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.reconciler import (
    AVG_PRICE_CRITICAL,
    MAX_EOD_FAILURES,
    CircuitBreaker,
    Severity,
    apply_reconcile,
    auto_correct,
    derive_t,
    is_within_auto_correct,
    reconcile,
)
from modes.base_mode import PositionState


def _state(**kw) -> PositionState:
    base = dict(ticker="TQQQ", division=40, principal=20000.0, fee_rate=0.0007,
                T=8.0, avg_price=69.75, holdings=60, cash=5000.0)
    base.update(kw)
    return PositionState(**base)


def _broker(qty=60, avg=69.75) -> dict:
    return {"poss_qty": qty, "qty": qty, "avg_price": avg}


# ================================================================
# 일치
# ================================================================

def test_matching_ledger_is_ok():
    r = reconcile(_state(), _broker(), deposit_usd=5000.0)
    assert r.severity == Severity.OK
    assert r.should_halt is False
    assert "일치" in r.report()


def test_small_avg_drift_ignored():
    """증권사가 수수료를 매입단가에 포함시키면 소수 차이가 난다"""
    r = reconcile(_state(avg_price=69.75), _broker(avg=69.78), deposit_usd=5000.0)
    assert r.severity == Severity.OK


# ================================================================
# 치명 — 정지해야 하는 것
# ================================================================

def test_holdings_mismatch_halts():
    """체결 누락이나 수동 거래가 있으면 보유수량이 어긋난다"""
    r = reconcile(_state(holdings=60), _broker(qty=57), deposit_usd=5000.0)
    assert r.severity == Severity.CRITICAL
    assert r.should_halt is True
    assert "보유수량 불일치" in r.halt_reason


def test_large_avg_drift_halts():
    """평단이 5% 이상 어긋나면 별지점과 목표매도가가 전부 틀어진다.

    오탐이 나면 /unhalt 한 번으로 풀 수 있지만, 어긋난 평단으로
    며칠 매매하면 복구가 어렵다. 보수적으로 잡는다."""
    r = reconcile(_state(avg_price=69.75), _broker(avg=80.0), deposit_usd=5000.0)
    assert r.severity == Severity.CRITICAL
    assert abs(80.0 - 69.75) / 69.75 >= AVG_PRICE_CRITICAL


def test_insufficient_funds_warns_but_does_not_halt():
    """원금을 장부상 금액으로 두고 실제 달러는 매일 채우는 운용에서
    부족은 '아직 입금 전' 이다. 정지를 걸면 입금해도 풀리지 않아
    그날 주문이 통째로 빠지고 매도까지 막힌다."""
    r = reconcile(_state(), _broker(), deposit_usd=100.0, required_cash=800.0)
    assert r.should_halt is False
    assert r.severity == Severity.WARNING
    assert any(f.code == "funding_shortfall" for f in r.findings)
    assert any("700.00" in f.message for f in r.findings)   # 입금 필요액


def test_ledger_cash_bigger_than_deposit_is_normal():
    """매일 채우는 방식에서는 장부 잔금이 항상 실제 예수금보다 크다"""
    r = reconcile(_state(cash=20000.0), _broker(), deposit_usd=900.0, required_cash=500.0)
    assert r.severity == Severity.OK


# ================================================================
# 경고 — 알리되 멈추지는 않는 것
# ================================================================

def test_moderate_avg_drift_warns_only():
    r = reconcile(_state(avg_price=69.75), _broker(avg=71.0), deposit_usd=5000.0)
    assert r.severity == Severity.WARNING
    assert r.should_halt is False


def test_missing_broker_data_warns():
    """잔고 조회 실패로 대조를 못 했으면 알리되 멈추지는 않는다"""
    r = reconcile(_state(), None, deposit_usd=5000.0)
    assert r.severity == Severity.WARNING
    assert r.should_halt is False


def test_no_holdings_skips_avg_check():
    """보유 0이면 평단 비교가 의미 없다"""
    r = reconcile(_state(holdings=0, avg_price=0.0), _broker(qty=0, avg=0.0))
    assert r.severity == Severity.OK


# ================================================================
# 회로차단기
# ================================================================

def test_halt_and_release():
    st = _state()
    assert CircuitBreaker.is_halted(st) is False
    assert CircuitBreaker.halt(st, "보유수량 불일치") is True
    assert CircuitBreaker.is_halted(st) is True
    assert st.halt_reason == "보유수량 불일치"
    assert st.halted_at != ""

    assert CircuitBreaker.release(st) is True
    assert CircuitBreaker.is_halted(st) is False
    assert st.halt_reason == ""


def test_halt_is_idempotent():
    """이미 정지 상태면 사유를 덮어쓰지 않는다 (최초 원인 보존)"""
    st = _state()
    CircuitBreaker.halt(st, "첫 번째 원인")
    assert CircuitBreaker.halt(st, "두 번째") is False
    assert st.halt_reason == "첫 번째 원인"


def test_release_when_not_halted():
    assert CircuitBreaker.release(_state()) is False


def test_halt_does_not_auto_clear():
    """다음 대조가 정상이어도 자동으로 풀리면 안 된다.
    원인이 남은 채 주문이 다시 나가기 때문이다."""
    st = _state()
    CircuitBreaker.halt(st, "보유수량 불일치")
    apply_reconcile(st, reconcile(st, _broker(), deposit_usd=5000.0))
    assert CircuitBreaker.is_halted(st) is True


def test_apply_reconcile_halts_on_critical():
    st = _state(holdings=60)
    assert apply_reconcile(st, reconcile(st, _broker(qty=50))) is True
    assert CircuitBreaker.is_halted(st) is True


def test_apply_reconcile_ignores_warning():
    st = _state()
    assert apply_reconcile(st, reconcile(st, _broker(avg=71.0))) is False
    assert CircuitBreaker.is_halted(st) is False


# ================================================================
# EOD 실패 누적
# ================================================================

def test_eod_failure_halts_after_threshold():
    """EOD 가 실패하면 T값이 갱신되지 않은 채 다음날 주문이 나간다"""
    st = _state()
    for i in range(MAX_EOD_FAILURES - 1):
        assert CircuitBreaker.record_eod_failure(st, "네트워크") is False
        assert CircuitBreaker.is_halted(st) is False
    assert CircuitBreaker.record_eod_failure(st, "네트워크") is True
    assert CircuitBreaker.is_halted(st) is True
    assert "EOD" in st.halt_reason


def test_eod_success_resets_counter():
    st = _state()
    CircuitBreaker.record_eod_failure(st, "일시 오류")
    assert st.eod_failures == 1
    CircuitBreaker.record_eod_success(st)
    assert st.eod_failures == 0


# ================================================================
# 영속화
# ================================================================

def test_halt_survives_save_and_restart():
    """프로세스가 재시작돼도 정지 상태가 유지돼야 한다"""
    st = _state()
    CircuitBreaker.halt(st, "보유수량 불일치")
    restored = PositionState.from_dict(st.to_dict())
    assert CircuitBreaker.is_halted(restored) is True
    assert restored.halt_reason == "보유수량 불일치"
    assert restored.halted_at == st.halted_at


def test_status_line():
    st = _state()
    assert CircuitBreaker.status_line(st) == ""
    CircuitBreaker.halt(st, "보유수량 불일치")
    line = CircuitBreaker.status_line(st)
    assert "정지" in line and "unhalt TQQQ" in line


# ================================================================
# 스케줄러 연동
# ================================================================

def test_scheduler_blocks_orders_when_halted():
    src = (Path(__file__).resolve().parent.parent / "scheduler" / "engine.py").read_text()
    assert "CircuitBreaker.is_halted(state)" in src
    # 주문 생성보다 앞서 검사해야 한다
    assert src.index("CircuitBreaker.is_halted(state)") < src.index("mode.plan(market)")


def test_scheduler_reconciles_after_eod():
    src = (Path(__file__).resolve().parent.parent / "scheduler" / "engine.py").read_text()
    assert "_reconcile_ticker" in src
    assert "record_eod_failure" in src


def test_telegram_has_halt_commands():
    src = (Path(__file__).resolve().parent.parent / "tg_bot" / "bot.py").read_text()
    assert 'CommandHandler("halt"' in src
    assert 'CommandHandler("unhalt"' in src


# ================================================================
# 자동 교정 (비파괴)
# ================================================================

def test_small_diff_auto_corrects_without_halt():
    """부분체결 반올림 수준의 차이는 조용히 맞추고 넘어간다"""
    st = _state(holdings=60)
    r = reconcile(st, _broker(qty=59, avg=69.75), deposit_usd=5000.0)
    assert r.should_halt is False
    apply_reconcile(st, r, _broker(qty=59, avg=69.75))
    assert st.holdings == 59
    assert len(r.corrections) == 1
    assert r.corrections[0].field == "보유수량"


def test_large_diff_corrects_and_halts():
    """크게 어긋나면 장부는 맞추되 주문은 멈춘다.

    장부만 맞추고 계속 매매하면 T 가 틀린 채로 주문이 나가고,
    멈추기만 하면 사람이 일일이 고쳐야 한다. 둘 다 해야 한다."""
    st = _state(holdings=60)
    broker = _broker(qty=39, avg=69.75)      # 21주 차이 (화면1 사례)
    r = reconcile(st, broker, deposit_usd=5000.0)
    assert r.should_halt is True
    assert apply_reconcile(st, r, broker) is True
    assert st.holdings == 39                  # 장부는 맞춰졌다
    assert CircuitBreaker.is_halted(st) is True


def test_auto_correct_does_not_touch_t():
    """T 는 체결 순서·종류로 결정되므로 잔고만으로 복원할 수 없다"""
    st = _state(holdings=60, T=8.0)
    auto_correct(st, _broker(qty=57, avg=70.0))
    assert st.holdings == 57
    assert st.T == 8.0


def test_auto_correct_closes_cycle_when_zero():
    st = _state(holdings=60, T=8.0, avg_price=69.75)
    auto_correct(st, _broker(qty=0, avg=0.0))
    assert st.holdings == 0
    assert st.T == 0.0
    assert st.avg_price == 0.0


def test_corrections_are_recorded_not_silent():
    """비파괴 — 무엇을 어떻게 고쳤는지 남는다"""
    st = _state(holdings=60, avg_price=69.75)
    cs = auto_correct(st, _broker(qty=57, avg=70.10))
    assert len(cs) == 2
    fields = {c.field for c in cs}
    assert fields == {"보유수량", "평단"}
    assert cs[0].before == 60 and cs[0].after == 57


def test_auto_correct_tolerance():
    assert is_within_auto_correct(60, 60) is True
    assert is_within_auto_correct(60, 58) is True      # 2주
    assert is_within_auto_correct(60, 57) is False     # 3주 > max(2, 1.8)
    assert is_within_auto_correct(200, 195) is True    # 5주 ≤ 6주(3%)
    assert is_within_auto_correct(200, 190) is False


# ================================================================
# T 역산 교차검증
# ================================================================

def test_derive_t_matches_screenshot_examples():
    """실제 운영 중인 다른 봇의 화면 수치로 검산.

    T = (보유수량 × 평단) / (원금 / 분할수)
    """
    assert abs(derive_t(66, 117.11, 20000, 20) - 7.7290) < 0.001
    assert abs(derive_t(60, 120.12, 20000, 20) - 7.2074) < 0.001


def test_derive_t_follows_methodology_rules():
    """원가 기준으로 보면 라오어 규칙과 일치한다"""
    base = derive_t(80, 100.0, 20000, 20)            # 원가 8000 → T=8
    assert abs(base - 8.0) < 1e-9
    # 쿼터매도: 1/4 매도 → 원가 ×0.75
    assert abs(derive_t(60, 100.0, 20000, 20) - base * 0.75) < 1e-9
    # 3/4 지정가매도 → 원가 ×0.25
    assert abs(derive_t(20, 100.0, 20000, 20) - base * 0.25) < 1e-9


def test_derive_t_zero_holdings():
    assert derive_t(0, 0.0, 20000, 20) == 0.0


def test_t_drift_warns_not_halts():
    """T 괴리는 경고만 한다. 폭락대비 매수가 많으면 정상적으로 벌어진다."""
    # 20분할, 원금 2만 → 1회매수금 $1,000. 원가 12,000 → 역산 T=12
    st = _state(division=20, holdings=60, avg_price=200.0, T=2.0)
    r = reconcile(st, _broker(qty=60, avg=200.0), deposit_usd=5000.0)
    assert r.derived_T is not None
    assert any(f.code == "t_drift" for f in r.findings)
    assert r.should_halt is False


def test_t_drift_not_reported_when_close():
    # 60주 × $120 = $7,200 / $1,000 = 7.2 → 장부와 일치
    st = _state(division=20, holdings=60, avg_price=120.0, T=7.2)
    r = reconcile(st, _broker(qty=60, avg=120.0), deposit_usd=5000.0)
    assert not any(f.code == "t_drift" for f in r.findings)


# ================================================================

if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:
                failed += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print("-" * 60)
    print("전부 통과" if failed == 0 else f"{failed}건 실패")
    sys.exit(1 if failed else 0)
