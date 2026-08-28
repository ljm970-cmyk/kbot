#!/usr/bin/env python3
"""KST 동기화 검증"""

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
UTC = ZoneInfo("UTC")

def check_kst_sync():
    print("=" * 60)
    print("🔍 KST 검증")
    print("=" * 60)
    
    # 환경변수
    tz_env = os.environ.get("TZ", "❌ 미설정")
    print(f"\n[1] TZ env: {tz_env}")
    
    # 시간
    now_kst = datetime.now(KST)
    now_utc = datetime.now(UTC)
    diff = (now_kst.hour - now_utc.hour) % 24
    
    print(f"[2] KST: {now_kst.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"[3] UTC: {now_utc.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"[4] 차이: {diff}시간")
    
    # 판정
    if tz_env == "Asia/Seoul" and diff in [8, 9, 10]:
        print(f"\n✅ KST 정상")
        return 0
    else:
        print(f"\n❌ KST 이상")
        return 1

if __name__ == "__main__":
    sys.exit(check_kst_sync())