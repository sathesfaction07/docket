"""Live change feed: every successful write becomes an event pushed to connected clients.

Design, and what each part protects against:

* **Ordered, replayable ids.** Each durable event gets ``id = "<epoch>:<seq>"``. A client that
  drops its connection reconnects with ``Last-Event-ID`` and is replayed exactly the events it
  missed from a ring buffer, so a flaky network does not mean a stale screen.
* **Honest gaps.** If the missed events have been evicted from the buffer, or the server
  restarted (new ``epoch``, so ``seq`` restarted too), replay is impossible. The client gets a
  ``resync`` event and reloads, instead of silently missing changes.
* **Slow consumers cannot hurt anyone else.** Each subscriber has a bounded queue. One that
  falls too far behind is marked overflowed and sent ``resync``; publishers never block.
* **Thread-safe publish.** FastAPI runs sync endpoints in a thread pool, so ``publish`` takes a
  lock and hands events to each subscriber's event loop with ``call_soon_threadsafe``.
* **Presence.** Subscribing and unsubscribing broadcast who is online. Presence events are
  ephemeral (no id, not buffered), so they never count as a missed change.

Scope: one feed per process. Running several API instances needs a shared feed (DynamoDB
Streams, Firestore listeners, or Redis pub/sub) behind the same ``publish``/``subscribe``
interface; see ARCHITECTURE.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import threading
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(eq=False)
class Subscription:
    user: str
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue
    start_seq: int
    overflowed: bool = False
    _closed: bool = field(default=False, repr=False)


class ChangeFeed:
    def __init__(self, capacity: int = 1000, queue_size: int = 500):
        self.epoch = secrets.token_hex(4)
        self._lock = threading.Lock()
        self._seq = 0
        self._buffer: deque[dict] = deque(maxlen=capacity)
        self._subs: list[Subscription] = []
        self._queue_size = queue_size

    # -- publishing ----------------------------------------------------------------
    def publish(self, type_: str, actor: str, **payload) -> dict:
        """Record and broadcast a durable event. Safe to call from any thread."""
        with self._lock:
            self._seq += 1
            event = {
                "id": f"{self.epoch}:{self._seq}",
                "seq": self._seq,
                "type": type_,
                "actor": actor,
                "at": datetime.now(UTC).isoformat(),
                **payload,
            }
            self._buffer.append(event)
            subs = list(self._subs)
        self._fan_out(subs, event)
        return event

    def _broadcast_ephemeral(self, event: dict, exclude: Subscription | None = None) -> None:
        with self._lock:
            subs = [s for s in self._subs if s is not exclude]
        self._fan_out(subs, event)

    def _fan_out(self, subs: list[Subscription], event: dict) -> None:
        for sub in subs:
            # A closed loop raises RuntimeError; that subscriber is about to unsubscribe.
            with contextlib.suppress(RuntimeError):
                sub.loop.call_soon_threadsafe(self._offer, sub, event)

    @staticmethod
    def _offer(sub: Subscription, event: dict) -> None:
        if sub._closed:
            return
        try:
            sub.queue.put_nowait(event)
        except asyncio.QueueFull:
            sub.overflowed = True

    # -- subscribing ---------------------------------------------------------------
    def subscribe(self, user: str) -> Subscription:
        """Register the calling coroutine's loop. Must be called from inside a running loop."""
        sub = Subscription(
            user=user,
            loop=asyncio.get_running_loop(),
            queue=asyncio.Queue(maxsize=self._queue_size),
            start_seq=0,
        )
        with self._lock:
            sub.start_seq = self._seq
            self._subs.append(sub)
        # Everyone else hears about the newcomer; the newcomer learns who is online from the
        # `hello` event at the head of its own stream.
        self._broadcast_ephemeral(self.presence_event(), exclude=sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        sub._closed = True
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)
        self._broadcast_ephemeral(self.presence_event())

    def online_users(self) -> list[str]:
        with self._lock:
            return sorted({s.user for s in self._subs})

    def presence_event(self) -> dict:
        return {"type": "presence", "online": self.online_users()}

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    # -- replay ----------------------------------------------------------------------
    def replay(self, last_event_id: str | None) -> tuple[list[dict], bool]:
        """Events after ``last_event_id``, and whether the client must resync instead.

        Returns ([], False) when there is nothing to replay (no id given, or already current).
        Returns ([], True) when the gap cannot be filled: unknown id format, a different epoch
        (server restarted), an id from the future, or events evicted from the buffer.
        """
        if not last_event_id:
            return [], False
        epoch, _, seq_txt = last_event_id.partition(":")
        if epoch != self.epoch or not seq_txt.isdigit():
            return [], True
        seen = int(seq_txt)
        with self._lock:
            current = self._seq
            buffered = list(self._buffer)
        if seen > current:
            return [], True
        if seen == current:
            return [], False
        oldest = buffered[0]["seq"] if buffered else current + 1
        if seen + 1 < oldest:
            return [], True
        return [e for e in buffered if e["seq"] > seen], False


def format_sse(event: dict) -> str:
    lines = []
    if "id" in event:
        lines.append(f"id: {event['id']}")
    lines.append(f"data: {json.dumps(event, separators=(',', ':'))}")
    return "\n".join(lines) + "\n\n"


async def event_stream(
    feed: ChangeFeed,
    user: str,
    last_event_id: str | None,
    is_disconnected: Callable[[], Awaitable[bool]],
    heartbeat_seconds: float = 15.0,
) -> AsyncIterator[str]:
    """The body of ``GET /api/events``: replay, then live events, with heartbeats."""
    sub = feed.subscribe(user)
    try:
        yield "retry: 3000\n\n"
        missed, resync = feed.replay(last_event_id)
        last_seq = sub.start_seq
        if resync:
            yield format_sse({"type": "resync", "reason": "events missed while disconnected"})
        for event in missed:
            yield format_sse(event)
            last_seq = event["seq"]
        if missed:
            last_seq = max(last_seq, missed[-1]["seq"])
        elif last_event_id and not resync:
            last_seq = max(last_seq, int(last_event_id.partition(":")[2]))
        yield format_sse(
            {"type": "hello", "user": user, "epoch": feed.epoch, "online": feed.online_users()}
        )

        while True:
            if await is_disconnected():
                break
            if sub.overflowed:
                sub.overflowed = False
                while not sub.queue.empty():
                    sub.queue.get_nowait()
                last_seq = feed.last_seq
                yield format_sse({"type": "resync", "reason": "client fell behind"})
                continue
            try:
                event = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat_seconds)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            seq = event.get("seq")
            if seq is not None:
                if seq <= last_seq:  # already sent during replay
                    continue
                last_seq = seq
            yield format_sse(event)
    finally:
        feed.unsubscribe(sub)
