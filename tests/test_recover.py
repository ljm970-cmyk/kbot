"""
================================================================
거부된 핵심 매수 되살리기

프리장에서 증권사가 거부한 별지점·평단 LOC 매수를 정규장에서
  1차 같은 가격 재접수 → 2차 현재가×1.15 대체 → 3차 알림
(실제 2026-11-03 TQQQ 3주 @72.55: 18:02·19:43 거부, 00:40 수동 접수 성공)

실행:  python tests/test_recover.py
================================================================
"""

import asyncio
import sys
import tempfile
import types
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules.setdefault(name, m)
    return sys.modules[name]


for mod, attrs in [("aiohttp", {"ClientSession": object,
                                "ClientTimeout": lambda **k: None,
                                "ClientError": Exception}),
                   ("websockets", {"connect": None})]:
    try:
        __import__(mod)
    except ImportError:
        _stub(mod, **attrs)
try:
    import apscheduler  # noqa: F401
except ImportError:
    for p in ["apscheduler", "apscheduler.schedulers", "apscheduler.triggers"]:
        _stub(p)
    _stub("apscheduler.schedulers.asyncio", AsyncIOScheduler=object)
    _stub("apscheduler.triggers.cron", CronTrigger=object)
    _stub("apscheduler.triggers.date", DateTrigger=object)

from core.funding import (  # noqa: E402
    FundingCheck,
    allocate_buys,
    core_buy_need,
    order_cost,
)
from core.order_registry import OrderRegistry  # noqa: E402
from core.state_manager import StateManager  # noqa: E402
from core.t_calculator import FillKind  # noqa: E402
from kiwoom.constants import TradeType  # noqa: E402
from modes.base_mode import MarketSnapshot, PlannedOrder, PositionState  # noqa: E402
from modes.normal_mode import NormalMode  # noqa: E402

from kiwoom.api_client import KiwoomAPIError  # noqa: E402

FEE = 0.0007
DAY = date(2026, 11, 3)
TD = "20261103"


def _engine(cur=63.0):
    from scheduler.engine import SchedulerEngine
    d = Path(tempfile.mkdtemp())
    sm = StateManager(d / "d")
    sm.save_config("TQQQ", {"ticker": "TQQQ", "division": 40, "principal": 20000.0,
                            "fee_rate": FEE, "is_active": True})
    reg = OrderRegistry(d / "o.db")
    box = {"live": set(), "stat": {}, "placed": [], "reject_sync": 0, "n": 100}
    sent = []

    class K:
        async def get_open_orders(self, t, ex):
            return [{"ord_no": n} for n in box["live"]]

        async def get_today_orders(self, t, ex):
            return [{"ord_no": n, "ord_stat": s} for n, s in box["stat"].items()]

        async def get_quote(self, t, ex):
            return {"cur_price": cur, "prev_close": cur}

        async def buy(self, t, ex, qty, tt, price):
            if box["reject_sync"] > 0:
                box["reject_sync"] -= 1
                raise KiwoomAPIError("ust20000", "주문가 범위 초과", 400)
            box["n"] += 1
            no = f"{box['n']:09d}"
            box["placed"].append((qty, tt, price, no))
            box["live"].add(no)
            box["stat"][no] = "접수"
            return type("O", (), {"ord_no": no})()

    class N:
        async def send(self, t): sent.append(t)

    eng = SchedulerEngine.__new__(SchedulerEngine)
    eng.state_mgr, eng.registry, eng.kiwoom, eng.notifier = sm, reg, K(), N()
    eng.config = type("C", (), {"dry_run": False})()
    return eng, reg, box, sent


def _place_bot(reg, box, tag=FillKind.STAR_BUY, qty=3, price=72.55, no="000007520"):
    rid = reg.record_submission(TD, "TQQQ", PlannedOrder(
        tag=tag, side="buy", trade_type=TradeType.LOC, qty=qty, price=price, note="별지점"))
    reg.attach_ord_no(rid, no)
    box["stat"][no] = "접수"
    box["live"].add(no)
    return rid


