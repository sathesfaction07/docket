"""Per-request identity, tested through the HTTP API rather than through `auth.py` alone.

The ownership rules in `auth.py` were always correct. What was wrong is where the user came
from: `getpass.getuser()` on the server, which is the same for every client, so "you can only
edit your own cases" compared the server with itself. These tests drive two different
clients at one server and assert that the server tells them apart.
"""

import pytest
from fastapi.testclient import TestClient

from case_forge import api
from case_forge.auth import resolve_user
from case_forge.config import settings
from case_forge.database.memory import MemoryBackend

ALICE = {"Authorization": "Bearer tok-alice"}
BOB = {"Authorization": "Bearer tok-bob"}
ROOT = {"Authorization": "Bearer tok-root"}

NEW_CASE = {
    "product_id": "SKU-001",
    "category": "Toys & Games",
    "marketplace": ["US"],
    "description": "Missing safety certificate",
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "api_tokens", "tok-alice:alice,tok-bob:bob,tok-root:root")
    monkeypatch.setattr(settings, "admin_users", "root")
    monkeypatch.setattr(settings, "local_user", "")
    monkeypatch.setattr(api, "db", MemoryBackend())
    return TestClient(api.app)


class TestResolveUser:
    @pytest.fixture(autouse=True)
    def _tokens(self, monkeypatch):
        monkeypatch.setattr(settings, "api_tokens", "tok-alice:alice, tok-bob:bob")
        monkeypatch.setattr(settings, "local_user", "")

    def test_each_token_maps_to_its_own_user(self):
        assert resolve_user("Bearer tok-alice") == "alice"
        assert resolve_user("Bearer tok-bob") == "bob"

    def test_the_scheme_is_case_insensitive(self):
        assert resolve_user("bearer tok-alice") == "alice"

    @pytest.mark.parametrize("header", ["Bearer nope", "Bearer ", "Basic tok-alice", "tok-alice"])
    def test_anything_else_is_unauthenticated(self, header):
        assert resolve_user(header) is None

    def test_no_header_is_unauthenticated_without_local_mode(self):
        assert resolve_user(None) is None

    def test_local_mode_covers_only_requests_with_no_header(self, monkeypatch):
        monkeypatch.setattr(settings, "local_user", "alice")
        assert resolve_user(None) == "alice"
        # A wrong token is a failed login, not an anonymous request.
        assert resolve_user("Bearer nope") is None

    def test_malformed_token_entries_are_ignored(self, monkeypatch):
        monkeypatch.setattr(settings, "api_tokens", "no-colon,:nouser,notoken:, tok:carol")
        assert settings.token_map == {"tok": "carol"}


class TestTheApiTellsClientsApart:
    def test_requests_without_a_token_are_rejected(self, client):
        assert client.get("/api/me").status_code == 401
        assert client.post("/api/cases", json=NEW_CASE).status_code == 401

    def test_an_invalid_token_is_rejected(self, client):
        resp = client.get("/api/me", headers={"Authorization": "Bearer forged"})
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"

    def test_me_reports_the_caller_not_the_server(self, client):
        assert client.get("/api/me", headers=ALICE).json() == {"user": "alice", "role": "user"}
        assert client.get("/api/me", headers=BOB).json() == {"user": "bob", "role": "user"}
        assert client.get("/api/me", headers=ROOT).json() == {"user": "root", "role": "admin"}

    def test_a_new_case_belongs_to_whoever_created_it(self, client):
        case = client.post("/api/cases", json=NEW_CASE, headers=ALICE).json()
        assert case["assignee"] == "alice"
        assert case["created_by"] == "alice"
        assert case["status_history"][0]["changed_by"] == "alice"

    def test_one_user_cannot_modify_or_delete_anothers_case(self, client):
        case_id = client.post("/api/cases", json=NEW_CASE, headers=ALICE).json()["case_id"]

        patch = client.patch(f"/api/cases/{case_id}", json={"description": "x"}, headers=BOB)
        assert patch.status_code == 403
        status = client.post(
            f"/api/cases/{case_id}/status", json={"new_status": "IN_PROGRESS"}, headers=BOB
        )
        assert status.status_code == 403
        assert client.delete(f"/api/cases/{case_id}", headers=BOB).status_code == 403

    def test_the_owner_and_an_admin_can(self, client):
        case_id = client.post("/api/cases", json=NEW_CASE, headers=ALICE).json()["case_id"]

        edited = client.patch(f"/api/cases/{case_id}", json={"description": "y"}, headers=ALICE)
        assert edited.status_code == 200
        assert edited.json()["edit_history"][-1]["changed_by"] == "alice"
        assert client.delete(f"/api/cases/{case_id}", headers=ROOT).status_code == 200

    def test_notes_are_attributed_to_their_author(self, client):
        case_id = client.post("/api/cases", json=NEW_CASE, headers=ALICE).json()["case_id"]
        client.post(f"/api/cases/{case_id}/notes", json={"text": "hi"}, headers=BOB)
        assert api.db.get_case(case_id).notes[-1]["author"] == "bob"

    def test_health_no_longer_leaks_a_username(self, client):
        assert "user" not in client.get("/health").json()
