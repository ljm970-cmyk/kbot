#!/usr/bin/env python3
"""모든 테스트를 순서대로 실행한다.

    python tests/run_all.py
"""
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent

def main() -> int:
    total = failed = 0
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        r = subprocess.run([sys.executable, str(path)],
                           cwd=ROOT, capture_output=True, text=True)
        passed = r.stdout.count("  PASS  ")
        total += passed
        tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "출력 없음"
        mark = "OK " if r.returncode == 0 else "실패"
        print(f"  {mark}  {path.stem:<24} {passed:>3}건  {tail}")
        if r.returncode != 0:
            failed += 1
            for line in r.stdout.splitlines():
                if "FAIL" in line:
                    print(f"          {line.strip()}")
            if r.stderr.strip():
                print(f"          {r.stderr.strip().splitlines()[-1]}")
    print("-" * 62)
    print(f"  총 {total}건 통과" + ("" if failed == 0 else f" · {failed}개 파일 실패"))
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
