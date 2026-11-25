import httpx

from booksy_bot.api.client import BASE_URL, BooksyClient
from booksy_bot.api.http import HttpCore


async def _no_sleep(_):
    pass


def make_client(handler) -> BooksyClient:
    """Client wired to httpx.MockTransport: no network in tests."""
    return BooksyClient(HttpCore(BASE_URL, {"X-Api-Key": "test-key"}, token_provider=lambda: "test-token",
                                 token_header="X-Access-Token", transport=httpx.MockTransport(handler), sleep=_no_sleep))
