"""
================================================================
설정 로더 · 상태 저장소 테스트

실행:  python tests/test_state.py
================================================================
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import ConfigLoader, KiwoomConfig, TelegramConfig
from core.state_manager import StateManager, atomic_write_json, read_json
from modes.base_mode import PositionState


def _mgr() -> StateManager:
    return StateManager(Path(tempfile.mkdtemp()) / "data")


def _cfg() -> dict:
    return {"division": 40, "principal": 20000.0, "fee_rate": 0.0007, "is_active": True}


# ================================================================
# 경로
# ================================================================

def test_no_hardcoded_data_path_in_code():
    """데이터 경로가 코드에 하드코딩돼 있으면 안 된다.

    docstring 의 설명 문구는 제외하고 실제 코드의 문자열 상수만 본다.
    """
    import ast
    src = (Path(__file__).resolve().parent.parent / "core" / "state_manager.py").read_text()
    tree = ast.parse(src)

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                docstrings.add(d)

    literals = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n.value not in docstrings
    ]
    bad = [v for v in literals if v.startswith("/opt/") or v.startswith("/home/")]
    assert bad == [], f"하드코딩된 절대경로: {bad}"


def test_data_dir_is_injected():
    tmp = Path(tempfile.mkdtemp()) / "custom"
    m = StateManager(tmp)
    m.save_config("TQQQ", _cfg())
    assert (tmp / "config" / "TQQQ.json").exists()


# ================================================================
# 설정
# ================================================================

def test_save_and_get_config():
    m = _mgr()
    m.save_config("tqqq", _cfg())
    cfg = m.get_config("TQQQ")
    assert cfg["division"] == 40
    assert cfg["ticker"] == "TQQQ"
    assert "created_at" in cfg


def test_list_tickers_skips_inactive():
    m = _mgr()
    m.save_config("TQQQ", _cfg())
    m.save_config("SOXL", {**_cfg(), "division": 20})
    assert m.list_tickers() == ["SOXL", "TQQQ"]
    m.deactivate("SOXL")
    assert m.list_tickers() == ["TQQQ"]
    assert "SOXL" in m.list_tickers(active_only=False)


# ================================================================
# 상태
# ================================================================

def test_initial_state_from_config():
    m = _mgr()
    m.save_config("TQQQ", _cfg())
    st = m.get_state("TQQQ")
    assert st.T == 0.0
    assert st.holdings == 0
    assert st.cash == 20000.0
    assert st.mode == "normal"


def test_state_roundtrip_preserves_precision():
    """평단의 소수점이 저장·복원 과정에서 깎이면 안 된다"""
    m = _mgr()
    m.save_config("TQQQ", _cfg())
    st = m.get_state("TQQQ")
    st.T = 38.14375
    st.avg_price = 110 / 7          # 15.714285714285714
    m.save_state(st)

    st2 = m.get_state("TQQQ")
    assert st2.T == 38.14375
    assert abs(st2.avg_price - 110 / 7) < 1e-12


def test_no_config_returns_none():
    assert _mgr().get_state("TQQQ") is None


def test_reset_state():
    m = _mgr()
    m.save_config("TQQQ", _cfg())
    st = m.get_state("TQQQ")
    st.T = 10.0
    st.holdings = 50
    m.save_state(st)

    st2 = m.reset_state("TQQQ")
    assert st2.T == 0.0
    assert st2.holdings == 0


# ================================================================
# 원자적 저장 · 락
# ================================================================

def test_atomic_write_leaves_no_partial_file():
    """쓰기 실패 시 임시파일이 남지 않는다"""
    d = Path(tempfile.mkdtemp())
    target = d / "state.json"
    atomic_write_json(target, {"a": 1})

    class Unserializable:
        pass

    # default=str 은 '값'만 처리한다. 직렬화 불가능한 '키'는 반드시 터진다.
    try:
        atomic_write_json(target, {Unserializable(): 1})
    except TypeError:
        pass
    else:
        raise AssertionError("직렬화 실패가 예외로 올라와야 한다")

    leftovers = [p for p in d.iterdir() if p.name.startswith(".")]
    assert leftovers == []
    assert read_json(target) == {"a": 1}     # 기존 파일이 온전하다


def test_edit_state_persists_on_exit():
    m = _mgr()
    m.save_config("TQQQ", _cfg())
    with m.edit_state("TQQQ") as st:
        st.cash = 12345.67
        st.T = 7.5
    assert m.get_state("TQQQ").cash == 12345.67
    assert m.get_state("TQQQ").T == 7.5


def test_edit_state_missing_config_raises():
    m = _mgr()
    try:
        with m.edit_state("NOPE"):
            pass
    except KeyError:
        pass
    else:
        raise AssertionError("설정 없는 종목은 KeyError 여야 한다")


def test_corrupted_state_file_does_not_crash():
    """상태 파일이 깨져도 예외로 죽지 않고 설정 기반 초기화로 돌아간다"""
    m = _mgr()
    m.save_config("TQQQ", _cfg())
    (m.data_dir / "state" / "TQQQ.json").write_text("{ broken json")
    st = m.get_state("TQQQ")
    assert st is not None
    assert st.T == 0.0


# ================================================================
# tg_bot 구버전 호환
#
# 텔레그램 코드는 get_state(user_id, ticker) 처럼 사용자별 상태를
# 전제한다. 이 봇은 계좌 하나로 매매하므로 user_id 는 무시하되,
# 호출부가 깨지지 않게 시그니처를 받아준다.
# ================================================================

def test_legacy_two_arg_get_state():
    m = _mgr()
    m.save_ticker_config("12345", "TQQQ", _cfg())
    st = m.get_state("12345", "TQQQ")
    assert st is not None
    assert st.division == 40


def test_new_and_legacy_share_one_state():
    """같은 종목이면 user_id 와 무관하게 같은 상태를 본다.
    계좌가 하나이므로 갈라지면 오히려 위험하다."""
    m = _mgr()
    m.save_config("TQQQ", _cfg())
    with m.edit_state("TQQQ") as st:
        st.T = 12.5
    assert m.get_state("99999", "TQQQ").T == 12.5
    assert m.get_state("TQQQ").T == 12.5


def test_state_supports_dict_access():
    """commands_handler 가 state['T'] 처럼 읽는다"""
    m = _mgr()
    m.save_config("TQQQ", _cfg())
    st = m.get_state("TQQQ")
    st.T = 7.5
    assert st["T"] == 7.5
    assert st["division"] == 40
    assert st["mode"] == "normal"
    assert st.get("holdings") == 0
    assert st.get("없는키", "기본값") == "기본값"
    assert "T" in st
    assert "없는키" not in st


def test_dict_access_raises_keyerror():
    m = _mgr()
    m.save_config("TQQQ", _cfg())
    st = m.get_state("TQQQ")
    try:
        st["존재하지않음"]
    except KeyError:
        pass
    else:
        raise AssertionError("없는 키는 KeyError 여야 한다")


def test_legacy_config_methods():
    m = _mgr()
    m.save_ticker_config("u1", "SOXL", {**_cfg(), "division": 20, "principal": 15000.0})
    cfg = m.get_ticker_config("u1", "SOXL")
    assert cfg["division"] == 20
    assert m.get_principal("u1", "SOXL") == 15000.0
    assert m.get_user_tickers("u1") == ["SOXL"]
    m.deactivate_ticker("u1", "SOXL")
    assert m.get_user_tickers("u1") == []


def test_legacy_manual_correction():
    """수동 보정은 기록만 하는 게 아니라 장부를 실제로 바꾼다.

    구버전은 파일에만 남기고 아무도 읽지 않아, /fix 가 사실상
    동작하지 않았다.
    """
    m = _mgr()
    m.save_config("TQQQ", _cfg())
    st = m.get_state("TQQQ")
    st.holdings, st.avg_price, st.cash = 10, 100.0, 5000.0
    m.save_state(st)

    res = m.add_manual_correction("u1", "TQQQ",
                                  {"qty": 2, "price": 90.0, "side": "buy"})
    assert res["ok"] is True
    assert m.get_state("TQQQ").holdings == 12

    rows = m.corrections("TQQQ")
    assert len(rows) == 1
    assert rows[0]["qty"] == 2


def test_manual_correction_needs_config():
    """설정이 없는 종목은 보정할 수 없다"""
    res = _mgr().add_manual_correction("u1", "NOPE",
                                       {"qty": 1, "price": 10.0, "side": "buy"})
    assert res["ok"] is False


def test_legacy_three_arg_save_state():
    m = _mgr()
    m.save_config("TQQQ", _cfg())
    m.save_state("u1", "TQQQ", {"ticker": "TQQQ", "division": 40, "T": 3.25,
                                "holdings": 11, "cash": 500.0, "mode": "normal"})
    st = m.get_state("TQQQ")
    assert st.T == 3.25
    assert st.holdings == 11


# ================================================================
# 아카이브
# ================================================================

def test_archive_appends():
    m = _mgr()
    m.archive_eod("TQQQ", "20260915", {"T": 9.0, "holdings": 67})
    m.archive_eod("TQQQ", "20260916", {"T": 9.5, "holdings": 70})
    m.archive_eod("SOXL", "20260916", {"T": 3.0, "holdings": 12})

    rows = m.read_archive("202609", "TQQQ")
    assert len(rows) == 2
    assert rows[0]["T"] == 9.0
    assert len(m.read_archive("202609")) == 3


def test_corrections_recorded():
    m = _mgr()
    m.record_correction("TQQQ", {"reason": "누락 체결 반영", "qty": 3, "price": 68.1})
    rows = m.corrections("TQQQ")
    assert len(rows) == 1
    assert rows[0]["qty"] == 3
    assert "recorded_at" in rows[0]


# ================================================================
# 설정 로더
# ================================================================

def _write_env(**kw) -> Path:
    d = Path(tempfile.mkdtemp())
    lines = {
        "KIWOOM_APP_KEY": "abcdefgh12345678",
        "KIWOOM_APP_SECRET": "secret",
        "TELEGRAM_BOT_TOKEN": "123:ABC",
        "TELEGRAM_ADMIN_ID": "999",
    }
    lines.update(kw)
    p = d / ".env"
    p.write_text("\n".join(f"{k}={v}" for k, v in lines.items()))
    return p


def _clear_env():
    for k in list(os.environ):
        if k.startswith(("KIWOOM_", "TELEGRAM_", "KBOT_", "DEFAULT_")):
            del os.environ[k]


def test_loader_reads_env_file():
    _clear_env()
    env = _write_env()
    os.environ["KBOT_HOME"] = str(env.parent)
    cfg = ConfigLoader.load(env)
    assert cfg.kiwoom.app_key == "abcdefgh12345678"
    assert cfg.telegram.is_admin("999")
    _clear_env()


def test_mock_switch_changes_base_url():
    _clear_env()
    env = _write_env(KIWOOM_MOCK="true")
    os.environ["KBOT_HOME"] = str(env.parent)
    cfg = ConfigLoader.load(env)
    assert cfg.kiwoom.mock is True
    assert cfg.kiwoom.base_url == "https://mockapi.kiwoom.com"
    assert cfg.kiwoom.is_production is False
    _clear_env()


def test_missing_admin_id_rejected():
    """관리자 지정 없이 봇을 열면 누구나 주문을 낼 수 있다"""
    _clear_env()
    env = _write_env(TELEGRAM_ADMIN_ID="")
    os.environ["KBOT_HOME"] = str(env.parent)
    try:
        ConfigLoader.load(env)
    except ValueError as e:
        assert "ADMIN" in str(e)
    else:
        raise AssertionError("관리자 미지정은 거부돼야 한다")
    _clear_env()


def test_missing_keys_rejected():
    _clear_env()
    env = _write_env(KIWOOM_APP_KEY="")
    os.environ["KBOT_HOME"] = str(env.parent)
    try:
        ConfigLoader.load(env)
    except ValueError:
        pass
    else:
        raise AssertionError("앱키 누락은 거부돼야 한다")
    _clear_env()


def test_app_key_is_masked_in_summary():
    """로그에 앱키 전체가 찍히면 안 된다"""
    k = KiwoomConfig(app_key="abcdefgh12345678", app_secret="s")
    assert k.masked() == "abcd...5678"
    assert "abcdefgh12345678" not in k.masked()


def test_multiple_admins():
    t = TelegramConfig(bot_token="x", admin_id="111, 222 ,333")
    assert t.admin_ids == {"111", "222", "333"}
    assert t.is_admin(222) is True
    assert t.is_admin("444") is False


def test_paths_are_created():
    _clear_env()
    env = _write_env()
    home = env.parent
    os.environ["KBOT_HOME"] = str(home)
    cfg = ConfigLoader.load(env)
    assert cfg.paths.state.exists()
    assert cfg.paths.orders_db.parent.exists()
    assert cfg.paths.data == home / "data"
    _clear_env()


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
