import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from ..models import Case, CaseFilters
from .base import BatchResult, CasePage, DashboardStats, DatabaseBackend, VersionConflictError

logger = logging.getLogger("case_forge")


class DynamoDBBackend(DatabaseBackend):
    def __init__(self, table_name: str = "Docket", region: str = "us-east-1", **kwargs):
        self.dynamodb = boto3.resource("dynamodb", region_name=region)
        self.table = self.dynamodb.Table(table_name)

    def create_case(self, case: Case) -> Case:
        item = self._to_dynamo(case)
        self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(case_id)")
        logger.info(f"Created case {case.case_id}")
        return case

    def get_case(self, case_id: str) -> Case | None:
        response = self.table.get_item(Key={"case_id": case_id})
        item = response.get("Item")
        if not item or item.get("is_deleted"):
            return None
        return self._from_dynamo(item)

    def list_cases(self, filters: CaseFilters) -> CasePage:
        scan_kwargs = {"FilterExpression": Attr("is_deleted").eq(False)}

        if filters.status:
            status_values = [s.value for s in filters.status]
            scan_kwargs["FilterExpression"] &= Attr("status").is_in(status_values)
        if filters.assignee:
            scan_kwargs["FilterExpression"] &= Attr("assignee").eq(filters.assignee)
        if filters.category:
            scan_kwargs["FilterExpression"] &= Attr("category").eq(filters.category)
        if filters.marketplace:
            scan_kwargs["FilterExpression"] &= Attr("marketplace").contains(filters.marketplace)
        if filters.is_high_value is not None:
            # is_high_value is a computed field of GMS, so it is stored nested under gms.
            scan_kwargs["FilterExpression"] &= Attr("gms.is_high_value").eq(filters.is_high_value)
        if filters.product_id:
            scan_kwargs["FilterExpression"] &= Attr("product_id").contains(filters.product_id)
        # ISO-8601 timestamps compare correctly as strings.
        if filters.created_after:
            scan_kwargs["FilterExpression"] &= Attr("created_at").gte(filters.created_after)
        if filters.created_before:
            scan_kwargs["FilterExpression"] &= Attr("created_at").lte(filters.created_before)

        # A scan has no useful order and DynamoDB applies Limit before the filter, so read every
        # match (all pages), then sort newest-first and page here -- the same strategy as the
        # Firestore backend, so both return identical pages. At this tool's scale (thousands of
        # cases) one full scan per request is cheap; beyond that, add a GSI keyed for listing.
        term = filters.description_contains.lower() if filters.description_contains else ""
        matched = []
        for raw in self._scan_all(**scan_kwargs):
            case = self._from_dynamo(raw)
            if not term or term in case.description.lower():
                matched.append(case)
        matched.sort(key=lambda c: c.created_at, reverse=True)
        start = int(filters.cursor) if filters.cursor else 0
        page = matched[start : start + filters.page_size]
        end = start + len(page)
        cursor = str(end) if end < len(matched) else None
        return CasePage(items=page, cursor=cursor, total=len(matched))

    def _conditional_set(self, case_id: str, fields: dict, expected_version: int) -> dict:
        """SET ``fields`` plus version+1, only if the item exists, is live, and is at
        ``expected_version``. One round trip; DynamoDB evaluates the condition atomically.
        """
        names = {"#version": "version", "#deleted": "is_deleted"}
        values = {":expected": expected_version, ":next": expected_version + 1, ":false": False}
        parts = ["#version = :next"]
        for i, (key, value) in enumerate(fields.items()):
            names[f"#n{i}"] = key
            values[f":v{i}"] = self._serialize_value(value)
            parts.append(f"#n{i} = :v{i}")

        # Items written before versioning have no version attribute; they are version 1.
        version_ok = (
            "(#version = :expected OR attribute_not_exists(#version))"
            if expected_version == 1
            else "#version = :expected"
        )
        try:
            response = self.table.update_item(
                Key={"case_id": case_id},
                UpdateExpression="SET " + ", ".join(parts),
                ConditionExpression=(
                    f"attribute_exists(case_id) AND #deleted = :false AND {version_ok}"
                ),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            current = self.get_case(case_id)
            if current is None:
                return {}
            raise VersionConflictError(case_id, expected_version, current) from None
        return response["Attributes"]

    def update_case(self, case_id: str, updates: dict, expected_version: int) -> Case | None:
        fields = {k: v for k, v in updates.items() if k not in ("case_id", "version")}
        fields["updated_at"] = datetime.now(UTC).isoformat()
        item = self._conditional_set(case_id, fields, expected_version)
        return self._from_dynamo(item) if item else None

    def soft_delete(self, case_id: str, deleted_by: str, expected_version: int) -> bool:
        now = datetime.now(UTC).isoformat()
        fields = {
            "is_deleted": True, "deleted_by": deleted_by, "deleted_at": now, "updated_at": now,
        }
        if not self._conditional_set(case_id, fields, expected_version):
            return False
        logger.info(f"Soft-deleted case {case_id} by {deleted_by}")
        return True

    def batch_import(self, cases: list[Case]) -> BatchResult:
        errors = []
        created = 0
        with self.table.batch_writer() as batch:
            for case in cases:
                try:
                    batch.put_item(Item=self._to_dynamo(case))
                    created += 1
                except Exception as e:
                    errors.append({"case_id": case.case_id, "error": str(e)})
        logger.info(f"Batch import: {created} created, {len(errors)} errors")
        return BatchResult(created=created, errors=errors)

    def get_stats(self) -> DashboardStats:
        # Every page: one scan call returns at most 1 MB, so a single call undercounts any
        # table larger than that.
        items = list(self._scan_all(FilterExpression=Attr("is_deleted").eq(False)))

        open_statuses = {"NEW", "IN_PROGRESS", "UNDER_REVIEW", "REOPENED"}
        today = datetime.now(UTC).strftime("%Y-%m-%d")

        total_open = sum(1 for i in items if i.get("status") in open_statuses)
        high_value_open = sum(
            1 for i in items
            if i.get("status") in open_statuses and (i.get("gms") or {}).get("is_high_value")
        )
        resolved_today = sum(
            1 for i in items
            if i.get("status") == "RESOLVED" and i.get("updated_at", "").startswith(today)
        )
        created_today = sum(1 for i in items if i.get("created_at", "").startswith(today))

        by_status = {}
        by_category = {}
        for i in items:
            s = i.get("status", "UNKNOWN")
            by_status[s] = by_status.get(s, 0) + 1
            c = i.get("category", "unknown")
            by_category[c] = by_category.get(c, 0) + 1

        return DashboardStats(
            total_open=total_open,
            resolved_today=resolved_today,
            created_today=created_today,
            high_value_open=high_value_open,
            by_status=by_status,
            by_category=by_category,
        )

    def _scan_all(self, **kwargs) -> Iterator[dict]:
        """Yield items from every page of a scan."""
        while True:
            response = self.table.scan(**kwargs)
            yield from response.get("Items", [])
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return
            kwargs["ExclusiveStartKey"] = last_key

    def _to_dynamo(self, case: Case) -> dict:
        data = json.loads(case.model_dump_json())
        return json.loads(json.dumps(data), parse_float=Decimal)

    def _from_dynamo(self, item: dict) -> Case:
        data = json.loads(json.dumps(item, default=str))
        return Case(**data)

    def _serialize_value(self, value):
        if isinstance(value, float):
            return Decimal(str(value))
        if isinstance(value, dict):
            return json.loads(json.dumps(value), parse_float=Decimal)
        if isinstance(value, list):
            return json.loads(json.dumps(value), parse_float=Decimal)
        return value
