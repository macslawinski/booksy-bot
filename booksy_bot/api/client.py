"""Booksy customer API: one method per endpoint. No headers, retries or error handling here — that lives in http.py."""
from datetime import date

from booksy_bot.api.http import HttpCore
from booksy_bot.api.models import BusinessResponse, TimeSlotsResponse

BASE_URL = "https://pl.booksy.com/core/v2/customer_api"
ANY_STAFFER = -1


class BooksyClient:
    def __init__(self, http: HttpCore):
        self._http = http

    async def business(self, business_id: int) -> BusinessResponse:
        """Salon details: staff and services (variant.id is the service_variant_id used by time_slots)."""
        return await self._http.request("GET", f"/businesses/{business_id}/", BusinessResponse)

    async def time_slots(self, business_id: int, service_variant_id: int, start: date, end: date,
                         staffer_id: int = ANY_STAFFER) -> TimeSlotsResponse:
        body = {"subbookings": [{"service_variant_id": service_variant_id, "staffer_id": staffer_id, "combo_children": []}],
                "start_date": start.isoformat(), "end_date": end.isoformat()}
        return await self._http.request("POST", f"/me/businesses/{business_id}/appointments/time_slots",
                                        TimeSlotsResponse, json_body=body, idempotent=True)  # read-only POST: safe to retry
