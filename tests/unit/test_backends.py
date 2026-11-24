"""The storage contract every backend must honour, run against each backend that can run here.

The in-memory backend runs as is. DynamoDB runs against moto, which evaluates condition
expressions, so the conditional-write logic is exercised for real rather than mocked.
Firestore runs against a real project when CF_TEST_FIRESTORE_PROJECT is set, and is
skipped otherwise (CI has no Google credentials).
"""

import os
import uuid

import pytest

from case_forge.database.base import VersionConflictError
from case_forge.database.memory import MemoryBackend
from case_forge.models import GMS, Case, CaseFilters, Status

moto = pytest.importorskip("moto")


def make_case(**overrides) -> Case:
    fields = dict(
        product_id="SKU-1",
        category="Toys & Games",
        marketplace=["US"],
        description="d",
        assignee="alice",
    )
    fields.update(overrides)
    return Case(**fields)


@pytest.fixture
def dynamodb_backend(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    import boto3

    from case_forge.database.dynamodb import DynamoDBBackend

    with moto.mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName="Docket",
            KeySchema=[{"AttributeName": "case_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "case_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield DynamoDBBackend(table_name="Docket", region="us-east-1")


@pytest.fixture
def firestore_backend():
    """A real Firestore database, opt-in. Set CF_TEST_FIRESTORE_PROJECT to a project you own
    (with a Native-mode Firestore database and application-default credentials) to run the
    contract against it. Each test gets a fresh collection, deleted afterwards."""
    project = os.environ.get("CF_TEST_FIRESTORE_PROJECT")
    if not project:
        pytest.skip("set CF_TEST_FIRESTORE_PROJECT to run against real Firestore")
    pytest.importorskip("google.cloud.firestore")
    from case_forge.database.firestore import FirestoreBackend

    collection = f"docket_test_{uuid.uuid4().hex[:12]}"
    backend = FirestoreBackend(project_id=project, collection_name=collection)
    yield backend
    for doc in backend.collection.stream():
        doc.reference.delete()


@pytest.fixture(params=["memory", "dynamodb", "firestore"])
def backend(request):
    if request.param == "memory":
        return MemoryBackend()
    return request.getfixturevalue(f"{request.param}_backend")


class TestConditionalWrites:
    def test_a_write_at_the_right_version_increments_it(self, backend):
        case = backend.create_case(make_case())
        assert backend.get_case(case.case_id).version == 1
        updated = backend.update_case(case.case_id, {"description": "x"}, expected_version=1)
        assert updated.version == 2
        assert updated.description == "x"
        assert backend.get_case(case.case_id).version == 2

    def test_a_stale_write_is_refused_and_reports_the_current_case(self, backend):
        case = backend.create_case(make_case())
        backend.update_case(case.case_id, {"description": "first"}, expected_version=1)
        with pytest.raises(VersionConflictError) as exc:
            backend.update_case(case.case_id, {"description": "second"}, expected_version=1)
        assert exc.value.current.version == 2
        assert exc.value.current.description == "first"
        assert backend.get_case(case.case_id).description == "first"

    def test_writes_never_touch_case_id_or_version_directly(self, backend):
        case = backend.create_case(make_case())
        updated = backend.update_case(
            case.case_id, {"version": 99, "case_id": "other", "description": "x"}, 1
        )
        assert (updated.case_id, updated.version) == (case.case_id, 2)

    def test_a_missing_case_is_none_not_an_upsert(self, backend):
        assert backend.update_case("nope", {"description": "x"}, expected_version=1) is None
        assert backend.get_case("nope") is None

    def test_a_deleted_case_cannot_be_updated(self, backend):
        case = backend.create_case(make_case())
        assert backend.soft_delete(case.case_id, "alice", expected_version=1) is True
        assert backend.update_case(case.case_id, {"description": "x"}, 2) is None
        assert backend.soft_delete(case.case_id, "alice", expected_version=2) is False

    def test_a_stale_delete_is_refused(self, backend):
        case = backend.create_case(make_case())
        backend.update_case(case.case_id, {"description": "x"}, expected_version=1)
        with pytest.raises(VersionConflictError):
            backend.soft_delete(case.case_id, "bob", expected_version=1)
        assert backend.get_case(case.case_id) is not None

    def test_creating_an_existing_id_does_not_overwrite(self, backend):
        case = backend.create_case(make_case(description="original"))
        with pytest.raises(Exception):  # noqa: B017 -- each backend raises its own type
            backend.create_case(make_case(case_id=case.case_id, description="clobber"))
        assert backend.get_case(case.case_id).description == "original"


class TestHighValue:
    """is_high_value is computed inside GMS, so it is stored under gms, not at the top level.
    The DynamoDB backend used to filter and count on a top-level attribute that create never
    wrote, so freshly created high-value cases were invisible to both."""

    def test_the_filter_finds_a_freshly_created_high_value_case(self, backend):
        backend.create_case(make_case(product_id="BIG", gms=GMS(shipped_units=100, unit_price=20)))
        backend.create_case(make_case(product_id="SMALL", gms=GMS(shipped_units=1, unit_price=5)))
        page = backend.list_cases(CaseFilters(is_high_value=True))
        assert [c.product_id for c in page.items] == ["BIG"]

    def test_the_dashboard_counts_it(self, backend):
        backend.create_case(make_case(gms=GMS(shipped_units=100, unit_price=20)))
        stats = backend.get_stats()
        assert stats.total_open == 1
        assert stats.high_value_open == 1


def test_dynamodb_items_written_before_versioning_load_and_update_as_version_1(
    dynamodb_backend,
):
    item = dynamodb_backend._to_dynamo(make_case())
    item.pop("version")
    dynamodb_backend.table.put_item(Item=item)
    legacy = dynamodb_backend.get_case(item["case_id"])
    assert legacy.version == 1
    updated = dynamodb_backend.update_case(item["case_id"], {"description": "x"}, 1)
    assert updated.version == 2


def test_memory_backend_hands_out_copies():
    backend = MemoryBackend()
    case = backend.create_case(make_case())
    fetched = backend.get_case(case.case_id)
    fetched.notes.append({"text": "sneaky"})
    fetched.status = Status.CLOSED
    stored = backend.get_case(case.case_id)
    assert stored.notes == [] and stored.status == Status.NEW


def _all_pages(backend, filters_kwargs, page_size):
    pages, cursor = [], None
    while True:
        page = backend.list_cases(CaseFilters(page_size=page_size, cursor=cursor,
                                              **filters_kwargs))
        pages.append([c.product_id for c in page.items])
        cursor = page.cursor
        if cursor is None:
            return pages
        assert len(pages) < 50, "paging did not terminate"


class TestDynamoDBPaging:
    """DynamoDB's Limit counts items READ, before the filter. Pages used to come back short or
    empty while matches remained, and stats read only the first 1 MB page."""

    def test_selective_filters_still_fill_every_page_and_miss_nothing(self, dynamodb_backend):
        for n in range(40):
            category = "Rare" if n % 8 == 0 else "Common"
            dynamodb_backend.create_case(make_case(product_id=f"P{n:02d}", category=category))

        pages = _all_pages(dynamodb_backend, {"category": "Rare"}, page_size=2)
        assert [len(p) for p in pages] == [2, 2, 1]
        assert sorted(pid for page in pages for pid in page) == ["P00", "P08", "P16", "P24", "P32"]

    def test_description_search_pages_the_same_way(self, dynamodb_backend):
        for n in range(30):
            text = "needle in here" if n % 6 == 0 else "hay"
            dynamodb_backend.create_case(make_case(product_id=f"D{n:02d}", description=text))

        pages = _all_pages(dynamodb_backend, {"description_contains": "NEEDLE"}, page_size=2)
        assert [len(p) for p in pages] == [2, 2, 1]
        assert len({pid for page in pages for pid in page}) == 5

    def test_a_page_that_ends_the_table_has_no_cursor(self, dynamodb_backend):
        for n in range(3):
            dynamodb_backend.create_case(make_case(product_id=f"E{n}"))
        page = dynamodb_backend.list_cases(CaseFilters(page_size=3))
        assert len(page.items) == 3
        assert page.cursor is None

    def test_stats_read_every_page(self, dynamodb_backend, monkeypatch):
        for n in range(7):
            dynamodb_backend.create_case(make_case(product_id=f"S{n}",
                                                   gms=GMS(shipped_units=100, unit_price=20)))
        real_scan = dynamodb_backend.table.scan

        class SmallPages:  # what a table larger than 1 MB looks like to a single scan call
            def scan(self, **kwargs):
                return real_scan(**{"Limit": 2, **kwargs})

        monkeypatch.setattr(dynamodb_backend, "table", SmallPages())
        stats = dynamodb_backend.get_stats()
        assert stats.total_open == 7
        assert stats.high_value_open == 7


class TestListing:
    """Listing behaves the same on every backend. On Firestore this needs no composite index:
    the query used to add order_by(created_at), which fails on a fresh database."""

    def seed(self, backend):
        rows = [("L1", "Toys", "NEW", "2026-01-01"), ("L2", "Toys", "IN_PROGRESS", "2026-01-02"),
                ("L3", "Books", "NEW", "2026-01-03"), ("L4", "Toys", "NEW", "2026-01-04"),
                ("L5", "Toys", "CLOSED", "2026-01-05")]
        for pid, cat, status, day in rows:
            backend.create_case(make_case(product_id=pid, category=cat, status=Status(status),
                                          created_at=f"{day}T10:00:00+00:00"))

    def test_an_unfiltered_list_is_newest_first(self, backend):
        self.seed(backend)
        page = backend.list_cases(CaseFilters(page_size=10))
        assert [c.product_id for c in page.items] == ["L5", "L4", "L3", "L2", "L1"]

    def test_filters_combine(self, backend):
        self.seed(backend)
        page = backend.list_cases(CaseFilters(category="Toys", status=[Status.NEW,
                                                                       Status.IN_PROGRESS]))
        assert sorted(c.product_id for c in page.items) == ["L1", "L2", "L4"]

    def test_cursor_paging_returns_everything_once(self, backend):
        self.seed(backend)
        seen, cursor = [], None
        for _ in range(10):
            page = backend.list_cases(CaseFilters(page_size=2, cursor=cursor))
            seen += [c.product_id for c in page.items]
            cursor = page.cursor
            if cursor is None:
                break
        assert sorted(seen) == ["L1", "L2", "L3", "L4", "L5"]

    def test_date_range_filters_are_honoured(self, backend):
        """The API accepts created_after/created_before; DynamoDB and Firestore used to
        ignore both silently and return every case."""
        self.seed(backend)
        page = backend.list_cases(CaseFilters(created_after="2026-01-02",
                                              created_before="2026-01-04T23:59:59+00:00"))
        assert sorted(c.product_id for c in page.items) == ["L2", "L3", "L4"]
