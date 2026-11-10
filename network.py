"""Synchronous access to HTTPX's pooled AnyIO/Happy Eyeballs transport.

Keep all async HTTP work on one event-loop thread. Only TCP connection attempts
race; requests are never replayed by this adapter.
"""

from contextlib import contextmanager
from functools import partial
from typing import Any, Iterator

import httpx
from anyio.from_thread import start_blocking_portal

CONNECT_TIMEOUT = 10.0


class _ResponseStream(httpx.SyncByteStream):
    def __init__(self, portal, response: httpx.Response):
        self.portal = portal
        self.response = response
        self.iterator = response.aiter_raw().__aiter__()

    def __iter__(self) -> Iterator[bytes]:
        while True:
            try:
                chunk = self.portal.call(anext, self.iterator)
            except StopAsyncIteration:
                return
            yield chunk

    def close(self) -> None:
        self.portal.call(self.response.aclose)


class HTTPClient:
    def __init__(self, **options: Any):
        self._manager = start_blocking_portal(name="chatgpt-web-http")
        self._portal = self._manager.__enter__()
        self._closed = False
        try:
            self._client = self._portal.call(partial(httpx.AsyncClient, **options))
        except BaseException:
            self._manager.__exit__(None, None, None)
            raise

    def request(self, method: str, url: str, **options: Any) -> httpx.Response:
        return self._portal.call(partial(self._client.request, method, url, **options))

    def get(self, url: str, **options: Any) -> httpx.Response:
        return self.request("GET", url, **options)

    def put(self, url: str, **options: Any) -> httpx.Response:
        return self.request("PUT", url, **options)

    @contextmanager
    def stream(self, method: str, url: str, **options: Any) -> Iterator[httpx.Response]:
        async def open_response():
            request = self._client.build_request(method, url, **options)
            return await self._client.send(request, stream=True)

        remote = self._portal.call(open_response)
        response = httpx.Response(
            remote.status_code, headers=remote.headers, request=remote.request,
            extensions=remote.extensions, stream=_ResponseStream(self._portal, remote),
        )
        try:
            yield response
        finally:
            response.close()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._portal.call(self._client.aclose)
        finally:
            self._closed = True
            self._manager.__exit__(None, None, None)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def connection_progress(on_event, stage: str) -> dict[str, Any]:
    """Only forward fixed stage names, never HTTP trace arguments or headers."""
    if on_event is None:
        return {}

    async def trace(name, _info):
        if name in {"connection.connect_tcp.started", "connection.start_tls.started"}:
            on_event({"type": "status", "stage": "connecting"})
        elif name in {"connection.connect_tcp.complete", "connection.start_tls.complete"}:
            on_event({"type": "status", "stage": stage})

    return {"trace": trace}
