"""Booksy customer API: one method per endpoint. No headers, retries or error handling here — that lives in http.py."""
from datetime import date, datetime

from booksy_bot.api.http import HttpCore
from booksy_bot.api.models import Ack, AppointmentDetailsResponse, BookingsResponse, AppointmentResponse, BusinessResponse, TimeSlotsResponse
from booksy_bot.timezones import service_wall_clock

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

    @staticmethod
    def _booking_body(service_variant_id: int, staffer_id: int, start: datetime) -> dict:
        return {"subbookings": [{"staffer_id": staffer_id, "booked_from": service_wall_clock(start),
                                 "service_variant": {"mode": "variant", "id": service_variant_id}, "combo_children": []}],
                "is_cancellation_fee_preauth_flow": False,
                # widget question "used this salon before?"; True: the bot books salons people already go to
                "recurring": True, "pre_auth_type": "control", "compatibilities": {"prepayment": True}}

    async def dry_run(self, business_id: int, service_variant_id: int, staffer_id: int, start: datetime) -> AppointmentResponse:
        """Validate a booking (slot still free, price, payment) without booking anything. 400 = slot no longer available."""
        return await self._http.request("POST", f"/me/appointments/business/{business_id}/dry_run/", AppointmentResponse,
                                        json_body=self._booking_body(service_variant_id, staffer_id, start), idempotent=True)

    async def book(self, business_id: int, service_variant_id: int, staffer_id: int, start: datetime) -> AppointmentResponse:
        """REAL booking. Never resent after a possibly-delivered request (idempotent=False): could book twice."""
        return await self._http.request("POST", f"/me/appointments/business/{business_id}/", AppointmentResponse,
                                        json_body=self._booking_body(service_variant_id, staffer_id, start), idempotent=False)

    async def appointment(self, appointment_uid: int | str) -> AppointmentDetailsResponse:
        """One of my appointments: status, whether it can still be cancelled, _version for the cancel action."""
        return await self._http.request("GET", f"/me/appointments/{appointment_uid}/", AppointmentDetailsResponse)

    async def cancel(self, appointment_uid: int | str, version: int) -> Ack:
        """REAL cancel (found in booksy.com's JS 2026-11-26). Not resent after an unknown outcome; check with appointment()."""
        return await self._http.request("POST", f"/me/appointments/{appointment_uid}/action/", Ack,
                                        json_body={"action": "cancel", "_version": version, "no_thumbs": "true"},
                                        idempotent=False)

    async def my_bookings(self) -> BookingsResponse:
        """My upcoming visits, booked by the bot or by hand (found in booksy.com's JS 2026-11-26)."""
        return await self._http.request("GET", "/me/bookings", BookingsResponse,
                                        params={"bookings_page": 1, "bookings_per_page": 100, "state": "active"})
