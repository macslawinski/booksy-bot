"""Minimal Telegram Bot API client (long polling). The bot token is part of every URL, so errors never include the URL."""
import httpx


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(base_url=f"https://api.telegram.org/bot{token}/",
                                         timeout=httpx.Timeout(40.0, connect=5.0), transport=transport)

    async def call(self, method: str, **params):
        try:
            data = (await self._client.post(method, json=params)).json()
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramError(f"{method}: {type(e).__name__}") from None  # `from None`: chained error would carry the token URL
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', 'unknown error')}")
        return data["result"]

    async def send(self, chat_id: int, text: str, buttons: list[list[dict]] | None = None) -> None:
        extra = {"reply_markup": {"inline_keyboard": buttons[:90]}} if buttons else {}  # Telegram caps keyboards at 100 buttons
        await self.call("sendMessage", chat_id=chat_id, text=text[:4000], parse_mode="HTML",
                        disable_web_page_preview=True, **extra)

    async def updates(self, offset: int | None) -> list[dict]:
        return await self.call("getUpdates", offset=offset, timeout=30, allowed_updates=["message", "callback_query"])

    async def aclose(self) -> None:
        await self._client.aclose()