def _broker_reject(box, no):
    box["live"].discard(no)
    box["stat"][no] = "거부"


def _run(eng):
    asyncio.run(eng.recover_rejected(DAY))


def test_step1_same_price_retry():
    eng, reg, box, sent = _engine()
    rid = _place_bot(reg, box)
    _broker_reject(box, "000007520")
    _run(eng)
    assert reg.get(rid).status == "rejected" and "증권사 거부" in reg.get(rid).note
    assert box["placed"] == [(3, TradeType.LOC, 72.55, "000000101")]
    assert any("증권사 거부" in s for s in sent)
    assert any("🔁" in s and "같은 가격" in s for s in sent)
    _run(eng)                                      # 살아 있으면 더 하지 않는다
    assert len(box["placed"]) == 1


def test_step2_guard_after_retry_rejected():
    eng, reg, box, sent = _engine(cur=63.0)
    _place_bot(reg, box)
    _broker_reject(box, "000007520")
    _run(eng)
    _broker_reject(box, "000000101")
    _run(eng)
    qty, tt, price, _ = box["placed"][-1]
    assert price == 72.45                          # 63 × 1.15
    assert qty == int(3 * 72.55 // 72.45)          # 같은 금액 → 3주
    rows = reg.day_orders(TD, "TQQQ")
    assert rows[-1].tag == FillKind.STAR_BUY       # 태그 그대로 (T 계산 보존)
    assert any("🛟" in s and "1.15" in s for s in sent)


def test_step3_give_up_once():
    eng, reg, box, sent = _engine(cur=63.0)
    _place_bot(reg, box)
    _broker_reject(box, "000007520")
    _run(eng)
    _broker_reject(box, "000000101")
    _run(eng)
    _broker_reject(box, "000000102")
    _run(eng)
    _run(eng)
    gave = [s for s in sent if "다시 내도 거부" in s]
    assert len(gave) == 1 and "/today TQQQ" in gave[0]
    assert len(box["placed"]) == 2


def test_sync_rejection_skips_to_guard_same_run():
    eng, reg, box, sent = _engine(cur=63.0)
    _place_bot(reg, box)
    _broker_reject(box, "000007520")
    box["reject_sync"] = 1
    _run(eng)
    assert [p[2] for p in box["placed"]] == [72.45]


def test_not_price_problem_gives_up():
    """현재가 × 1.15 가 원래 가격보다 높으면 가격 문제가 아니다 — 대체하지 않고 알린다"""
    eng, reg, box, sent = _engine(cur=70.0)        # 70 × 1.15 = 80.5 > 72.55
    _place_bot(reg, box)
    _broker_reject(box, "000007520")
    box["reject_sync"] = 1
    _run(eng)
    assert box["placed"] == [] and any("다시 내도 거부" in s for s in sent)


def test_direct_cancel_not_recovered():
    eng, reg, box, sent = _engine()
    rid = _place_bot(reg, box)
    box["live"].discard("000007520")
    box["stat"]["000007520"] = "취소"
    _run(eng)
    assert reg.get(rid).status == "cancelled" and box["placed"] == []


def test_crash_buy_not_recovered():
    eng, reg, box, sent = _engine()
    _place_bot(reg, box, tag=FillKind.CRASH_BUY, qty=1, price=56.42, no="000007600")
    _broker_reject(box, "000007600")
    _run(eng)
    assert box["placed"] == []


def test_sync_cancelled_loc_labels_rejection():
    eng, reg, box, sent = _engine()
    rid = _place_bot(reg, box)
    _broker_reject(box, "000007520")
    asyncio.run(eng._sync_cancelled_loc("TQQQ", TD))
    assert reg.get(rid).status == "rejected"
    assert any("거부 1건" in s for s in sent) and not any("직접 취소" in s for s in sent)


def test_give_up_blocks_later_runs_even_if_price_drops():
    """포기 알림 뒤에는 시세가 바뀌어도 봇이 다시 내지 않는다 (사용자가 직접 넣었을 수 있다)"""
    eng, reg, box, sent = _engine(cur=70.0)
    _place_bot(reg, box)
    _broker_reject(box, "000007520")
    box["reject_sync"] = 1
    _run(eng)                                      # R1 즉시 거부, 70×1.15 > 72.55 → 포기
    assert any("다시 내도 거부" in s for s in sent)

    async def cheap(t, ex): return {"cur_price": 55.0, "prev_close": 55.0}
    eng.kiwoom.get_quote = cheap
    _run(eng)
    _run(eng)
    assert box["placed"] == []
    assert sum("다시 내도 거부" in s for s in sent) == 1


def test_quote_failure_retries_next_run_not_give_up():
    eng, reg, box, sent = _engine(cur=63.0)
    _place_bot(reg, box)
    _broker_reject(box, "000007520")
    box["reject_sync"] = 1
    good = eng.kiwoom.get_quote

    async def bad(t, ex): raise RuntimeError("timeout")
    eng.kiwoom.get_quote = bad
    _run(eng)
    assert box["placed"] == [] and not any("다시 내도 거부" in s for s in sent)
    eng.kiwoom.get_quote = good
    _run(eng)
    assert [p[2] for p in box["placed"]] == [72.45]


def test_quote_failure_on_final_run_gives_up():
    eng, reg, box, sent = _engine()
    _place_bot(reg, box)
    _broker_reject(box, "000007520")
    box["reject_sync"] = 1

    async def bad(t, ex): raise RuntimeError("timeout")
    eng.kiwoom.get_quote = bad
    asyncio.run(eng.recover_rejected(DAY, final=True))
    assert any("다시 내도 거부" in s for s in sent)


def test_non_api_error_is_unknown_and_stops():
    """API 거부가 아닌 오류는 주문이 나갔을 수 있다 — 2단계로 가지 않는다"""
    eng, reg, box, sent = _engine(cur=63.0)
    _place_bot(reg, box)
    _broker_reject(box, "000007520")

    async def boom(*a, **k): raise ValueError("bad json")
    eng.kiwoom.buy = boom
    _run(eng)
    _run(eng)
    assert any("결과를 확인할 수 없습니다" in s for s in sent)
    assert box["placed"] == []
    rows = reg.day_orders(TD, "TQQQ")
    assert rows[-1].status == "submitted"


def test_manual_fill_not_attached_when_bot_retry_live():
    """봇 재접수가 살아 있는데 사용자도 넣은 경우, 사용자 체결을 봇 주문으로 세지 않는다"""
    eng, reg, box, sent = _engine()
    _place_bot(reg, box)
    _broker_reject(box, "000007520")
    _run(eng)                                      # R1 살아 있음
    rec = reg.resolve(TD, "TQQQ", "buy", 72.55, 3, ord_no="MANUAL", trade_type=TradeType.LOC)
    assert rec is not None and "정규장 재접수" in (rec.note or "")    # 살아 있는 R1 쪽
    reg.record_fill(TD, "TQQQ", "buy", 3, 70.0, ord_no="000000101", order_price=72.55,
                    trade_type=TradeType.LOC)
    assert reg.resolve(TD, "TQQQ", "buy", 72.55, 3, ord_no="MANUAL",
                       trade_type=TradeType.LOC) is None


def test_recover_jobs_scheduled():
    src = (ROOT / "scheduler" / "engine.py").read_text(encoding="utf-8")
    assert 'f"recover_rejected_{i}"' in src
    assert "(31, self._recover_final)" in src
    import inspect
    from scheduler.engine import SchedulerEngine
    for n in ("_recover_first", "_recover_second", "_recover_final"):
        assert inspect.iscoroutinefunction(getattr(SchedulerEngine, n))


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
