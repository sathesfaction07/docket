import logging
from datetime import UTC, datetime

from ..models import Case, CaseFilters
from .base import BatchResult, CasePage, DashboardStats, DatabaseBackend, VersionConflictError

logger = logging.getLogger("case_forge")


class FirestoreBackend(DatabaseBackend):
    def __init__(self, project_id: str = "", collection_name: str = "cases", **kwargs):
        from google.cloud import firestore
        self._firestore = firestore
        self.db = firestore.Client(project=project_id) if project_id else firestore.Client()
        self.collection = self.db.collection(collection_name)

    def create_case(self, case: Case) -> Case:
        # create() fails if the document exists, unlike set(), which would overwrite it.
        self.collection.document(case.case_id).create(case.model_dump())
        logger.info(f"Created case {case.case_id}")
        return case

    def get_case(self, case_id: str) -> Case | None:
        doc = self.collection.document(case_id).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        if data.get("is_deleted"):
            return None
        return Case(**data)

    def list_cases(self, filters: CaseFilters) -> CasePage:
        # Only equality / `in` filters go to Firestore: it serves any combination of those from
        # its automatic single-field indexes. Adding order_by(created_at) to a filtered query
        # needs a composite index per filter combination -- none ship, so on a fresh database
        # every list failed, even the unfiltered one (is_deleted == False + order_by). Sorting
        # and paging happen here instead, which reads every matching case per page: fine at
        # this tool's scale (thousands of cases); beyond that, deploy composite indexes and
        # push order_by/limit back into the query.
        query = self.collection.where("is_deleted", "==", False)
        if filters.assignee:
            query = query.where("assignee", "==", filters.assignee)
        if filters.category:
            query = query.where("category", "==", filters.category)
        if filters.status:
            query = query.where("status", "in", [s.value for s in filters.status])
        if filters.is_high_value is not None:
            query = query.where("gms.is_high_value", "==", filters.is_high_value)

        matched = [case for doc in query.stream() if _keep(case := Case(**doc.to_dict()), filters)]
        matched.sort(key=lambda c: c.created_at, reverse=True)
        start = int(filters.cursor) if filters.cursor else 0
        page = matched[start : start + filters.page_size]
        end = start + len(page)
        cursor = str(end) if end < len(matched) else None
        return CasePage(items=page, cursor=cursor, total=len(matched))

    def _conditional_update(self, case_id: str, fields: dict, expected_version: int) -> dict:
        """Read-check-write inside a Firestore transaction. Firestore retries the transaction
        if the document changes between the read and the commit, so the version check and the
        write are atomic. Returns the new document, or {} if it is missing or deleted.
        """
        doc_ref = self.collection.document(case_id)

        @self._firestore.transactional
        def run(transaction):
            snapshot = doc_ref.get(transaction=transaction)
            if not snapshot.exists:
                return {}
            data = snapshot.to_dict()
            if data.get("is_deleted"):
                return {}
            if data.get("version", 1) != expected_version:
                raise VersionConflictError(case_id, expected_version, Case(**data))
            new_fields = {**fields, "version": expected_version + 1}
            transaction.update(doc_ref, new_fields)
            return {**data, **new_fields}

        return run(self.db.transaction())

    def update_case(self, case_id: str, updates: dict, expected_version: int) -> Case | None:
        fields = {k: v for k, v in updates.items() if k not in ("case_id", "version")}
        fields["updated_at"] = datetime.now(UTC).isoformat()
        data = self._conditional_update(case_id, fields, expected_version)
        return Case(**data) if data else None

    def soft_delete(self, case_id: str, deleted_by: str, expected_version: int) -> bool:
        now = datetime.now(UTC).isoformat()
        fields = {
            "is_deleted": True, "deleted_by": deleted_by, "deleted_at": now, "updated_at": now,
        }
        if not self._conditional_update(case_id, fields, expected_version):
            return False
        logger.info(f"Soft-deleted case {case_id} by {deleted_by}")
        return True

    def batch_import(self, cases: list[Case]) -> BatchResult:
        errors = []
        created = 0
        batch = self.db.batch()

        for i, case in enumerate(cases):
            try:
                ref = self.collection.document(case.case_id)
                batch.set(ref, case.model_dump())
                created += 1
                if (i + 1) % 500 == 0:
                    batch.commit()
                    batch = self.db.batch()
            except Exception as e:
                errors.append({"case_id": case.case_id, "error": str(e)})

        batch.commit()
        logger.info(f"Batch import: {created} created, {len(errors)} errors")
        return BatchResult(created=created, errors=errors)

    def get_stats(self) -> DashboardStats:
        docs = list(self.collection.where("is_deleted", "==", False).stream())
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        open_statuses = {"NEW", "IN_PROGRESS", "UNDER_REVIEW", "REOPENED"}

        total_open = 0
        high_value_open = 0
        resolved_today = 0
        created_today = 0
        by_status = {}
        by_category = {}

        for doc in docs:
            data = doc.to_dict()
            status = data.get("status", "")
            if status in open_statuses:
                total_open += 1
                if data.get("gms", {}).get("is_high_value"):
                    high_value_open += 1
            if status == "RESOLVED" and data.get("updated_at", "").startswith(today):
                resolved_today += 1
            if data.get("created_at", "").startswith(today):
                created_today += 1
            by_status[status] = by_status.get(status, 0) + 1
            cat = data.get("category", "unknown")
            by_category[cat] = by_category.get(cat, 0) + 1

        return DashboardStats(
            total_open=total_open,
            resolved_today=resolved_today,
            created_today=created_today,
            high_value_open=high_value_open,
            by_status=by_status,
            by_category=by_category,
        )


def _keep(case: Case, filters: CaseFilters) -> bool:
    """The filters Firestore cannot apply from single-field indexes."""
    if filters.product_id and filters.product_id.lower() not in case.product_id.lower():
        return False
    if (
        filters.description_contains
        and filters.description_contains.lower() not in case.description.lower()
    ):
        return False
    if filters.marketplace and filters.marketplace not in case.marketplace:
        return False
    if filters.created_after and case.created_at < filters.created_after:
        return False
    return not (filters.created_before and case.created_at > filters.created_before)
