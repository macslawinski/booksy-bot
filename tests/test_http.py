"""Contract tests for the HTTP core. Copied next to http.py's tests; run with pytest (no async plugin needed)."""
import asyncio
import logging

import httpx
import pytest
from pydantic import BaseModel

from booksy_bot.api.http import ApiChanged, ApiError, HttpCore, RateLimited, SessionExpired, mask

SECRET = "tok_SECRET_123456"


class Ok(BaseModel):
    ok: bool


def make_core(handler, sleeps, **kw):
    return HttpCore("https://api.test", {"X-Api-Key": "k"}, token_provider=lambda: SECRET, token_header="X-Access-Token",
                    transport=httpx.MockTransport(handler), sleep=lambda s: _record(sleeps, s), **kw)


async def _record(sleeps, s):
    sleeps.append(s)


def run(coro):
    return asyncio.run(coro)


def test_headers_from_one_place_and_parse():
    seen = {}

    def handler(req):
        seen.update(req.headers)
        return httpx.Response(200, json={"ok": True, "extra": 1})

    assert run(make_core(handler, []).request("GET", "/x", Ok)).ok is True
    assert seen["x-api-key"] == "k" and seen["x-access-token"] == SECRET


def test_429_respects_retry_after_then_succeeds():
    calls, sleeps = [], []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "7"}) if len(calls) == 1 else httpx.Response(200, json={"ok": True})

    assert run(make_core(handler, sleeps).request("POST", "/book", Ok, json_body={})).ok
    assert sleeps == [7.0]


def test_429_gives_up():
    with pytest.raises(RateLimited):
        run(make_core(lambda r: httpx.Response(429), [], max_retries=2).request("GET", "/x", Ok))


def test_backoff_grows_on_5xx_for_get():
    sleeps = []
    with pytest.raises(ApiError):
        run(make_core(lambda r: httpx.Response(503), sleeps, max_retries=3, backoff_base=1).request("GET", "/x", Ok))
    assert len(sleeps) == 3 and sleeps[0] <= 1 and 1 <= sleeps[1] <= 2 and 2 <= sleeps[2] <= 4


def test_post_not_resent_after_read_timeout():
    calls = []

    def handler(req):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(ApiError, match="outcome unknown"):
        run(make_core(handler, []).request("POST", "/book", Ok, json_body={}))
    assert len(calls) == 1  # resending a booking could book twice


def test_post_resent_after_connect_error():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("down", request=req)
        return httpx.Response(200, json={"ok": True})

    assert run(make_core(handler, []).request("POST", "/book", Ok, json_body={})).ok and len(calls) == 2


def test_session_expired():
    with pytest.raises(SessionExpired):
        run(make_core(lambda r: httpx.Response(401), []).request("GET", "/x", Ok))


def test_api_changed_on_schema_mismatch():
    with pytest.raises(ApiChanged):
        run(make_core(lambda r: httpx.Response(200, json={"renamed": True}), []).request("GET", "/x", Ok))


def test_logs_never_contain_secrets(caplog):
    caplog.set_level(logging.DEBUG, logger="api")
    run(make_core(lambda r: httpx.Response(200, json={"ok": True, "access_token": SECRET, "email": "a@b.pl"}), [])
        .request("POST", "/login", Ok, json_body={"password": "hunter2", "email": "a@b.pl"}))
    assert caplog.text and SECRET not in caplog.text and "hunter2" not in caplog.text and "a@b.pl" not in caplog.text


def test_mask():
    assert mask({"X-Access-Token": "t", "nested": {"password": "p", "name": "ok"}}) == {"X-Access-Token": "***", "nested": {"password": "***", "name": "ok"}}
