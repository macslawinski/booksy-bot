import asyncio
import json
from datetime import date, datetime, time, timezone
from pathlib import Path

import httpx
import pytest

from booksy_bot.api.http import ApiChanged
from conftest import make_client

FIX = json.loads((Path(__file__).parent / "fixtures/time_slots.json").read_text(encoding="utf-8"))


def test_time_slots_parses_real_response():
    seen = {}

    def handler(req):
        seen["method"], seen["path"], seen["body"] = req.method, req.url.path, json.loads(req.content)
        seen["token"] = req.headers.get("X-Access-Token")
        return httpx.Response(200, json=FIX)

    r = asyncio.run(make_client(handler).time_slots(100001, 300001, date(2026, 11, 27), date(2026, 11, 30), staffer_id=200001))

    assert [d.day for d in r.days] == [date(2026, 11, 27), date(2026, 11, 28)]
    assert r.days[0].slots[0].at == time(14, 0) and len(r.days[0].slots) == 7
    assert r.days[1].slots[-1].at == time(10, 30)
    assert r.days[0].starts()[0].astimezone(timezone.utc) == datetime(2026, 11, 27, 13, 0, tzinfo=timezone.utc)  # CET +1

    assert seen["method"] == "POST"
    assert seen["path"] == "/core/v2/customer_api/me/businesses/100001/appointments/time_slots"
    assert seen["body"] == {"subbookings": [{"service_variant_id": 300001, "staffer_id": 200001, "combo_children": []}],
                            "start_date": "2026-11-27", "end_date": "2026-11-30"}
    assert seen["token"] == "test-token"


def test_time_slots_any_staffer_by_default():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"time_slots": []})

    r = asyncio.run(make_client(handler).time_slots(1, 2, date(2026, 11, 27), date(2026, 11, 27)))
    assert r.days == [] and seen["body"]["subbookings"][0]["staffer_id"] == -1


def test_time_slots_detects_api_change():
    client = make_client(lambda req: httpx.Response(200, json={"time_slots": [{"date": "2026-11-27", "slots": [{"time": "14:00"}]}]}))
    with pytest.raises(ApiChanged):
        asyncio.run(client.time_slots(100001, 300001, date(2026, 11, 27), date(2026, 11, 30)))


def test_time_slots_retried_after_server_error():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json=FIX)

    asyncio.run(make_client(handler).time_slots(100001, 300001, date(2026, 11, 27), date(2026, 11, 30)))
    assert len(calls) == 2  # idempotent=True: read-only POST may be resent
