"""Multi-user behaviour through the HTTP API: optimistic concurrency and the live feed.

Races are made deterministic by a backend that runs another user's write in the gap between
the API reading a case and writing it back -- exactly where a lost update used to happen.
"""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from case_forge import api
from case_forge.config import settings
from case_forge.database.base import VersionConflictError
from case_forge.database.memory import MemoryBackend
from case_forge.events import ChangeFeed

ALICE = {"Authorization": "Bearer tok-alice"}
BOB = {"Authorization": "Bearer tok-bob"}
NEW_CASE = {
    "product_id": "SKU-001",
    "category": "Toys & Games",
    "marketplace": ["US"],
    "description": "Missing safety certificate",
}


class RacingBackend(MemoryBackend):
    """Runs ``interleave`` once, just before the next conditional write."""

    def __init__(self):
        super().__init__()
        self.interleave = None
        self.write_attempts = 0

    def update_case(self, case_id, updates, expected_version):
        self.write_attempts += 1
        if self.interleave:
            hook, self.interleave = self.interleave, None
            hook()
        return super().update_case(case_id, updates, expected_version)


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setattr(settings, "api_tokens", "tok-alice:alice,tok-bob:bob")
    monkeypatch.setattr(settings, "admin_users", "")
    monkeypatch.setattr(settings, "local_user", "")
    db = RacingBackend()
    monkeypatch.setattr(api, "db", db)
    monkeypatch.setattr(api, "feed", ChangeFeed())
    return db


@pytest.fixture
def client(backend):
    return TestClient(api.app)


def create(client, headers=ALICE):
    resp = client.post("/api/cases", json=NEW_CASE, headers=headers)
    assert resp.status_code == 201
    return resp.json()["case_id"]


def events():
    return list(api.feed._buffer)


class TestVersionsAndETags:
    def test_reads_and_writes_carry_the_version_as_an_etag(self, client):
        resp = client.post("/api/cases", json=NEW_CASE, headers=ALICE)
        assert resp.headers["etag"] == '"v1"'
        case_id = resp.json()["case_id"]
        assert client.get(f"/api/cases/{case_id}", headers=ALICE).headers["etag"] == '"v1"'
        edited = client.patch(
            f"/api/cases/{case_id}",
            json={"description": "x"},
            headers={**ALICE, "If-Match": '"v1"'},
        )
        assert edited.status_code == 200
        assert edited.headers["etag"] == '"v2"'
        assert edited.json()["version"] == 2

    @pytest.mark.parametrize("tag", ['"v1"', 'W/"v1"', "v1", "1"])
    def test_if_match_accepts_the_usual_spellings(self, client, tag):
        case_id = create(client)
        resp = client.patch(
            f"/api/cases/{case_id}", json={"description": "x"}, headers={**ALICE, "If-Match": tag}
        )
        assert resp.status_code == 200

    def test_a_garbage_if_match_is_a_400(self, client):
        case_id = create(client)
        resp = client.patch(
            f"/api/cases/{case_id}", json={"description": "x"}, headers={**ALICE, "If-Match": "abc"}
        )
        assert resp.status_code == 400

    def test_a_stale_edit_is_refused_with_the_current_case(self, client):
        case_id = create(client)
        client.patch(f"/api/cases/{case_id}", json={"description": "first"}, headers=ALICE)
        stale = client.patch(
            f"/api/cases/{case_id}",
            json={"description": "second"},
            headers={**ALICE, "If-Match": '"v1"'},
        )
        assert stale.status_code == 409
        detail = stale.json()["detail"]
        assert detail["current"]["description"] == "first"
        assert detail["current"]["version"] == 2
        assert "version 1" in detail["message"]

    def test_a_no_op_edit_writes_nothing_and_announces_nothing(self, client):
        case_id = create(client)
        before = len(events())
        resp = client.patch(
            f"/api/cases/{case_id}", json={"description": NEW_CASE["description"]}, headers=ALICE
        )
        assert resp.json()["version"] == 1
        assert resp.json()["edit_history"] == []
        assert len(events()) == before


