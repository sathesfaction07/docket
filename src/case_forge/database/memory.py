"""In-process backend: no cloud account needed to run, demo, or test CaseForge.

It implements the same contract as the DynamoDB and Firestore backends, including the
optimistic-concurrency rule (every write is conditional on the version the caller read), so
the API's conflict handling and the live event stream behave identically on it. Data lives
only as long as the process.
"""

import logging
import threading
from datetime import UTC, datetime

from ..models import Case, CaseFilters
from .base import BatchResult, CasePage, DashboardStats, DatabaseBackend, VersionConflictError

logger = logging.getLogger("case_forge")

OPEN_STATUSES = {"NEW", "IN_PROGRESS", "UNDER_REVIEW", "REOPENED"}


class MemoryBackend(DatabaseBackend):
    def __init__(self, **kwargs):
        self._lock = threading.Lock()
        self._cases: dict[str, Case] = {}

    # Cases are copied on the way in and out so no caller can mutate stored state in place.
    def create_case(self, case: Case) -> Case:
        with self._lock:
            if case.case_id in self._cases:
                raise ValueError(f"case {case.case_id} already exists")
            self._cases[case.case_id] = case.model_copy(deep=True)
        logger.info(f"Created case {case.case_id}")
        return case

    def get_case(self, case_id: str) -> Case | None:
        with self._lock:
            case = self._cases.get(case_id)
            return case.model_copy(deep=True) if case and not case.is_deleted else None

    def list_cases(self, filters: CaseFilters) -> CasePage:
        with self._lock:
            items = [c.model_copy(deep=True) for c in self._cases.values() if not c.is_deleted]

        def keep(c: Case) -> bool:
            if filters.status and c.status not in filters.status:
                return False
            if filters.assignee and c.assignee != filters.assignee:
                return False
            if filters.category and c.category != filters.category:
                return False
            if filters.marketplace and filters.marketplace not in c.marketplace:
                return False
            if filters.is_high_value is not None and c.gms.is_high_value != filters.is_high_value:
                return False
            if filters.product_id and filters.product_id.lower() not in c.product_id.lower():
                return False
            if (
                filters.description_contains
                and filters.description_contains.lower() not in c.description.lower()
            ):
                return False
            if filters.created_after and c.created_at < filters.created_after:
                return False
            return not (filters.created_before and c.created_at > filters.created_before)

        matched = sorted((c for c in items if keep(c)), key=lambda c: c.created_at, reverse=True)
        start = int(filters.cursor) if filters.cursor else 0
        page = matched[start : start + filters.page_size]
        end = start + len(page)
        cursor = str(end) if end < len(matched) else None
        return CasePage(items=page, cursor=cursor, total=len(matched))

    def _conditional_update(self, case_id: str, fields: dict, expected_version: int) -> Case | None:
        with self._lock:
            stored = self._cases.get(case_id)
            if stored is None or stored.is_deleted:
                return None
            if stored.version != expected_version:
                raise VersionConflictError(case_id, expected_version, stored.model_copy(deep=True))
            data = stored.model_dump()
            data.update(fields)
            data["version"] = expected_version + 1
            updated = Case(**data)
            self._cases[case_id] = updated
            return updated.model_copy(deep=True)

    def update_case(self, case_id: str, updates: dict, expected_version: int) -> Case | None:
        fields = {k: v for k, v in updates.items() if k not in ("case_id", "version")}
        fields["updated_at"] = datetime.now(UTC).isoformat()
        return self._conditional_update(case_id, fields, expected_version)

    def soft_delete(self, case_id: str, deleted_by: str, expected_version: int) -> bool:
        now = datetime.now(UTC).isoformat()
        fields = {
            "is_deleted": True, "deleted_by": deleted_by, "deleted_at": now, "updated_at": now,
        }
        if self._conditional_update(case_id, fields, expected_version) is None:
            return False
        logger.info(f"Soft-deleted case {case_id} by {deleted_by}")
        return True

    def batch_import(self, cases: list[Case]) -> BatchResult:
        errors, created = [], 0
        for case in cases:
            try:
                self.create_case(case)
                created += 1
            except ValueError as e:
                errors.append({"case_id": case.case_id, "error": str(e)})
        return BatchResult(created=created, errors=errors)

    def get_stats(self) -> DashboardStats:
        with self._lock:
            items = [c for c in self._cases.values() if not c.is_deleted]
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        by_status: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for c in items:
            by_status[c.status.value] = by_status.get(c.status.value, 0) + 1
            by_category[c.category] = by_category.get(c.category, 0) + 1
        return DashboardStats(
            total_open=sum(1 for c in items if c.status.value in OPEN_STATUSES),
            high_value_open=sum(
                1 for c in items if c.status.value in OPEN_STATUSES and c.gms.is_high_value
            ),
            resolved_today=sum(
                1 for c in items if c.status.value == "RESOLVED" and c.updated_at.startswith(today)
            ),
            created_today=sum(1 for c in items if c.created_at.startswith(today)),
            by_status=by_status,
            by_category=by_category,
        )
