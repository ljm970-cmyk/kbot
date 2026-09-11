infinite-buy-v4/
├── .github/
│   └── workflows/
│       └── deploy.yml              # CI/CD: push → VM 자동 배포
│
├── config/
│   ├── __init__.py
│   ├── settings.py                 # 환경변수 설정 (.env 기반)
│   └── market_hours.py             # KST/서머타임 계산
│
├── core/
│   ├── auth.py                     # 키움 OAuth 토큰 관리
│   └── http_client.py              # 재시도/타임아웃
│
├── api/
│   ├── __init__.py
│   ├── order_api.py                # 주문: ust20000~ust20003
│   ├── account_api.py              # 계좌: ust21110, ust21120
│   ├── market_data_api.py          # 시세: usa10007~usa10009
│   └── websocket_client.py         # WebSocket: uws10001/uws20005
│
├── infinite_buy/
│   ├── __init__.py
│   ├── models/
│   │   └── state.py                # InfiniteBuyState (T값, 잔금, 모드)
│   ├── calculator/
│   │   ├── t_value_calculator.py   # T값 (+1, +0.5, ×0.75, etc)
│   │   └── star_point_calculator.py # 별지점 (15-1.5T, etc)
│   ├── strategy/
│   │   ├── normal_mode.py          # 일반모드 (전반전/후반전)
│   │   ├── reverse_mode.py         # 리버스모드 (MOC, 쿼터)
│   │   └── mode_transition.py      # 일반↔리버스 자동 전환
│   └── order/
│       ├── conditional_loc.py      # LOC + 15% 재시도
│       └── moc_executor.py         # MOC 매도 (33)
│
├── database/
│   ├── __init__.py
│   ├── schema.sql                  # DB 스키마 (테이블 8개)
│   └── db_manager.py             # SQLite CRUD + 원자적 쓰기
│
├── notification/
│   ├── __init__.py
│   ├── telegram_bot.py             # 단방향 알림 발송
│   ├── telegram_commands_v4.py     # 양방향 커맨드 (/start, /sync, etc)
│   └── views/                      # UI 템플릿 분리
│
├── scheduler/
│   ├── __init__.py
│   ├── market_time.py              # KST 17:00/18:00 판단
│   └── trading_scheduler.py        # APScheduler 등록
│
├── logs/                           # .gitignore (런타임 로그)
├── data/                           # .gitignore (SQLite DB)
│
├── tests/
│   ├── __init__.py
│   ├── test_t_value.py
│   ├── test_star_point.py
│   └── integration/
│       └── test_scenarios.py       # 5대 시나리오
│
├── scripts/
│   ├── setup-vm.sh                 # VM 초기 설치
│   ├── deploy.sh                   # 배포 스크립트
│   └── health_check.sh             # 헬스체크
│
├── main.py                         # 애플리케이션 진입점
├── requirements.txt                # Python 의존성
├── Dockerfile                      # Docker 이미지
├── docker-compose.yml              # Docker Compose
├── .env.example                    # 환경변수 템플릿
├── .gitignore                      # 제외 파일
└── README.md                       # 문서