class TestRacesThatUsedToLoseData:
    def test_two_concurrent_notes_both_survive(self, client, backend):
        case_id = create(client)

        def bob_adds_a_note_first():
            case = backend.get_case(case_id)
            notes = [*case.notes, {"text": "bob's note", "author": "bob"}]
            MemoryBackend.update_case(backend, case_id, {"notes": notes}, case.version)

        backend.interleave = bob_adds_a_note_first
        resp = client.post(
            f"/api/cases/{case_id}/notes", json={"text": "alice's note"}, headers=ALICE
        )
        assert resp.status_code == 200
        texts = [n["text"] for n in backend.get_case(case_id).notes]
        assert texts == ["bob's note", "alice's note"]
        assert backend.write_attempts == 2  # lost the race once, re-read, then won

    def test_a_status_change_is_revalidated_against_what_actually_happened(self, client, backend):
        case_id = create(client)

        def someone_else_starts_it():
            case = backend.get_case(case_id)
            MemoryBackend.update_case(backend, case_id, {"status": "IN_PROGRESS"}, case.version)

        backend.interleave = someone_else_starts_it
        resp = client.post(
            f"/api/cases/{case_id}/status", json={"new_status": "IN_PROGRESS"}, headers=ALICE
        )
        # Without the re-check this succeeded, writing a second NEW -> IN_PROGRESS entry for a
        # case that was no longer NEW.
        assert resp.status_code == 400
        assert "IN_PROGRESS → IN_PROGRESS" in resp.json()["detail"]

    def test_a_pinned_status_change_on_a_stale_view_is_a_409(self, client, backend):
        case_id = create(client)
        client.post(f"/api/cases/{case_id}/notes", json={"text": "bump"}, headers=BOB)
        resp = client.post(
            f"/api/cases/{case_id}/status",
            json={"new_status": "IN_PROGRESS"},
            headers={**ALICE, "If-Match": '"v1"'},
        )
        assert resp.status_code == 409
        assert backend.get_case(case_id).status == "NEW"

    def test_a_stale_delete_is_a_409(self, client):
        case_id = create(client)
        client.patch(f"/api/cases/{case_id}", json={"description": "x"}, headers=ALICE)
        resp = client.delete(f"/api/cases/{case_id}", headers={**ALICE, "If-Match": '"v1"'})
        assert resp.status_code == 409
        assert client.get(f"/api/cases/{case_id}", headers=ALICE).status_code == 200

    def test_truly_concurrent_notes_are_never_silently_lost(self, backend, monkeypatch):
        """Real threads, real races: eight writers on one case, with a pause between each
        read and its write so the races actually happen (without it, requests ran one at a
        time and this test passed vacuously). Under this load a request may give up with a
        409 after its retries -- but it says so. A 200 whose note is missing must never
        happen."""
        slow = SlowReadBackend()
        monkeypatch.setattr(api, "db", slow)
        codes, stored = hammer_with_notes(slow)

        assert slow.conflicts > 0, "no races happened, so nothing was tested"
        assert set(codes) <= {200, 409}
        succeeded = {f"n{i}" for i, code in enumerate(codes) if code == 200}
        assert stored == succeeded
        assert len(succeeded) >= len(codes) // 2  # retries absorb most collisions

    def test_without_version_checks_the_same_load_loses_notes(self, backend, monkeypatch):
        """The control: identical load against a backend that ignores the version (the
        pre-fix write path). Every request reports success and notes still vanish."""
        unchecked = UncheckedSlowBackend()
        monkeypatch.setattr(api, "db", unchecked)
        codes, stored = hammer_with_notes(unchecked)

        assert codes == [200] * len(codes)
        assert len(stored) < len(codes)


class SlowReadBackend(MemoryBackend):
    def __init__(self):
        super().__init__()
        self.conflicts = 0

    def get_case(self, case_id):
        case = super().get_case(case_id)
        time.sleep(0.005)
        return case

    def update_case(self, case_id, updates, expected_version):
        try:
            return super().update_case(case_id, updates, expected_version)
        except VersionConflictError:
            self.conflicts += 1
            raise


class UncheckedSlowBackend(SlowReadBackend):
    def update_case(self, case_id, updates, expected_version):
        return super().update_case(case_id, updates, self._cases[case_id].version)


def hammer_with_notes(backend, writers=8, notes=24):
    with TestClient(api.app) as c:
        case_id = create(c)

    def add(i):
        with TestClient(api.app) as c:
            return c.post(
                f"/api/cases/{case_id}/notes", json={"text": f"n{i}"}, headers=ALICE
            ).status_code

    with ThreadPoolExecutor(max_workers=writers) as pool:
        codes = list(pool.map(add, range(notes)))
    return codes, {n["text"] for n in backend.get_case(case_id).notes}


class TestTheLiveFeed:
    def test_every_write_is_announced_with_actor_and_version(self, client):
        case_id = create(client)
        client.patch(f"/api/cases/{case_id}", json={"description": "x"}, headers=ALICE)
        client.post(
            f"/api/cases/{case_id}/status", json={"new_status": "IN_PROGRESS"}, headers=ALICE
        )
        client.post(f"/api/cases/{case_id}/notes", json={"text": "hi"}, headers=BOB)
        client.delete(f"/api/cases/{case_id}", headers=ALICE)

        log = [(e["type"], e["actor"], e.get("version"), e.get("change")) for e in events()]
        assert log == [
            ("case.created", "alice", 1, {}),
            ("case.updated", "alice", 2, {"fields": ["description"]}),
            ("case.updated", "alice", 3, {"status": ["NEW", "IN_PROGRESS"]}),
            ("case.updated", "bob", 4, {"note": True}),
            ("case.deleted", "alice", None, None),
        ]
        assert events()[1]["case"]["description"] == "x"

    def test_refused_writes_announce_nothing(self, client):
        case_id = create(client)
        before = len(events())
        client.patch(f"/api/cases/{case_id}", json={"description": "x"}, headers=BOB)  # 403
        client.post(f"/api/cases/{case_id}/status", json={"new_status": "CLOSED"}, headers=ALICE)
        assert len(events()) == before

    def test_the_list_says_which_event_it_is_consistent_with(self, client):
        create(client)
        create(client)
        body = client.get("/api/cases", headers=ALICE).json()
        assert body["as_of_event"] == api.feed.last_seq == 2

    def test_the_event_stream_requires_a_token(self, client):
        assert client.get("/api/events").status_code == 401

    def test_presence_lists_connected_users(self, client):
        assert client.get("/api/presence", headers=ALICE).json() == {"online": []}

    def test_empty_notes_are_refused(self, client):
        case_id = create(client)
        resp = client.post(f"/api/cases/{case_id}/notes", json={"text": "  "}, headers=ALICE)
        assert resp.status_code == 400
