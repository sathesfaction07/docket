"""The storage contract every backend must honour, run against each backend that can run here.

The in-memory backend runs as is. DynamoDB runs against moto, which evaluates condition
expressions, so the conditional-write logic is exercised for real rather than mocked.
Firestore needs the Firestore emulator and is not covered by this file.
"""

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
            TableName="CaseForge",
            KeySchema=[{"AttributeName": "case_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "case_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield DynamoDBBackend(table_name="CaseForge", region="us-east-1")


@pytest.fixture(params=["memory", "dynamodb"])
def backend(request):
    if request.param == "memory":
        return MemoryBackend()
    return request.getfixturevalue("dynamodb_backend")


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
