from unittest.mock import patch

from case_forge.auth import can_delete, can_modify, is_admin
from case_forge.config import Settings


class TestAuth:
    @patch("case_forge.auth.settings")
    def test_admin_user(self, mock_settings):
        mock_settings.admin_list = ["admin", "superuser"]
        assert is_admin("admin") is True
        assert is_admin("superuser") is True
        assert is_admin("regular") is False

    @patch("case_forge.auth.settings")
    def test_owner_can_modify(self, mock_settings):
        mock_settings.admin_list = ["admin"]
        assert can_modify("john", "john") is True
        assert can_modify("john", "jane") is False
        assert can_modify("admin", "jane") is True

    @patch("case_forge.auth.settings")
    def test_owner_can_delete(self, mock_settings):
        mock_settings.admin_list = ["admin"]
        assert can_delete("john", "john") is True
        assert can_delete("john", "jane") is False
        assert can_delete("admin", "anyone") is True


class TestAdminListParsing:
    """The comma-separated string -> list conversion `auth.py` actually depends on.

    Untested until 2026-09-18. Its absence is why three tests could patch the wrong attribute
    and still look meaningful.
    """

    def test_single_user(self):
        assert Settings(admin_users="admin").admin_list == ["admin"]

    def test_several_users_with_whitespace(self):
        assert Settings(admin_users="admin, second ,third").admin_list == [
            "admin", "second", "third",
        ]

    def test_empty_entries_are_dropped(self):
        assert Settings(admin_users="admin,,").admin_list == ["admin"]

    def test_empty_string_gives_no_admins(self):
        assert Settings(admin_users="").admin_list == []

    def test_the_default_grants_exactly_one_admin(self, monkeypatch):
        """`Settings()` is NOT hermetic -- it reads CF_* from the environment and from .env.

        So the environment has to be cleared and `_env_file` disabled, or this asserts on
        whatever the machine happens to have. Written the naive way first, it passed locally
        and failed the moment CF_ADMIN_USERS was set in the shell.
        """
        monkeypatch.delenv("CF_ADMIN_USERS", raising=False)
        assert Settings(_env_file=None).admin_list == ["admin"]
