import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, computed_field


class Status(StrEnum):
    NEW = "NEW"
    IN_PROGRESS = "IN_PROGRESS"
    UNDER_REVIEW = "UNDER_REVIEW"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    CLOSED = "CLOSED"
    REOPENED = "REOPENED"


VALID_TRANSITIONS = {
    Status.NEW: {Status.IN_PROGRESS},
    Status.IN_PROGRESS: {Status.UNDER_REVIEW, Status.ESCALATED},
    Status.UNDER_REVIEW: {Status.RESOLVED, Status.ESCALATED, Status.CLOSED},
    Status.RESOLVED: {Status.REOPENED},
    Status.ESCALATED: {Status.CLOSED},
    Status.REOPENED: {Status.IN_PROGRESS},
    Status.CLOSED: set(),
}


class GMS(BaseModel):
    shipped_units: float = 0
    unit_price: float = 0
    shipping_charge: float = 0
    gift_wrap_charge: float = 0
    price_discount: float = 0
    shipping_discount: float = 0
    gift_wrap_discount: float = 0

    @computed_field
    @property
    def t4w_gms(self) -> float:
        return round(
            (self.shipped_units * self.unit_price)
            + self.shipping_charge
            + self.gift_wrap_charge
            + self.price_discount
            + self.shipping_discount
            + self.gift_wrap_discount,
            2,
        )

    @computed_field
    @property
    def is_high_value(self) -> bool:
        return self.t4w_gms >= 1000.0


class CaseCreate(BaseModel):
    product_id: str
    category: str
    marketplace: list[str]
    description: str
    source: str = "manual"
    gms: GMS = Field(default_factory=GMS)


class CaseUpdate(BaseModel):
    category: str | None = None
    marketplace: list[str] | None = None
    description: str | None = None
    source: str | None = None
    gms: GMS | None = None


class StatusTransition(BaseModel):
    new_status: Status
    resolution: str | None = None
    resolution_notes: str | None = None


class NoteAdd(BaseModel):
    text: str


class CaseFilters(BaseModel):
    product_id: str | None = None
    assignee: str | None = None
    category: str | None = None
    marketplace: str | None = None
    status: list[Status] | None = None
    is_high_value: bool | None = None
    description_contains: str | None = None
    created_after: str | None = None
    created_before: str | None = None
    page_size: int = 50
    cursor: str | None = None


class EditRecord(BaseModel):
    field: str
    old_value: str | None
    new_value: str | None
    changed_by: str
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class Case(BaseModel):
    case_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    product_id: str
    category: str
    marketplace: list[str]
    description: str
    source: str = "manual"
    assignee: str
    status: Status = Status.NEW
    gms: GMS = Field(default_factory=GMS)
    resolution: str | None = None
    resolution_notes: str | None = None
    notes: list[dict] = Field(default_factory=list)
    status_history: list[dict] = Field(default_factory=list)
    edit_history: list[dict] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    created_by: str = ""
    is_deleted: bool = False
    deleted_by: str | None = None
    deleted_at: str | None = None
    # Optimistic-concurrency counter. Every successful write increments it, and every write
    # is conditional on the version it read, so two people editing one case cannot silently
    # overwrite each other. Records written before this field existed load as version 1.
    version: int = 1
