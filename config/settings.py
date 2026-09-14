"""
================================================================
kbot 설정 로더

기존 telegram_commands.py [4], telegram_bot.py [5]의
.env 직접 사용 패턴 원칙을 적용
================================================================
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class KiwoomConfig:
    """
    키움증권 API 설정 (운영 전용)
    
    [1] 키움 REST API 문서:
        - 운영 도메인: https://api.kiwoom.com
        - WebSocket: wss://api.kiwoom.com:10000
    """
    api_base: str = "https://api.kiwoom.com"
    ws_url: str = "wss://api.kiwoom.com:10000"
    app_key: str = ""
    app_secret: str = ""
    mode: str = "real"  # 고정, 모의투자 미사용
    
    @property
    def is_production(self) -> bool:
        return True  # 항상 운영


@dataclass(frozen=True)
class TelegramConfig:
    """텔레그램 봇 설정"""
    bot_token: str = ""
    admin_id: str = ""


@dataclass(frozen=True)
class GCSConfig:
    """Google Cloud Storage 백업 설정 (선택)"""
    bucket_name: str = ""
    project_id: str = ""
    enabled: bool = False


@dataclass(frozen=True)
class AppConfig:
    """
    애플리케이션 전역 설정
    
    기존 [4]의 queue_ledger, blink_account 등
    런타임 구조체는 별도 state_manager에서 관리
    """
    kiwoom: KiwoomConfig
    telegram: TelegramConfig
    gcs: GCSConfig
    default_division: int = 40
    default_principal: float = 20000.0
    default_fee_rate: float = 0.0007  # 0.07% 입력 → 변환된 값
    data_dir: Path = Path("/opt/kbot/data")


class ConfigLoader:
    """
    VM .env 파일 로더
    
    기존 [5]의 도감 방지 로직 (_is_admin)과 유사하게
    필수값 누락 시 즉시 종료 (런타임 붕괴 방지)
    """
    
    ENV_PATH = Path("/opt/kbot/.env")
    
    @classmethod
    def load(cls) -> AppConfig:
        # .env 파일 존재 확인 (기존 [4]의 파일 존재 체크 패턴)
        if not cls.ENV_PATH.exists():
            # 개발 환경: 프로젝트 루트도 시도
            dev_path = Path(__file__).parent.parent / ".env"
            if dev_path.exists():
                load_dotenv(dev_path)
            else:
                raise FileNotFoundError(
                    f".env 파일 없음: {cls.ENV_PATH}\n"
                    f"VM에 복사: scp .env user@vm:/opt/kbot/"
                )
        else:
            load_dotenv(cls.ENV_PATH)
        
        # 키움 설정
        kiwoom = KiwoomConfig(
            api_base=os.getenv("KIWOOM_API_BASE", "https://api.kiwoom.com"),
            ws_url=os.getenv("KIWOOM_WS_URL", "wss://api.kiwoom.com:10000/api/us/websocket"),
            app_key=os.getenv("KIWOOM_APP_KEY", "").strip(),
            app_secret=os.getenv("KIWOOM_APP_SECRET", "").strip(),
        )
        
        # 필수값 검증 (기존 [5]의 관리자 ID 검증과 유사)
        if not kiwoom.app_key or not kiwoom.app_secret:
            raise ValueError(
                "KIWOOM_APP_KEY, KIWOOM_APP_SECRET 필수.\n"
                ".env 파일을 확인해주세요."
            )
        
        # 텔레그램 설정
        telegram = TelegramConfig(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            admin_id=os.getenv("TELEGRAM_ADMIN_ID", "").strip(),
        )
        
        if not telegram.bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN 필수")
        
        # GCS (선택)
        gcs = GCSConfig(
            bucket_name=os.getenv("GCS_BUCKET_NAME", ""),
            project_id=os.getenv("GCP_PROJECT_ID", ""),
            enabled=bool(os.getenv("GCS_BUCKET_NAME"))
        )
        
        return AppConfig(
            kiwoom=kiwoom,
            telegram=telegram,
            gcs=gcs,
            default_division=int(os.getenv("DEFAULT_DIVISION", "40")),
            default_principal=float(os.getenv("DEFAULT_PRINCIPAL", "20000")),
            default_fee_rate=float(os.getenv("DEFAULT_FEE_RATE", "0.0007")),
        )
