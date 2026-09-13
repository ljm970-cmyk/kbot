# kbot - 무한매수법 V4.0 (키움증권 미국주식 자동매매)

## 설치
```bash
git clone https://github.com/YOUR_ID/kbot.git
cd kbot
pip install -r requirements.txt
# kbot - 무한매수법 자동 매매 시스템

키움증권 REST API를 이용한 TQQQ/SOXL 무한매수법 자동 매매 봇

## 기능

- **종목**: TQQQ 단독 / SOXL 단독 / 동시 운용
- **모드**: 일반모드 (매수중심) ↔ 리버스모드 (매도중심) 자동 전환
- **시간**: 써머타임/비써머타임 KST 기준 자동 구분
- **주문**: 프리장 LOC/MOC/지정가(GTC) 예약주문
- **계산**: 장마감 후 T값/잔금/평단 일괄 계산
- **알림**: 텔레그램 실시간 상태/주문/알림

## 아키텍처
kbot/
├── main.py # 진입점
├── config/ # 설정 로드
├── core/ # T값/별지점/상태 계산
├── modes/ # 일반모드/리버스모드
├── kiwoom/ # REST API / WebSocket
├── scheduler/ # Cron 스케줄러
├── eod/ # 장마감 후 계산
├── telegram/ # 텔레그램 봇
└── utils/ # 공통 유틸리티
