"""HTTP core for the service client: the ONLY place that knows headers, retries, 429, session expiry and log masking.

Endpoints live in client.py and call `HttpCore.request(...)`; they never build headers or catch HTTP errors themselves.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

log = logging.getLogger("api")
M = TypeVar("M", bound=BaseModel)


class ApiError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class SessionExpired(ApiError):
    """Token/cookie no longer valid. Login may need a human (CAPTCHA), so the caller decides what to do."""


class RateLimited(ApiError):
    """429 still returned after all retries."""


class ApiChanged(ApiError):
    """Response no longer matches our model: the service changed its API. Alert, do not retry."""


# ---------- secret masking (logs only; fixtures are masked by har-analysis) ----------

_SENSITIVE = re.compile(r"auth|token|key|secret|session|cookie|csrf|xsrf|signature|fingerprint|captcha|pass|pwd|"
                        r"e-?mail|phone|mobile|pesel|first_?name|last_?name|address|card|iban", re.I)
_JWT = re.compile(r"eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]*")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def mask(value: Any) -> Any:
    """Mask secrets in headers/JSON/text before logging. Keys decide; regexes catch leftovers."""
    if isinstance(value, Mapping):
        return {k: "***" if _SENSITIVE.search(str(k)) and v not in (None, "") else mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    if isinstance(value, str):
        return _EMAIL.sub("***@***", _JWT.sub("***jwt***", value))
    return value


def _mask_body(content: bytes | str | None, limit: int = 2000) -> str:
    if not content:
        return ""
    text = content.decode("utf-8", "replace") if isinstance(content, bytes) else content
    try:
        return json.dumps(mask(json.loads(text)), ensure_ascii=False)[:limit]
    except ValueError:
        return mask(text)[:limit]


# ---------- core ----------

_SAFE_TO_RESEND = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)  # request never reached the server
_MAYBE_SENT = (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.RemoteProtocolError)


class HttpCore:
    def __init__(
        self,
        base_url: str,
        static_headers: Mapping[str, str],
        token_provider: Callable[[], str | None] = lambda: None,
        token_header: str = "Authorization",
        is_session_expired: Callable[[httpx.Response], bool] = lambda r: r.status_code == 401,
        timeout: httpx.Timeout = httpx.Timeout(15.0, connect=5.0),
        max_retries: int = 4,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ):
        self._client = httpx.AsyncClient(base_url=base_url, headers=dict(static_headers), timeout=timeout,
                                         transport=transport, follow_redirects=False)
        self._token_provider = token_provider
        self._token_header = token_header
        self._is_session_expired = is_session_expired
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep

    async def aclose(self) -> None:
        await self._client.aclose()

    def _delay(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), self._backoff_cap)
            except ValueError:
                pass  # HTTP-date form: fall back to backoff
        exp = min(self._backoff_cap, self._backoff_base * 2 ** attempt)
        return exp / 2 + random.uniform(0, exp / 2)  # jitter spreads retries

    async def request(
        self,
        method: str,
        path: str,
        model: type[M],
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        idempotent: bool | None = None,
        retries: int | None = None,
    ) -> M:
        """Send, retry what is safe, map errors, parse into `model`.

        idempotent=False (default for non-GET) means: never resend after the request may have reached the server
        (read timeout, dropped connection, 5xx) — resending a booking could book twice. 429 and connect errors are
        always safe to resend because the server did not act.
        """
        method = method.upper()
        if idempotent is None:
            idempotent = method in ("GET", "HEAD", "OPTIONS")
        headers = {}
        token = self._token_provider()
        if token:
            headers[self._token_header] = token

        max_retries = self._max_retries if retries is None else retries   # 0 = fail fast (booking window)
        for attempt in range(max_retries + 1):
            last = attempt == max_retries
            t0 = time.monotonic()
            try:
                resp = await self._client.request(method, path, params=params, json=json_body, headers=headers)
            except _SAFE_TO_RESEND as e:
                if last:
                    raise ApiError(f"{method} {path}: connection failed: {e!r}") from e
                log.warning("%s %s connect error (%s), retry %d", method, path, type(e).__name__, attempt + 1)
                await self._sleep(self._delay(attempt))
                continue
            except _MAYBE_SENT as e:
                if not idempotent or last:
                    raise ApiError(f"{method} {path}: {type(e).__name__}, outcome unknown") from e
                log.warning("%s %s %s, retry %d", method, path, type(e).__name__, attempt + 1)
                await self._sleep(self._delay(attempt))
                continue

            ms = (time.monotonic() - t0) * 1000
            log.info("%s %s -> %d (%.0f ms)", method, path, resp.status_code, ms)
            if log.isEnabledFor(logging.DEBUG):
                log.debug("request headers=%s body=%s", mask(dict(resp.request.headers)), _mask_body(resp.request.content))
                log.debug("response body=%s", _mask_body(resp.content))

            if self._is_session_expired(resp):
                raise SessionExpired(f"{method} {path}: session expired", resp.status_code)
            if resp.status_code == 429:
                if last:
                    raise RateLimited(f"{method} {path}: 429 after {attempt + 1} attempts", 429)
                delay = self._delay(attempt, resp.headers.get("Retry-After"))
                log.warning("%s %s 429, waiting %.1fs", method, path, delay)
                await self._sleep(delay)
                continue
            if resp.status_code >= 500 and idempotent and not last:
                log.warning("%s %s %d, retry %d", method, path, resp.status_code, attempt + 1)
                await self._sleep(self._delay(attempt))
                continue
            if resp.status_code >= 400:
                raise ApiError(f"{method} {path}: HTTP {resp.status_code}: {_mask_body(resp.content, 500)}", resp.status_code)

            try:
                return model.model_validate(resp.json())
            except (ValueError, ValidationError) as e:
                raise ApiChanged(f"{method} {path}: response does not match {model.__name__}: {e}", resp.status_code) from e
        raise AssertionError("unreachable")
