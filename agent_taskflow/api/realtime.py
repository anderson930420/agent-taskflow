"""Server-Sent Events transport for V1 Step 3 (SPEC §15, §15.1).

    Runtime -> SQLite state / ObservedStep -> SSE -> Mission Control

Reconnect semantics come straight from §15.1 and are deliberately minimal:

* every connection begins by sending a **full state snapshot**;
* then it streams updates;
* there is no event replay, no ``Last-Event-ID``, and no missed-event backfill.

Because the snapshot is what makes reconnect correct, the stream never emits an
``id:`` line at all — a client cannot form a resume contract that this server
would then have to honour.

The stream is a projection of persisted state (§44). It reads; it never writes,
never decides lifecycle, and never contacts GitHub.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse


SSE_EVENT_SNAPSHOT = "snapshot"
SSE_EVENT_UPDATE = "update"

SSE_MEDIA_TYPE = "text/event-stream"

#: Buffering must be off end-to-end or events arrive in batches.
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

DEFAULT_POLL_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class RealtimeStreamOptions:
    """Stream tuning, injectable so tests can bound an endless stream.

    ``max_updates`` and ``max_polls`` are ``None`` in production — the stream
    runs until the client goes away. ``on_poll`` is an observation seam used by
    tests to change persisted state at a deterministic point in the loop; it is
    never set by the default app.
    """

    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS
    max_updates: int | None = None
    max_polls: int | None = None
    on_poll: Callable[[int], None] | None = None


def format_sse(event: str, data: Any) -> str:
    """Encode one SSE frame. Never emits an ``id:`` line (§15.1)."""

    payload = json.dumps(data, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


class RealtimeEventSource:
    """Turns a snapshot builder into a snapshot-first SSE frame sequence."""

    def __init__(
        self,
        build_snapshot: Callable[[], Any],
        *,
        max_updates: int | None = None,
    ) -> None:
        self._build_snapshot = build_snapshot
        self._max_updates = max_updates
        self._sent_updates = 0
        self._last_encoded: str | None = None

    @property
    def exhausted(self) -> bool:
        return (
            self._max_updates is not None
            and self._sent_updates >= self._max_updates
        )

    def initial_event(self) -> str:
        """Return the full state snapshot that opens every connection."""

        data = self._build_snapshot()
        self._last_encoded = json.dumps(data, default=str, sort_keys=True)
        return format_sse(SSE_EVENT_SNAPSHOT, data)

    def poll(self) -> str | None:
        """Return an update frame if persisted state changed, else ``None``."""

        if self.exhausted:
            return None
        data = self._build_snapshot()
        encoded = json.dumps(data, default=str, sort_keys=True)
        if encoded == self._last_encoded:
            return None
        self._last_encoded = encoded
        self._sent_updates += 1
        return format_sse(SSE_EVENT_UPDATE, data)


def iter_realtime_events(
    source: RealtimeEventSource,
    *,
    sleep: Callable[[float], None],
    interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_polls: int | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Iterator[str]:
    """Synchronous frame generator. The async route mirrors this loop."""

    yield source.initial_event()
    polls = 0
    while not source.exhausted:
        if max_polls is not None and polls >= max_polls:
            return
        if should_stop is not None and should_stop():
            return
        sleep(interval)
        polls += 1
        frame = source.poll()
        if frame is not None:
            yield frame


async def _aiter_realtime_events(
    request: Request,
    source: RealtimeEventSource,
    options: RealtimeStreamOptions,
) -> AsyncIterator[str]:
    yield await run_in_threadpool(source.initial_event)
    polls = 0
    while not source.exhausted:
        if options.max_polls is not None and polls >= options.max_polls:
            return
        if await request.is_disconnected():
            return
        if options.poll_interval > 0:
            await asyncio.sleep(options.poll_interval)
        polls += 1
        if options.on_poll is not None:
            options.on_poll(polls)
        frame = await run_in_threadpool(source.poll)
        if frame is not None:
            yield frame


def realtime_stream_response(
    request: Request,
    build_snapshot: Callable[[], Any],
    options: RealtimeStreamOptions,
) -> StreamingResponse:
    """Return the §15 SSE response for a snapshot builder."""

    source = RealtimeEventSource(build_snapshot, max_updates=options.max_updates)
    return StreamingResponse(
        _aiter_realtime_events(request, source, options),
        media_type=SSE_MEDIA_TYPE,
        headers=dict(SSE_HEADERS),
    )


__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "SSE_EVENT_SNAPSHOT",
    "SSE_EVENT_UPDATE",
    "SSE_HEADERS",
    "SSE_MEDIA_TYPE",
    "RealtimeEventSource",
    "RealtimeStreamOptions",
    "format_sse",
    "iter_realtime_events",
    "realtime_stream_response",
]
