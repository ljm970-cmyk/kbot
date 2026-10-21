"""
================================================================
모든 파이썬 파일이 문법적으로 맞는지

다른 테스트는 main.py 를 import 하지 않아서, main.py 에 문법 오류가
있어도 전부 통과했다 (봇은 기동조차 못 하는데). 배포 전 테스트에서
반드시 잡히게 모든 .py 를 컴파일해 본다.

실행:  python tests/test_compile.py
================================================================
"""

import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = {"venv", ".venv", "data", "__pycache__", ".git"}


def _files():
    for p in sorted(ROOT.rglob("*.py")):
        if not any(part in SKIP for part in p.relative_to(ROOT).parts):
            yield p


def test_all_python_files_compile():
    bad = []
    for p in _files():
        try:
            py_compile.compile(str(p), doraise=True, cfile=None)
        except py_compile.PyCompileError as e:
            bad.append(f"{p.relative_to(ROOT)}: {e.msg.strip().splitlines()[-1]}")
    assert not bad, "\n".join(bad)


def test_main_is_checked():
    assert any(p.name == "main.py" and p.parent == ROOT for p in _files())


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
