from abc import ABC, abstractmethod

from ..models import Case, CaseFilters


class VersionConflictError(Exception):
    """A conditional write lost: the stored case is no longer at the version the caller read.

    ``current`` is the case as it is now (None if it was deleted meanwhile), so the caller can
    retry against it or show the user what changed.
    """

    def __init__(self, case_id: str, expected: int, current: Case | None):
        self.case_id = case_id
        self.expected = expected
        self.current = current
        actual = current.version if current else "deleted"
        super().__init__(f"case {case_id}: expected version {expected}, found {actual}")


class BatchResult:
    def __init__(self, created: int = 0, errors: list[dict] | None = None):
        self.created = created
        self.errors = errors or []


class CasePage:
    def __init__(self, items: list[Case], cursor: str | None = None, total: int = 0):
        self.items = items
        self.cursor = cursor
        self.total = total


class DashboardStats:
    def __init__(self, **kwargs):
        self.total_open = kwargs.get("total_open", 0)
        self.resolved_today = kwargs.get("resolved_today", 0)
        self.created_today = kwargs.get("created_today", 0)
        self.high_value_open = kwargs.get("high_value_open", 0)
        self.avg_resolution_hours = kwargs.get("avg_resolution_hours", 0)
        self.by_status = kwargs.get("by_status", {})
        self.by_category = kwargs.get("by_category", {})


class DatabaseBackend(ABC):
    @abstractmethod
    def create_case(self, case: Case) -> Case: ...

    @abstractmethod
    def get_case(self, case_id: str) -> Case | None: ...

    @abstractmethod
    def list_cases(self, filters: CaseFilters) -> CasePage: ...

    @abstractmethod
    def update_case(self, case_id: str, updates: dict, expected_version: int) -> Case | None:
        """Apply ``updates`` only if the stored case is at ``expected_version``.

        Returns the updated case (version incremented), None if the case does not exist or is
        deleted, and raises VersionConflictError if someone else wrote first.
        """

    @abstractmethod
    def soft_delete(self, case_id: str, deleted_by: str, expected_version: int) -> bool:
        """Soft-delete only if the stored case is at ``expected_version``.

        Returns False if the case does not exist or is already deleted; raises
        VersionConflictError if it changed since the caller read it.
        """

    @abstractmethod
    def batch_import(self, cases: list[Case]) -> BatchResult: ...

    @abstractmethod
    def get_stats(self) -> DashboardStats: ...
