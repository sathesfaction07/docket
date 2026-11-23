"""The live change feed: replay after a dropped connection, honest resyncs, presence, and
delivery of events published from other threads (FastAPI's sync endpoints run in a pool)."""

import asyncio
import json
import threading

import pytest

from case_forge.events import ChangeFeed, event_stream, format_sse


def parse(chunk: str) -> dict | None:
    data = [line[6:] for line in chunk.splitlines() if line.startswith("data: ")]
    return json.loads(data[0]) if data else None


class TestReplay:
    def test_no_last_event_id_means_nothing_to_replay(self):
        feed = ChangeFeed()
        feed.publish("case.created", "alice", case_id="a")
        assert feed.replay(None) == ([], False)

    def test_only_the_missed_events_are_replayed(self):
        feed = ChangeFeed()
        first = feed.publish("case.created", "alice", case_id="a")
        feed.publish("case.updated", "bob", case_id="a")
        feed.publish("case.deleted", "bob", case_id="a")
        missed, resync = feed.replay(first["id"])
        assert [e["type"] for e in missed] == ["case.updated", "case.deleted"]
        assert resync is False

    def test_an_up_to_date_client_gets_nothing(self):
        feed = ChangeFeed()
        last = feed.publish("case.created", "alice", case_id="a")
        assert feed.replay(last["id"]) == ([], False)

    def test_a_restarted_server_forces_a_resync(self):
        old = ChangeFeed()
        seen = old.publish("case.created", "alice", case_id="a")
        new = ChangeFeed()  # new epoch; its seq numbers mean something else
        new.publish("case.created", "alice", case_id="b")
        assert new.replay(seen["id"]) == ([], True)

    def test_an_evicted_gap_forces_a_resync(self):
        feed = ChangeFeed(capacity=3)
        first = feed.publish("e", "alice")
        for _ in range(5):
            feed.publish("e", "alice")
        assert feed.replay(first["id"]) == ([], True)

    @pytest.mark.parametrize("bad", ["garbage", "abc:", "x:y"])
    def test_a_malformed_id_forces_a_resync(self, bad):
        assert ChangeFeed().replay(bad) == ([], True)

    def test_an_id_from_the_future_forces_a_resync(self):
        feed = ChangeFeed()
        assert feed.replay(f"{feed.epoch}:99") == ([], True)


def test_format_sse_carries_the_id_only_for_durable_events():
    assert format_sse({"id": "e:1", "type": "x"}).startswith("id: e:1\ndata: ")
    assert format_sse({"type": "presence"}).startswith("data: ")


async def collect(stream, n, timeout=2.0):
    out = []

    async def run():
        async for chunk in stream:
            event = parse(chunk)
            if event:
                out.append(event)
            if len(out) >= n:
                return

    await asyncio.wait_for(run(), timeout)
    return out


def never_disconnected():
    async def check():
        return False

    return check


class TestEventStream:
    def test_events_published_from_another_thread_arrive_in_order(self):
        async def scenario():
            feed = ChangeFeed()
            stream = event_stream(feed, "alice", None, never_disconnected())
            hello = await collect(stream, 1)
            assert hello[0]["type"] == "hello"

            def writer():
                for i in range(5):
                    feed.publish("case.updated", "bob", case_id="a", n=i)

            threading.Thread(target=writer).start()
            got = []
            while len(got) < 5:
                (event,) = await collect(stream, 1)
                if event["type"] == "case.updated":
                    got.append(event["n"])
            await stream.aclose()
            return got

        assert asyncio.run(scenario()) == [0, 1, 2, 3, 4]

    def test_reconnecting_replays_the_gap_without_duplicates(self):
        async def scenario():
            feed = ChangeFeed()
            seen = feed.publish("case.created", "alice", case_id="a")
            feed.publish("case.updated", "bob", case_id="a", n=1)
            feed.publish("case.updated", "bob", case_id="a", n=2)
            stream = event_stream(feed, "alice", seen["id"], never_disconnected())
            events = await collect(stream, 3)
            feed.publish("case.updated", "bob", case_id="a", n=3)
            events += await collect(stream, 1)
            await stream.aclose()
            return events

        events = asyncio.run(scenario())
        types = [e["type"] for e in events]
        assert types == ["case.updated", "case.updated", "hello", "case.updated"]
        assert [e.get("n") for e in events if e["type"] == "case.updated"] == [1, 2, 3]

    def test_an_unfillable_gap_sends_resync_first(self):
        async def scenario():
            feed = ChangeFeed()
            stream = event_stream(feed, "alice", "old-epoch:5", never_disconnected())
            events = await collect(stream, 2)
            await stream.aclose()
            return events

        assert [e["type"] for e in asyncio.run(scenario())] == ["resync", "hello"]

    def test_a_client_that_falls_behind_is_told_to_resync(self):
        async def scenario():
            feed = ChangeFeed(queue_size=2)
            stream = event_stream(feed, "alice", None, never_disconnected())
            await collect(stream, 1)  # hello
            for i in range(10):  # nobody is reading: the queue overflows
                feed.publish("case.updated", "bob", n=i)
            await asyncio.sleep(0.05)  # let the loop deliver what fits
            events = await collect(stream, 1)
            await stream.aclose()
            return events

        assert asyncio.run(scenario())[0]["type"] == "resync"

    def test_heartbeats_keep_an_idle_connection_alive(self):
        async def scenario():
            feed = ChangeFeed()
            stream = event_stream(feed, "alice", None, never_disconnected(), heartbeat_seconds=0.01)
            chunks = [await stream.__anext__() for _ in range(4)]
            await stream.aclose()
            return chunks

        assert ": ping\n\n" in asyncio.run(scenario())

    def test_presence_tracks_who_is_connected(self):
        async def scenario():
            feed = ChangeFeed()
            alice = event_stream(feed, "alice", None, never_disconnected())
            hello = (await collect(alice, 1))[0]
            assert hello["online"] == ["alice"]
            bob = event_stream(feed, "bob", None, never_disconnected())
            await collect(bob, 1)
            joined = await collect(alice, 1)
            assert joined[0] == {"type": "presence", "online": ["alice", "bob"]}
            await bob.aclose()
            left = await collect(alice, 1)
            await alice.aclose()
            return left[0], feed.online_users()

        left, online_after = asyncio.run(scenario())
        assert left == {"type": "presence", "online": ["alice"]}
        assert online_after == []

    def test_disconnect_ends_the_stream_and_unsubscribes(self):
        async def scenario():
            feed = ChangeFeed()
            gone = {"v": False}

            async def is_disconnected():
                return gone["v"]

            stream = event_stream(feed, "alice", None, is_disconnected, heartbeat_seconds=0.01)
            await collect(stream, 1)
            gone["v"] = True
            rest = [chunk async for chunk in stream]
            return rest, feed.online_users()

        rest, online = asyncio.run(scenario())
        assert online == []
        assert all(not parse(c) for c in rest)
