import hmac

from .config import settings


def resolve_user(authorization: str | None) -> str | None:
    """Map one request's Authorization header to a user, or None if it is not authenticated.

    A header that is present but wrong never falls back to the local user: a bad token is a
    failed login, not an anonymous request.
    """
    if authorization:
        scheme, _, token = authorization.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            return None
        user = None
        # Compare against every token so the time taken does not reveal which prefix matched.
        for known, owner in settings.token_map.items():
            if hmac.compare_digest(known.encode(), token.encode()):
                user = owner
        return user
    return settings.local_user or None


def is_admin(user: str) -> bool:
    return user in settings.admin_list


def can_modify(user: str, case_assignee: str) -> bool:
    return is_admin(user) or user == case_assignee


def can_delete(user: str, case_assignee: str) -> bool:
    return is_admin(user) or user == case_assignee
