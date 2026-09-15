"""
================================================================
키움증권 미국주식 REST API 클라이언트

키움 REST API 문서 기준 재작성:
  - api-id / authorization / cont-yn / next-key 는 HTTP 헤더
  - 요청 바디는 중첩 없는 평평한 JSON
  - 매수/매도는 바디 필드가 아니라 api-id 로 구분

엔드포인트
  /oauth2/token       au10001  접근토큰 발급
  /oauth2/revoke      au10002  접근토큰 폐기
  /api/us/ordr        ust20000 매수 / ust20001 매도 / ust20003 취소
                      ust21200 예약매수 / ust21201 예약매도
                      ust21203 예약취소 / ust21205 예약내역
                      ust31490 주문가능수량
  /api/us/acnt        ust21070 원장잔고 / ust21050 원장미체결
                      ust21160 예수금상세 / ust21510 당일주문체결
  /api/us/mrkcond     usa20100 현재가 / usa20590 일별주가
================================================================
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import aiohttp

logger = logging.getLogger("kbot.kiwoom")


# ================================================================
# 상수 (kiwoom/constants.py 에서 re-export — 기존 import 경로 호환)
# ================================================================

from kiwoom.constants import (  # noqa: F401
    BUY_ALLOWED_TRADE_TYPES,
    EXCHANGE_MAP,
    Exchange,
    ReserveType,
    SideFilter,
    TradeType,
    exchange_of,
)


# ================================================================
# 예외 / 응답
# ================================================================

#: 재시도하면 안 되는 API (비멱등).
#:
#: 주문 요청이 서버에 도달했는데 응답만 못 받는 경우가 있다. 이때
#: 재시도하면 같은 주문이 한 번 더 들어간다. 조회는 몇 번을 다시 해도
#: 안전하지만, 주문·취소는 그렇지 않다.
NON_IDEMPOTENT_APIS = frozenset({
    "ust20000",   # 매수
    "ust20001",   # 매도
    "ust20003",   # 취소
    "ust21200",   # 예약매수
    "ust21201",   # 예약매도
    "ust21203",   # 예약취소
})


class KiwoomOrderUncertainError(Exception):
    """주문 요청의 결과를 알 수 없는 상태.

    통신이 끊겨 응답을 못 받았을 뿐, 주문은 접수됐을 수 있다.
    절대 재시도하지 말고, 조회로 실제 상태를 확인해야 한다.
    """

    def __init__(self, api_id: str, cause: Exception):
        self.api_id = api_id
        self.cause = cause
        super().__init__(
            f"[{api_id}] 주문 결과를 확인할 수 없습니다 ({cause}). "
            f"접수됐을 수 있으므로 재시도하지 않습니다. "
            f"ust21205/ust21050 으로 실제 상태를 확인하세요."
        )


#: "조회 결과 없음" 을 뜻하는 메시지 조각.
#:
#: 키움은 데이터가 없을 때도 return_code != 0 으로 응답한다.
#: 에러로 처리하면 예약주문이 하나도 없는 날 검증 단계가 통째로 실패한다.
EMPTY_RESULT_HINTS = ("자료가 존재하지 않습니다", "조회할 자료가 없습니다",
                      "데이터가 없습니다")


def is_empty_result(message: str) -> bool:
    return any(h in (message or "") for h in EMPTY_RESULT_HINTS)


class KiwoomAPIError(Exception):
    """키움 API 가 return_code != 0 을 반환했을 때"""

    def __init__(self, api_id: str, code: Any, message: str, body: dict | None = None):
        self.api_id = api_id
        self.code = code
        self.message = message
        self.body = body or {}
        super().__init__(f"[{api_id}] return_code={code}: {message}")


class KiwoomAuthError(KiwoomAPIError):
    """토큰 발급 / 만료 관련 오류"""


@dataclass
class OrderResult:
    """주문 접수 결과"""
    ord_no: str
    stk_nm: str = ""
    raw: dict = None

    def __post_init__(self):
        if self.raw is None:
            self.raw = {}


@dataclass
class ReserveResult:
    """예약주문 접수 결과"""
    rsrv_ord_no: str
    frcs_dt: str = ""
    raw: dict = None

    def __post_init__(self):
        if self.raw is None:
            self.raw = {}


# ================================================================
# 파싱 헬퍼
# ================================================================

def to_float(value: Any) -> float:
    """키움 숫자 문자열 → float

    주의: 시세 필드는 등락 방향을 부호로 표현한다.
    `"-198.4500"` 은 음수 가격이 아니라 '하락 마감한 198.45' 라는 뜻이다.
    가격은 항상 양수이므로 절댓값을 취한다.

        to_float("-198.4500")  -> 198.45
        to_float("+201.4700")  -> 201.47
        to_float("1,507.70")   -> 1507.70
        to_float("")           -> 0.0
    """
    if value is None:
        return 0.0
    s = str(value).strip().replace(",", "")
    if not s:
        return 0.0
    s = s.lstrip("+-")
    try:
        return abs(float(s))
    except ValueError:
        return 0.0


def to_signed_float(value: Any) -> float:
    """부호를 그대로 살려야 하는 필드용 (손익금액, 정산금 등)"""
    if value is None:
        return 0.0
    s = str(value).strip().replace(",", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def to_int(value: Any) -> int:
    """0-padding 된 수량 문자열 → int  ("000000000005" -> 5)"""
    if value is None:
        return 0
    s = str(value).strip().replace(",", "")
    if not s:
        return 0
    neg = s.startswith("-")
    s = s.lstrip("+-").lstrip("0") or "0"
    try:
        n = int(float(s))
    except ValueError:
        return 0
    return -n if neg else n


def fmt_price(price: float) -> str:
    """주문단가 포맷 (미국주식 호가단위 0.01)"""
    return f"{round(float(price) + 1e-9, 2):.2f}"


def _pick_list(body: dict, *keys: str) -> list:
    """응답에서 결과 리스트를 꺼낸다.

    문서 예시에 `result_lsit` 같은 오타가 섞여 있어(ust21510) 후보를 모두 본다.
    """
    for k in (*keys, "result_list", "result_lsit", "output", "output1", "output2"):
        v = body.get(k)
        if isinstance(v, list):
            return v
    return []


def _pick(row: dict, *keys: str, default: Any = "") -> Any:
    """응답 스펙과 예시가 엇갈리는 필드용 (trde_tp vs frgn_trde_tp 등)"""
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return default


# ================================================================
# 클라이언트
# ================================================================

class KiwoomAPIClient:
    """키움 미국주식 REST API 비동기 클라이언트

    사용법::

        async with KiwoomAPIClient(app_key, app_secret) as api:
            quote = await api.get_quote("TQQQ", Exchange.NASDAQ)
            res = await api.reserve_loc_buy("TQQQ", Exchange.NASDAQ, 52.82, 11)

    또는 수동 관리::

        api = KiwoomAPIClient(app_key, app_secret)
        await api.start()
        ...
        await api.close()
    """

    PATH_TOKEN = "/oauth2/token"
    PATH_REVOKE = "/oauth2/revoke"
    PATH_ORDER = "/api/us/ordr"
    PATH_ACCOUNT = "/api/us/acnt"
    PATH_MARKET = "/api/us/mrkcond"
    PATH_CHART = "/api/us/chart"

    #: 연속조회 최대 반복 (무한루프 방지)
    MAX_PAGES = 20
    #: 요청 간 최소 간격(초) — 키움 유량 제한 대비
    MIN_INTERVAL = 0.2

    #: User-Agent.
    #:
    #: 키움 앞단 WAF 가 aiohttp 기본 UA(Python/3.x aiohttp/3.x)를 차단한다.
    #: 헤더 없이 보내면 API 가 아니라 방화벽이 HTML 400 "Request Blocked" 를
    #: 돌려주므로 응답 파싱부터 실패한다. 문서에 없는 사항이라
    #: 실제 호출로만 확인된다.
    USER_AGENT = "Mozilla/5.0 (compatible; kbot/1.0)"

    def __init__(
        self,
        app_key: Any,
        app_secret: str = "",
        base_url: str = "https://api.kiwoom.com",
        mock: bool = False,
        timeout: float = 20.0,
        max_retries: int = 2,
    ):
        # 호환: KiwoomAPIClient(config.kiwoom) 형태의 기존 호출 지원
        if hasattr(app_key, "app_key"):
            cfg = app_key
            app_key = cfg.app_key
            app_secret = cfg.app_secret
            base_url = getattr(cfg, "api_base", base_url) or base_url
            mock = mock or getattr(cfg, "mode", "real") == "mock"

        if mock:
            base_url = "https://mockapi.kiwoom.com"
        self.base_url = base_url.rstrip("/")
        self.app_key = app_key
        self.app_secret = app_secret
        self.mock = mock
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_retries = max_retries

        self.token: str = ""
        self.token_expiry: Optional[datetime] = None

        self._session: Optional[aiohttp.ClientSession] = None
        self._auth_lock = asyncio.Lock()
        self._rate_lock = asyncio.Lock()
        self._last_call = 0.0

    # ------------------------------------------------------------
    # 수명주기
    # ------------------------------------------------------------

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        await self._ensure_token()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> "KiwoomAPIClient":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    # ------------------------------------------------------------
    # 인증
    # ------------------------------------------------------------

    async def authenticate(self) -> bool:
        """접근토큰 발급 (au10001)

        응답의 `expires_dt` 는 YYYYMMDDHHMMSS 문자열이다.
        `expires_in` 필드는 존재하지 않는다.
        """
        session = self._ensure_session()
        url = f"{self.base_url}{self.PATH_TOKEN}"
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.app_secret,
        }

        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": self.USER_AGENT,
        }
        async with session.post(url, json=payload, headers=headers) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception as e:
                text = await resp.text()
                raise KiwoomAuthError("au10001", resp.status, f"응답 파싱 실패: {text[:200]}") from e

        if data.get("return_code") not in (0, None) or not data.get("token"):
            raise KiwoomAuthError(
                "au10001",
                data.get("return_code"),
                data.get("return_msg", "토큰 발급 실패"),
                data,
            )

        self.token = data["token"]
        self.token_expiry = self._parse_expiry(data.get("expires_dt"))
        logger.info("키움 토큰 발급 성공 (만료 %s)", self.token_expiry)
        return True

    @staticmethod
    def _parse_expiry(expires_dt: Optional[str]) -> datetime:
        """expires_dt(YYYYMMDDHHMMSS) → datetime. 5분 여유를 둔다."""
        if expires_dt:
            try:
                return datetime.strptime(str(expires_dt), "%Y%m%d%H%M%S") - timedelta(minutes=5)
            except ValueError:
                logger.warning("expires_dt 파싱 실패: %r", expires_dt)
        # 파싱 실패 시 보수적으로 1시간
        return datetime.now() + timedelta(hours=1)

    async def _ensure_token(self) -> None:
        """토큰이 없거나 만료가 임박하면 갱신 (동시 갱신 방지)"""
        if self.token and self.token_expiry and datetime.now() < self.token_expiry:
            return
        async with self._auth_lock:
            # lock 대기 중 다른 코루틴이 갱신했을 수 있다
            if self.token and self.token_expiry and datetime.now() < self.token_expiry:
                return
            await self.authenticate()

    async def revoke(self) -> None:
        """접근토큰 폐기 (au10002)"""
        if not self.token:
            return
        session = self._ensure_session()
        url = f"{self.base_url}{self.PATH_REVOKE}"
        payload = {
            "appkey": self.app_key,
            "secretkey": self.app_secret,
            "token": self.token,
        }
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": self.USER_AGENT,
        }
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                await resp.json(content_type=None)
        except Exception as e:
            logger.warning("토큰 폐기 실패: %s", e)
        finally:
            self.token = ""
            self.token_expiry = None

    # ------------------------------------------------------------
    # 요청 코어
    # ------------------------------------------------------------

    def _headers(self, api_id: str, cont_yn: str = "N", next_key: str = "") -> dict:
        return {
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": self.USER_AGENT,
            "authorization": f"Bearer {self.token}",
            "api-id": api_id,
            "cont-yn": cont_yn,
            "next-key": next_key,
        }

    async def _throttle(self) -> None:
        async with self._rate_lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait = self.MIN_INTERVAL - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = asyncio.get_running_loop().time()

    async def request(
        self,
        api_id: str,
        path: str,
        body: dict | None = None,
        cont_yn: str = "N",
        next_key: str = "",
    ) -> tuple[dict, dict]:
        """단건 요청. (응답바디, 응답헤더) 반환.

        return_code != 0 이면 KiwoomAPIError 를 던진다.
        """
        await self._ensure_token()
        session = self._ensure_session()
        url = f"{self.base_url}{path}"
        payload = body or {}

        last_exc: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            await self._throttle()
            try:
                async with session.post(
                    url, headers=self._headers(api_id, cont_yn, next_key), json=payload
                ) as resp:
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    data = await resp.json(content_type=None)

                    # 인증 만료 → 1회 재발급 후 재시도.
                    # 주문 계열은 제외한다. 401 이 뜬 시점에 주문이 접수되지
                    # 않았다고 단정할 수 없고, 재전송하면 중복 위험이 있다.
                    if (resp.status in (401, 403) and attempt < self.max_retries
                            and api_id not in NON_IDEMPOTENT_APIS):
                        logger.warning("[%s] 인증 오류(%s), 토큰 재발급", api_id, resp.status)
                        self.token = ""
                        self.token_expiry = None
                        await self._ensure_token()
                        continue

                    code = data.get("return_code")
                    if code not in (0, None):
                        raise KiwoomAPIError(api_id, code, data.get("return_msg", ""), data)

                    return data, resp_headers

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exc = e

                # 주문 계열은 재시도하지 않는다. 요청이 서버에 도달한 뒤
                # 응답만 유실됐을 수 있고, 그때 재시도하면 중복 주문이 된다.
                if api_id in NON_IDEMPOTENT_APIS:
                    logger.error("[%s] 통신 실패 — 재시도하지 않습니다 (중복주문 방지): %s",
                                 api_id, e)
                    raise KiwoomOrderUncertainError(api_id, e) from e

                if attempt < self.max_retries:
                    delay = 1.0 * (2 ** attempt)
                    logger.warning("[%s] 통신 실패(%s), %.1fs 후 재시도", api_id, e, delay)
                    await asyncio.sleep(delay)
                    continue
                raise

        raise last_exc or RuntimeError(f"[{api_id}] 요청 실패")

    async def request_all(
        self,
        api_id: str,
        path: str,
        body: dict | None = None,
        list_keys: tuple[str, ...] = ("result_list",),
    ) -> list[dict]:
        """연속조회(cont-yn / next-key)를 끝까지 따라가며 리스트를 모은다."""
        rows: list[dict] = []
        cont_yn, next_key = "N", ""

        for _ in range(self.MAX_PAGES):
            try:
                data, headers = await self.request(api_id, path, body, cont_yn, next_key)
            except KiwoomAPIError as e:
                if is_empty_result(e.message):
                    logger.debug("[%s] 조회 결과 없음", api_id)
                    break
                raise
            rows.extend(_pick_list(data, *list_keys))

            if headers.get("cont-yn") != "Y":
                break
            cont_yn = "Y"
            next_key = headers.get("next-key", "")
            if not next_key:
                break
        else:
            logger.warning("[%s] 연속조회 상한(%d페이지) 도달", api_id, self.MAX_PAGES)

        return rows

    # ============================================================
    # 주문 — 실시간
    # ============================================================

    async def buy(
        self,
        ticker: str,
        exchange: str,
        qty: int,
        trade_type: str = TradeType.LOC,
        price: float | None = None,
    ) -> OrderResult:
        """미국주식 매수 주문 (ust20000)

        Args:
            trade_type: LOC(30) / LIMIT(00) / MARKET(03) 등.
                        MOC(33)·STOP(34/35)은 매수에서 지원되지 않는다.
            price: 지정가·LOC 계열은 필수. 시장가 계열은 None.
        """
        if trade_type not in BUY_ALLOWED_TRADE_TYPES:
            raise ValueError(
                f"매수에서 지원되지 않는 trde_tp={trade_type} "
                f"(허용: {sorted(BUY_ALLOWED_TRADE_TYPES)})"
            )
        body = {
            "stex_tp": exchange,
            "stk_cd": ticker,
            "ord_qty": str(int(qty)),
            "ord_uv": fmt_price(price) if price is not None else "",
            "trde_tp": trade_type,
        }
        data, _ = await self.request("ust20000", self.PATH_ORDER, body)
        logger.info("매수 접수 %s %s %s주 @%s → ord_no=%s",
                    ticker, trade_type, qty, body["ord_uv"] or "MKT", data.get("ord_no"))
        return OrderResult(str(data.get("ord_no", "")), data.get("stk_nm", ""), data)

    async def sell(
        self,
        ticker: str,
        exchange: str,
        qty: int,
        trade_type: str = TradeType.LOC,
        price: float | None = None,
        stop_price: float | None = None,
    ) -> OrderResult:
        """미국주식 매도 주문 (ust20001)

        매도는 MOC(33)·STOP LIMIT(34)·STOP(35)까지 지원한다.
        """
        body = {
            "stk_cd": ticker,
            "stex_tp": exchange,
            "ord_qty": str(int(qty)),
            "ord_uv": fmt_price(price) if price is not None else "",
            "stop_pric": fmt_price(stop_price) if stop_price is not None else "",
            "trde_tp": trade_type,
        }
        data, _ = await self.request("ust20001", self.PATH_ORDER, body)
        logger.info("매도 접수 %s %s %s주 @%s → ord_no=%s",
                    ticker, trade_type, qty, body["ord_uv"] or "MKT", data.get("ord_no"))
        return OrderResult(str(data.get("ord_no", "")), data.get("stk_nm", ""), data)

    async def cancel(self, orig_ord_no: str, ticker: str, exchange: str) -> dict:
        """미국주식 취소 주문 (ust20003)"""
        body = {
            "orig_ord_no": str(orig_ord_no),
            "stex_tp": exchange,
            "stk_cd": ticker,
        }
        data, _ = await self.request("ust20003", self.PATH_ORDER, body)
        return data

    # ============================================================
    # 주문 — 예약
    # ============================================================

    async def reserve_buy(
        self,
        ticker: str,
        exchange: str,
        qty: int,
        trade_type: str = TradeType.LOC,
        price: float | None = None,
        reserve_type: str = ReserveType.NORMAL,
        start_dt: str = "",
        end_dt: str = "",
    ) -> ReserveResult:
        """미국주식 예약 매수주문 (ust21200)

        주의: 예약주문은 접수 시점에 증거금·잔고·가격제한폭을 검증하지 않는다.
        접수 성공이 체결 대기를 보장하지 않으므로, 반드시
        `get_reserved_orders()` 로 proc_tp / err_cntn 을 확인해야 한다.
        """
        if trade_type not in BUY_ALLOWED_TRADE_TYPES:
            raise ValueError(f"예약 매수에서 지원되지 않는 trde_tp={trade_type}")
        body = {
            "rsrv_ord_tp": reserve_type,
            "rsrv_strt_dt": start_dt,
            "rsrv_end_dt": end_dt,
            "stex_tp": exchange,
            "stk_cd": ticker,
            "ord_qty": str(int(qty)),
            "ord_uv": fmt_price(price) if price is not None else "",
            "trde_tp": trade_type,
        }
        data, _ = await self.request("ust21200", self.PATH_ORDER, body)
        logger.info("예약매수 접수 %s %s %s주 @%s → rsrv_ord_no=%s",
                    ticker, trade_type, qty, body["ord_uv"] or "MKT", data.get("rsrv_ord_no"))
        return ReserveResult(str(data.get("rsrv_ord_no", "")), data.get("frcs_dt", ""), data)

    async def reserve_sell(
        self,
        ticker: str,
        exchange: str,
        qty: int,
        trade_type: str = TradeType.LOC,
        price: float | None = None,
        stop_price: float | None = None,
        reserve_type: str = ReserveType.NORMAL,
        start_dt: str = "",
        end_dt: str = "",
    ) -> ReserveResult:
        """미국주식 예약 매도주문 (ust21201)"""
        body = {
            "rsrv_ord_tp": reserve_type,
            "rsrv_strt_dt": start_dt,
            "rsrv_end_dt": end_dt,
            "stex_tp": exchange,
            "stk_cd": ticker,
            "ord_qty": str(int(qty)),
            "ord_uv": fmt_price(price) if price is not None else "",
            "stop_pric": fmt_price(stop_price) if stop_price is not None else "",
            "trde_tp": trade_type,
        }
        data, _ = await self.request("ust21201", self.PATH_ORDER, body)
        logger.info("예약매도 접수 %s %s %s주 @%s → rsrv_ord_no=%s",
                    ticker, trade_type, qty, body["ord_uv"] or "MKT", data.get("rsrv_ord_no"))
        return ReserveResult(str(data.get("rsrv_ord_no", "")), data.get("frcs_dt", ""), data)

    # 무한매수법 편의 래퍼 --------------------------------------

    async def reserve_loc_buy(self, ticker: str, exchange: str, price: float, qty: int) -> ReserveResult:
        """별지점/평단/폭락대비 LOC 매수"""
        return await self.reserve_buy(ticker, exchange, qty, TradeType.LOC, price)

    async def reserve_loc_sell(self, ticker: str, exchange: str, price: float, qty: int) -> ReserveResult:
        """쿼터 LOC 매도 / 리버스 LOC 매도"""
        return await self.reserve_sell(ticker, exchange, qty, TradeType.LOC, price)

    async def reserve_moc_sell(self, ticker: str, exchange: str, qty: int) -> ReserveResult:
        """리버스모드 첫날 무조건 매도 (MOC=33, 매도 전용)"""
        return await self.reserve_sell(ticker, exchange, qty, TradeType.MOC, price=None)

    async def reserve_limit_sell(
        self, ticker: str, exchange: str, price: float, qty: int, end_dt: str = ""
    ) -> ReserveResult:
        """목표가 지정가매도.

        end_dt 를 주면 기간예약(잔량주문)으로 접수해 여러 거래일에 걸쳐 유지한다.
        """
        if end_dt:
            return await self.reserve_sell(
                ticker, exchange, qty, TradeType.LIMIT, price,
                reserve_type=ReserveType.PERIOD_REMAIN,
                start_dt=datetime.now().strftime("%Y%m%d"),
                end_dt=end_dt,
            )
        return await self.reserve_sell(ticker, exchange, qty, TradeType.LIMIT, price)

    async def cancel_reserved(self, rsrv_dt: str, rsrv_ord_no: str, ticker: str, exchange: str) -> dict:
        """예약주문 취소 (ust21203). 예약 '정정' API 는 없으므로 취소 후 재접수한다."""
        body = {
            "rsrv_dt": rsrv_dt,
            "rsrv_ord_no": str(rsrv_ord_no),
            "stex_tp": exchange,
            "stk_cd": ticker,
        }
        data, _ = await self.request("ust21203", self.PATH_ORDER, body)
        return data

    async def get_reserved_orders(
        self,
        fr_dt: str = "",
        to_dt: str = "",
        ticker: str = "",
        exchange: str = "",
        include_cancelled: bool = False,
    ) -> list[dict]:
        """예약주문 내역조회 (ust21205)

        반환 row 의 유용한 필드:
            rsrv_ord_no, rsrv_cncl_yn, ord_qty, ord_uv,
            proc_tp   0:미처리 / 1:정상처리 / 9:처리중에러
            err_cntn  처리내역 (거부 사유가 여기 들어온다)
        """
        body = {
            "fr_rsrv_dt": fr_dt,
            "to_rsrv_dt": to_dt,
            "fr_rsrv_ord_no": "",
            "rsrv_ord_tp": "0",
            "rsrv_cncl_yn": "0" if include_cancelled else "N",
            "rsrv_proc_tp": "%",
            "slby_tp": SideFilter.ALL,
            "stex_tp": exchange,
            "stk_cd": ticker,
            # 문서상 Required=N 이고 예제도 빈 문자열이지만, 실제로는
            # 비워 보내면 "기준일구분값을 확인하십시요" 로 거부된다.
            # 0:주문전송일 — 그날 실제로 나갈 주문을 보는 것이 목적이다.
            "base_dt_tp": "0",
        }
        return await self.request_all("ust21205", self.PATH_ORDER, body)

    async def verify_reserved_orders(self, ticker: str = "", exchange: str = "") -> list[dict]:
        """접수한 예약주문 중 거부/에러가 난 건만 골라낸다.

        예약주문은 검증 없이 접수되므로 이 확인 단계가 반드시 필요하다.
        """
        rows = await self.get_reserved_orders(ticker=ticker, exchange=exchange,
                                              include_cancelled=True)
        bad = []
        for r in rows:
            proc = str(_pick(r, "proc_tp"))
            cancelled = str(_pick(r, "rsrv_cncl_yn"))
            # proc_tp 는 코드('9') 또는 라벨('처리중에러')로 올 수 있다
            if "에러" in proc or proc == "9" or "취소" in cancelled or "무효" in cancelled:
                bad.append({
                    "rsrv_ord_no": _pick(r, "rsrv_ord_no"),
                    "stk_cd": _pick(r, "stk_cd"),
                    "ord_qty": to_int(_pick(r, "ord_qty")),
                    "ord_uv": to_float(_pick(r, "ord_uv")),
                    "proc_tp": proc,
                    "reason": _pick(r, "err_cntn"),
                    "raw": r,
                })
        return bad

    async def get_orderable(self, ticker: str, exchange: str, price: float) -> dict:
        """주문가능수량 조회 (ust31490)"""
        body = {"stex_tp": exchange, "stk_cd": ticker, "uv": fmt_price(price)}
        data, _ = await self.request("ust31490", self.PATH_ORDER, body)
        return {
            "ord_alowa_100": to_float(data.get("ord_alowa_100")),
            "ord_alowq_100": to_int(data.get("ord_alowq_100")),
            "min_ord_alowa": to_float(data.get("min_ord_alowa")),
            "min_ord_alowq": to_int(data.get("min_ord_alowq")),
            "raw": data,
        }

    # ============================================================
    # 계좌
    # ============================================================

    async def get_balance(self, ticker: str = "", exchange: str = "") -> list[dict]:
        """원장잔고확인 (ust21070) → 보유종목 리스트"""
        body = {"stex_tp": exchange, "stk_cd": ticker}
        rows = await self.request_all("ust21070", self.PATH_ACCOUNT, body)
        return [
            {
                "stk_cd": _pick(r, "stk_cd"),
                "stk_nm": _pick(r, "frgn_stk_nm"),
                "qty": to_int(_pick(r, "qty")),
                "poss_qty": to_int(_pick(r, "poss_qty")),
                "sell_alowq": to_int(_pick(r, "sell_alowq")),
                "avg_price": to_float(_pick(r, "frgn_stk_book_uv")),
                "now_price": to_float(_pick(r, "now_pric")),
                "evlt_amt": to_float(_pick(r, "evlt_amt")),
                "pl_amt": to_signed_float(_pick(r, "pl_amt")),
                "raw": r,
            }
            for r in rows
        ]

    async def get_position(self, ticker: str, exchange: str = "") -> Optional[dict]:
        """특정 종목의 보유 현황. 미보유면 None."""
        for row in await self.get_balance(ticker, exchange):
            if row["stk_cd"].upper() == ticker.upper():
                return row
        return None

    async def get_deposit_usd(self) -> dict:
        """예수금 상세 (ust21160) → USD 예수금

        무한매수법의 '잔금'은 전략 내부 장부로 관리하지만,
        실제 주문 가능 여부를 사전 점검할 때 D0 외화예수금과 대조한다.
        """
        data, _ = await self.request("ust21160", self.PATH_ACCOUNT, {})
        return {
            "d0_usd": to_float(data.get("d0_usd_fx_entr")),
            "d1_usd": to_float(data.get("d1_usd_fx_entr")),
            "d2_usd": to_float(data.get("d2_usd_fx_entr")),
            "usd_krw": to_float(data.get("usd_exch_rate")),
            "raw": data,
        }

    async def get_open_orders(
        self, ticker: str = "", exchange: str = "", ord_dt: str = ""
    ) -> list[dict]:
        """원장 미체결 (ust21050)"""
        body = {
            "ord_dt": ord_dt,
            "slby_tp": SideFilter.ALL,
            "stex_tp": exchange,
            "stk_cd": ticker,
        }
        rows = await self.request_all("ust21050", self.PATH_ACCOUNT, body)
        return [self._normalize_order_row(r) for r in rows]

    async def get_today_orders(self, ticker: str = "", exchange: str = "") -> list[dict]:
        """당일 주문체결 확인 (ust21510)

        WebSocket(F5) 이 끊겼을 때의 대조·복구용. EOD 계산에서
        실시간 수신분과 대조해 누락 체결을 찾아내는 데 쓴다.
        """
        body = {"slby_tp": SideFilter.ALL, "stex_tp": exchange, "stk_cd": ticker}
        rows = await self.request_all("ust21510", self.PATH_ACCOUNT, body)
        return [self._normalize_order_row(r) for r in rows]

    @staticmethod
    def _normalize_order_row(r: dict) -> dict:
        """ust21050 / ust21510 의 공통 필드를 정규화"""
        side_raw = str(_pick(r, "slby_tp"))
        side = "sell" if side_raw.startswith("1") or "매도" in side_raw else "buy"
        return {
            "ord_no": _pick(r, "ord_no"),
            "orig_ord_no": _pick(r, "orig_ord_no"),
            "stk_cd": _pick(r, "stk_cd"),
            "side": side,
            "trade_type": _pick(r, "frgn_trde_tp", "trde_tp"),
            "trade_type_nm": _pick(r, "frgn_trde_nm", "trde_nm"),
            "ord_qty": to_int(_pick(r, "ord_qty")),
            "ord_uv": to_float(_pick(r, "ord_uv")),
            "cntr_qty": to_int(_pick(r, "cntr_qty")),
            "fill_no": _pick(r, "cntr_no", "fill_no", default=""),
            "cntr_uv": to_float(_pick(r, "cntr_uv")),
            "remain_qty": to_int(_pick(r, "ord_remnq")),
            "ord_stat": _pick(r, "ord_stat"),
            "ord_time": _pick(r, "ord_time"),
            "cntr_time": _pick(r, "cntr_time"),
            "is_reserved": "예약" in str(_pick(r, "rsrv_tp")) or str(_pick(r, "rsrv_tp")) == "1",
            "raw": r,
        }

    # ============================================================
    # 시세
    # ============================================================

    async def get_quote(self, ticker: str, exchange: str) -> dict:
        """현재가 종목정보 (usa20100)

        `base_close_pric` 가 전일종가다. 처음매수(전일종가 +15% LOC)와
        방법론 8번(현재가 대비 ±20% 괴리 시 대체주문) 판정에 쓴다.
        """
        body = {"stex_tp": exchange, "stk_cd": ticker}
        data, _ = await self.request("usa20100", self.PATH_MARKET, body)
        return {
            "stk_cd": data.get("stk_cd", ticker),
            "stk_nm": data.get("stk_nm", ""),
            "cur_price": to_float(data.get("cur_prc")),
            "prev_close": to_float(data.get("base_close_pric")),
            "open": to_float(data.get("open_pric")),
            "high": to_float(data.get("high_pric")),
            "low": to_float(data.get("low_pric")),
            "flu_rt": to_signed_float(data.get("flu_rt")),
            "suspended": str(data.get("trd_susp_tp", "0")) not in ("0", ""),
            "usd_krw": to_float(data.get("base_exrt")),
            "raw": data,
        }

    async def get_daily_prices(
        self, ticker: str, exchange: str, base_dt: str = "", limit: int = 10
    ) -> list[dict]:
        """일별주가 (usa20590). 최신일자가 앞에 온다.

        Args:
            base_dt: YYYYMMDD. 이 날짜 '이전' 내역을 조회한다.
        """
        body = {"stex_tp": exchange, "stk_cd": ticker, "base_dt": base_dt}
        rows: list[dict] = []
        cont_yn, next_key = "N", ""

        for _ in range(self.MAX_PAGES):
            data, headers = await self.request("usa20590", self.PATH_MARKET, body, cont_yn, next_key)
            for r in _pick_list(data):
                rows.append({
                    "dt": r.get("dt", ""),
                    "close": to_float(r.get("cur_prc")),
                    "open": to_float(r.get("open_pric")),
                    "high": to_float(r.get("high_pric")),
                    "low": to_float(r.get("low_pric")),
                    "volume": to_int(r.get("acc_trde_qty")),
                })
                if len(rows) >= limit:
                    return rows
            if headers.get("cont-yn") != "Y" or not headers.get("next-key"):
                break
            cont_yn, next_key = "Y", headers["next-key"]

        return rows

    async def get_prev_close(self, ticker: str, exchange: str) -> float:
        """전일 종가 (처음매수 +15% LOC 기준가)"""
        quote = await self.get_quote(ticker, exchange)
        if quote["prev_close"] > 0:
            return quote["prev_close"]
        # 폴백: 일 차트 최신 1건
        try:
            daily = await self.get_daily_chart(ticker, exchange, limit=1)
        except KiwoomAPIError:
            daily = await self.get_daily_prices(ticker, exchange, limit=1)
        return daily[0]["close"] if daily else 0.0

    async def get_daily_chart(
        self, ticker: str, exchange: str, limit: int = 10, adjusted: bool = True
    ) -> list[dict]:
        """미국주식 일 차트 (usa06012). 최신일자가 앞에 온다.

        일별주가(usa20590) 대신 이쪽을 주 경로로 쓴다. usa20590 에는
        수정주가 옵션이 없기 때문이다.

        TQQQ·SOXL 은 액면분할 이력이 있어서, 수정주가를 적용하지 않으면
        분할이 낀 구간에서 과거 종가가 현재 주가 체계와 달라진다.
        리버스 별지점(직전 5거래일 종가 평균)을 오늘 주가와 비교하므로
        같은 기준이어야 한다.

        Args:
            adjusted: 수정주가 적용 여부 (upd_stkpc_tp)
        """
        # 미국 휴장일을 감안해 필요 거래일보다 넉넉히 조회한다
        start = (datetime.now() - timedelta(days=limit * 2 + 10)).strftime("%Y%m%d")
        body = {
            "stex_tp": exchange,
            "stk_cd": ticker,
            "strt_dt": start,
            "upd_stkpc_tp": "1" if adjusted else "0",
            "exrt_appl_tp": "0",      # 환율 미적용 (USD 원본)
        }
        data, _ = await self.request("usa06012", self.PATH_CHART, body)
        rows = []
        for r in _pick_list(data)[:limit]:
            rows.append({
                "dt": r.get("dt", ""),
                "close": to_float(r.get("cur_prc")),
                "open": to_float(r.get("open_pric")),
                "high": to_float(r.get("high_pric")),
                "low": to_float(r.get("low_pric")),
                "volume": to_int(r.get("acc_trde_qty")),
            })
        return rows

    async def get_recent_closes(self, ticker: str, exchange: str, n: int = 5) -> list[float]:
        """직전 n거래일 종가 (최신순). 리버스모드 별지점 계산용.

        수정주가가 적용되는 일 차트를 먼저 쓰고, 실패하면 일별주가로
        물러난다.
        """
        try:
            daily = await self.get_daily_chart(ticker, exchange, limit=n)
        except KiwoomAPIError as e:
            logger.warning("일 차트 조회 실패(%s) — 일별주가로 대체: %s", ticker, e)
            daily = await self.get_daily_prices(ticker, exchange, limit=n)
        closes = [d["close"] for d in daily if d["close"] > 0]
        if len(closes) < n:
            logger.warning("%s 최근 종가 %d건만 조회됨 (요청 %d건)", ticker, len(closes), n)
        return closes[:n]

    async def get_reverse_star_point(self, ticker: str, exchange: str) -> float:
        """리버스모드 별지점 = 직전 5거래일 종가의 평균

        일반모드 별지점(평단 × 별%)과는 정의가 전혀 다르다.
        평단 대비 -15% / -20% 는 리버스 '종료조건'이지 별지점이 아니다.
        """
        closes = await self.get_recent_closes(ticker, exchange, 5)
        if len(closes) < 5:
            raise KiwoomAPIError("usa20590", -1,
                                 f"{ticker} 직전 5거래일 종가를 확보하지 못함 ({len(closes)}건)")
        return round(sum(closes) / 5, 2)

    # ============================================================
    # 거래소 판별
    # ============================================================

    # ============================================================
    # 구버전 호환 별칭 (점진적 교체용 — 신규 코드에서는 쓰지 말 것)
    # ============================================================

    async def get_current_price(self, ticker: str) -> float:
        """구버전 호환. 내부적으로 usa20100 을 호출한다."""
        quote = await self.get_quote(ticker, exchange_of(ticker))
        return quote["cur_price"]

    async def get_reserv_orders(
        self, fr_rsrv_dt: str = "", to_rsrv_dt: str = "", ticker: str = ""
    ) -> list[dict]:
        """구버전 호환 → get_reserved_orders()"""
        return await self.get_reserved_orders(fr_rsrv_dt, to_rsrv_dt, ticker)

    async def cancel_reserv_order(self, rsrv_dt: str, rsrv_ord_no: str, ticker: str) -> dict:
        """구버전 호환 → cancel_reserved()"""
        return await self.cancel_reserved(rsrv_dt, rsrv_ord_no, ticker, exchange_of(ticker))

    # ============================================================
    # 거래소 판별
    # ============================================================

    async def resolve_exchange(self, ticker: str) -> str:
        """티커의 stex_tp 를 실제 조회로 판별한다.

        TQQQ 는 NASDAQ(ND) 로 확정이지만 SOXL 은 NYSE Arca 상장이라
        키움이 NY 로 잡는지 NA 로 잡는지 문서만으로는 알 수 없다.
        운영 전에 한 번 호출해 결과를 상수로 고정해두는 용도.
        """
        for ex in Exchange.ALL:
            try:
                q = await self.get_quote(ticker, ex)
                if q["cur_price"] > 0 or q["prev_close"] > 0:
                    logger.info("%s 거래소 판별: %s", ticker, ex)
                    return ex
            except KiwoomAPIError:
                continue
        raise KiwoomAPIError("usa20100", -1, f"{ticker} 거래소 판별 실패")
