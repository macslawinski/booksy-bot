import argparse
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from booksy_bot.api.http import ApiChanged
from booksy_bot.cli import business_id_arg
from conftest import make_client

FIX = json.loads((Path(__file__).parent / "fixtures/business.json").read_text(encoding="utf-8"))


def test_business_parses_real_response():
    seen = {}

    def handler(req):
        seen["method"], seen["path"] = req.method, req.url.path
        return httpx.Response(200, json=FIX)

    b = asyncio.run(make_client(handler).business(100001)).business

    assert seen == {"method": "GET", "path": "/core/v2/customer_api/businesses/100001/"}
    assert b.id == 100001 and [s.id for s in b.staff] == [200001]
    services = b.bookable_services()
    assert [s.name for s in services] == ["Strzyżenie Męskie", "Strzyżenie Meskie Maszynka", "Strzyżenie Dziecięce"]
    v = services[0].variants[0]
    assert (v.id, v.duration, v.price, v.service_price, v.staffer_ids) == (300001, 30, 60.0, "60,00\xa0zł", [200001])  # Booksy uses a non-breaking space
    assert v.needs_payment is False


def test_prepayment_or_deposit_flags_variant():
    fix = json.loads(json.dumps(FIX))
    fix["business"]["service_categories"][0]["services"][0]["variants"][0]["deposit"] = 20.0
    b = asyncio.run(make_client(lambda req: httpx.Response(200, json=fix)).business(100001)).business
    assert b.bookable_services()[0].variants[0].needs_payment is True


def test_inactive_services_hidden():
    fix = json.loads(json.dumps(FIX))
    fix["business"]["service_categories"][0]["services"][1]["is_available_for_customer_booking"] = False
    b = asyncio.run(make_client(lambda req: httpx.Response(200, json=fix)).business(100001)).business
    assert [s.id for s in b.bookable_services()] == [400001, 400003]


def test_business_detects_api_change():
    broken = {"business": {"id": 1, "name": "x", "staff": [], "services": []}}  # service_categories renamed
    with pytest.raises(ApiChanged):
        asyncio.run(make_client(lambda req: httpx.Response(200, json=broken)).business(1))


@pytest.mark.parametrize("value,expected", [
    ("100001", 100001),
    ("https://booksy.com/pl-pl/100001_salon-testowy_fryzjer_10729_warszawa#ba_s=ar_1", 100001),
    ("https://booksy.com/pl-pl/dl/show-business/278383", 278383),  # "Udostępnij" from the Booksy app
])
def test_business_id_from_id_or_url(value, expected):
    assert business_id_arg(value) == expected


def test_business_id_rejects_garbage():
    with pytest.raises(argparse.ArgumentTypeError):
        business_id_arg("salon")
