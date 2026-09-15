"""
================================================================
kbot 설정 로더

기존 코드는 /opt/kbot 을 하드코딩했는데 실제 배포 경로는 ~/kbot 이라
상태 파일 저장이 권한 오류로 죽었다. 모든 경로를 한 곳에서 결정하도록
바꾸고, 환경변수로 덮어쓸 수 있게 했다.

경로 우선순위
  1. 환경변수 KBOT_HOME / KBOT_DATA_DIR
  2. 프로젝트 루트 (이 파일 기준 상위 디렉토리)

.env 탐색 순서
  1. 환경변수 KBOT_ENV_FILE
  2. <프로젝트 루트>/.env
  3. /opt/kbot/.env  (구버전 배포 호환)
================================================================
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

#: 이 파일은 <프로젝트 루트>/config/settings.py 에 있다
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_home() -> Path:
    env = os.getenv("KBOT_HOME")
    return Path(env).expanduser().resolve() if env else PROJECT_ROOT


def _resolve_env_file(home: Path) -> Optional[Path]:
    explicit = os.getenv("KBOT_ENV_FILE")
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.exists() else None
    for candidate in (home / ".env", Path("/opt/kbot/.env")):
        if candidate.exists():
            return candidate
    return None


# ================================================================
# 설정 구조
# ================================================================

@dataclass(frozen=True)
class KiwoomConfig:
    """키움증권 API 설정

    운영   https://api.kiwoom.com
    모의투자 https://mockapi.kiwoom.com

    실계좌에 붙이기 전 모의투자로 LOC(30)·MOC(33) 접수를 확인해야 하므로
    mock 스위치를 열어뒀다 (기존 코드는 운영 전용으로 막혀 있었다).
    """
    app_key: str
    app_secret: str
    mock: bool = False
    api_base: str = "https://api.kiwoom.com"
    mock_api_base: str = "https://mockapi.kiwoom.com"
    ws_url: str = "wss://api.kiwoom.com:10000"
    mock_ws_url: str = "wss://mockapi.kiwoom.com:10000"

    @property
    def base_url(self) -> str:
        return self.mock_api_base if self.mock else self.api_base

    @property
    def websocket_url(self) -> str:
        return self.mock_ws_url if self.mock else self.ws_url

    @property
    def is_production(self) -> bool:
        return not self.mock

    def masked(self) -> str:
        k = self.app_key
        return f"{k[:4]}...{k[-4:]}" if len(k) > 8 else "****"


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str = ""
    admin_id: str = ""

    @property
    def admin_ids(self) -> set[str]:
        """쉼표 구분 다중 관리자 지원"""
        return {x.strip() for x in self.admin_id.split(",") if x.strip()}

    def is_admin(self, user_id) -> bool:
        return str(user_id) in self.admin_ids


@dataclass(frozen=True)
class Paths:
    """모든 파일 경로를 한 곳에서 결정한다"""
    home: Path
    data: Path
    logs: Path

    @property
    def state(self) -> Path:
        return self.data / "state"

    @property
    def config(self) -> Path:
        return self.data / "config"

    @property
    def orders_db(self) -> Path:
        return self.data / "orders" / "orders.db"

    @property
    def fills_db(self) -> Path:
        return self.data / "fills" / "realtime_fills.db"

    def ensure(self) -> None:
        for p in (self.data, self.logs, self.state, self.config,
                  self.orders_db.parent, self.fills_db.parent):
            p.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class AppConfig:
    kiwoom: KiwoomConfig
    telegram: TelegramConfig
    paths: Paths
    default_division: int = 40
    default_principal: float = 20000.0
    default_fee_rate: float = 0.0007
    default_min_fee: float = 0.0
    dry_run: bool = False

    def summary(self) -> str:
        mode = "모의투자" if self.kiwoom.mock else "실계좌"
        dry = " · DRY RUN(주문 미전송)" if self.dry_run else ""
        return (f"{mode}{dry}\n"
                f"  API   {self.kiwoom.base_url}\n"
                f"  앱키  {self.kiwoom.masked()}\n"
                f"  홈    {self.paths.home}\n"
                f"  데이터 {self.paths.data}")


# ================================================================
# 로더
# ================================================================

def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


class ConfigLoader:
    """.env 로더"""

    @classmethod
    def load(cls, env_file: Optional[str | Path] = None) -> AppConfig:
        home = _resolve_home()

        path = Path(env_file).expanduser() if env_file else _resolve_env_file(home)
        if path and path.exists():
            load_dotenv(path, override=False)
        # .env 가 없어도 환경변수만으로 구동할 수 있어야 한다 (systemd EnvironmentFile 등)

        app_key = os.getenv("KIWOOM_APP_KEY", "").strip()
        app_secret = os.getenv("KIWOOM_APP_SECRET", "").strip()
        if not app_key or not app_secret:
            raise ValueError(
                "KIWOOM_APP_KEY / KIWOOM_APP_SECRET 가 필요합니다.\n"
                f"  .env 탐색 경로: {path or '(없음)'}\n"
                f"  KBOT_ENV_FILE 로 직접 지정할 수 있습니다."
            )

        kiwoom = KiwoomConfig(
            app_key=app_key,
            app_secret=app_secret,
            mock=_env_bool("KIWOOM_MOCK", False),
            api_base=os.getenv("KIWOOM_API_BASE", "https://api.kiwoom.com"),
            ws_url=os.getenv("KIWOOM_WS_URL", "wss://api.kiwoom.com:10000"),
        )

        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN 이 필요합니다.")

        telegram = TelegramConfig(
            bot_token=bot_token,
            admin_id=os.getenv("TELEGRAM_ADMIN_ID", "").strip(),
        )
        if not telegram.admin_ids:
            raise ValueError(
                "TELEGRAM_ADMIN_ID 가 필요합니다. "
                "관리자 지정 없이 봇을 열면 누구나 주문을 낼 수 있습니다."
            )

        data_env = os.getenv("KBOT_DATA_DIR")
        paths = Paths(
            home=home,
            data=Path(data_env).expanduser().resolve() if data_env else home / "data",
            logs=Path(os.getenv("KBOT_LOG_DIR", str(home / "logs"))).expanduser(),
        )
        paths.ensure()

        return AppConfig(
            kiwoom=kiwoom,
            telegram=telegram,
            paths=paths,
            default_division=int(os.getenv("DEFAULT_DIVISION", "40")),
            default_principal=float(os.getenv("DEFAULT_PRINCIPAL", "20000")),
            default_fee_rate=float(os.getenv("DEFAULT_FEE_RATE", "0.0007")),
            default_min_fee=float(os.getenv("DEFAULT_MIN_FEE", "0")),
            dry_run=_env_bool("KBOT_DRY_RUN", False),
        )
