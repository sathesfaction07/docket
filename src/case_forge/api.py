import csv
import io
import json
import logging
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .auth import can_delete, can_modify, is_admin, resolve_user
from .config import settings
from .database import VersionConflictError, get_database
from .events import ChangeFeed, event_stream
from .models import (
    GMS,
    VALID_TRANSITIONS,
    Case,
    CaseCreate,
    CaseFilters,
    CaseUpdate,
    EditRecord,
    NoteAdd,
    Status,
    StatusTransition,
)

logging.basicConfig(
    level=settings.log_level,
    format='{"time":"%(asctime)s","level":"%(levelname)s","module":"%(module)s","msg":"%(message)s"}',
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("case_forge")

app = FastAPI(title="CaseForge API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type", "If-Match", "Last-Event-ID"],
    expose_headers=["ETag"],
)


def require_user(authorization: Annotated[str | None, Header()] = None) -> str:
    user = resolve_user(authorization)
    if user is None:
        raise HTTPException(
            401, "Missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"}
        )
    return user


User = Annotated[str, Depends(require_user)]

db = get_database(
    settings.database_backend,
    table_name=settings.dynamodb_table,
    region=settings.aws_region,
    project_id=settings.gcp_project,
    collection_name=settings.firestore_collection,
)
feed = ChangeFeed()

START_TIME = time.time()

# A write that loses a version race is retried against the fresh case this many times when
# the client did not pin a version. Every retry re-runs permission and transition checks.
MAX_WRITE_ATTEMPTS = 3
# Notes are pure appends that cannot be invalidated by a concurrent change, so they retry
# harder: giving up on one would only push the retry onto the user.
NOTE_WRITE_ATTEMPTS = 10


def backoff(attempt: int) -> None:
    """Randomised, growing pause before retry ``attempt`` (1-based), so writers that just
    collided do not collide again in lockstep. Endpoints are sync, so this blocks only the
    worker thread handling this request."""
    time.sleep(random.uniform(0, 0.004 * attempt))


# -- concurrency helpers -------------------------------------------------------------


def etag(case: Case) -> str:
    return f'"v{case.version}"'


def parse_if_match(value: str | None) -> int | None:
    """Accept ``"v3"``, ``W/"v3"``, ``v3`` or ``3``. None means the client pinned nothing."""
    if value is None or value.strip() in ("", "*"):
        return None
    raw = value.strip().removeprefix("W/").strip('"').removeprefix("v")
    if not raw.isdigit():
        raise HTTPException(400, f"If-Match must name a case version, got {value!r}")
    return int(raw)


def conflict(current: Case | None, expected: int) -> HTTPException:
    if current is None:
        return HTTPException(404, "Case not found")
    return HTTPException(
        409,
        {
            "message": (
                f"Case changed since you loaded it (you had version {expected}, "
                f"it is now version {current.version})."
            ),
            "current": current.model_dump(mode="json"),
        },
    )


def apply_write(
    case_id: str,
    if_match: int | None,
    build: Callable[[Case], dict | None],
    attempts: int = MAX_WRITE_ATTEMPTS,
) -> tuple[Case, Case]:
    """Read, decide, and conditionally write one case. Returns (before, after).

    ``build`` sees the freshly read case and returns the updates to apply, or None for a
    no-op. It raises HTTPException to refuse (403, 400). If another writer gets in between
    the read and the write, the storage layer raises VersionConflictError:

    * if the client pinned a version with If-Match, that is a 409 -- the user decided based
      on a state that no longer exists, so they must see the new state first;
    * otherwise the whole decision is re-made against the new state and retried, so an
      append (a note, a history entry) is never lost to a concurrent append.
    """
    for attempt in range(attempts):
        if attempt:
            backoff(attempt)
        case = db.get_case(case_id)
        if case is None:
            raise HTTPException(404, "Case not found")
        if if_match is not None and case.version != if_match:
            raise conflict(case, if_match)
        updates = build(case)
        if not updates:
            return case, case
        try:
            updated = db.update_case(case_id, updates, expected_version=case.version)
        except VersionConflictError as e:
            if if_match is not None:
                raise conflict(e.current, if_match) from None
            continue
        if updated is None:
            raise HTTPException(404, "Case not found")
        return case, updated
    raise HTTPException(409, "Case is being changed by others too fast; try again")


def publish_case(type_: str, user: str, case: Case, **change) -> None:
    feed.publish(
        type_,
        user,
        case_id=case.case_id,
        version=case.version,
        case=case.model_dump(mode="json"),
        change=change,
    )


# -- routes ----------------------------------------------------------------------------


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "backend": settings.database_backend,
        "live_clients": len(feed.online_users()),
    }


@app.get("/api/me")
def me(user: User):
    return {"user": user, "role": "admin" if is_admin(user) else "user"}


@app.get("/api/events")
async def events(
    request: Request,
    user: User,
    last_event_id: Annotated[str | None, Header()] = None,
):
    """Server-Sent Events stream of every change, for live views.

    Send ``Last-Event-ID`` on reconnect to be replayed what you missed. A ``resync`` event
    means the gap could not be filled and the client should reload.
    """
    return StreamingResponse(
        event_stream(feed, user, last_event_id, request.is_disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/presence")
def presence(user: User):
    return {"online": feed.online_users()}


@app.post("/api/cases", status_code=201)
def create_case(req: CaseCreate, user: User, response: Response):
    case = Case(
        product_id=req.product_id,
        category=req.category,
        marketplace=req.marketplace,
        description=req.description,
        source=req.source,
        gms=req.gms,
        assignee=user,
        created_by=user,
        status_history=[
            {
                "from": None,
                "to": "NEW",
                "timestamp": datetime.now(UTC).isoformat(),
                "changed_by": user,
            }
        ],
    )
    db.create_case(case)
    publish_case("case.created", user, case)
    response.headers["ETag"] = etag(case)
    return case.model_dump()


@app.get("/api/cases")
def list_cases(
    user: User,
    product_id: str | None = None,
    assignee: str | None = None,
    category: str | None = None,
    marketplace: str | None = None,
    status: str | None = None,
    is_high_value: bool | None = None,
    description_contains: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    page_size: int = 50,
    cursor: str | None = None,
):
    status_list = [Status(s.strip()) for s in status.split(",")] if status else None
    filters = CaseFilters(
        product_id=product_id,
        assignee=assignee,
        category=category,
        marketplace=marketplace,
        status=status_list,
        is_high_value=is_high_value,
        description_contains=description_contains,
        created_after=created_after,
        created_before=created_before,
        page_size=page_size,
        cursor=cursor,
    )
    page = db.list_cases(filters)
    return {
        "cases": [c.model_dump() for c in page.items],
        "cursor": page.cursor,
        "count": len(page.items),
        # The feed position this list is consistent with: a live client applies only
        # events after it, so nothing between the fetch and the subscription is lost.
        "as_of_event": feed.last_seq,
    }


@app.get("/api/cases/{case_id}")
def get_case(case_id: str, user: User, response: Response):
    case = db.get_case(case_id)
    if not case:
        raise HTTPException(404, "Case not found")
    response.headers["ETag"] = etag(case)
    return case.model_dump()


@app.patch("/api/cases/{case_id}")
def update_case(
    case_id: str,
    req: CaseUpdate,
    user: User,
    response: Response,
    if_match: Annotated[str | None, Header()] = None,
):
    requested = req.model_dump(exclude_none=True)

    def build(case: Case) -> dict | None:
        if not can_modify(user, case.assignee):
            raise HTTPException(403, "You can only modify your own cases")
        updates: dict = {}
        edits = list(case.edit_history)
        for field, new_value in requested.items():
            if field == "gms":
                old_value = case.gms.model_dump()
                new_value = GMS(**new_value).model_dump()
            else:
                old_value = getattr(case, field, None)
            if old_value == new_value:
                continue  # not a change, so not an edit and not an event
            updates[field] = new_value
            edits.append(
                EditRecord(
                    field=field,
                    old_value=str(old_value),
                    new_value=str(new_value),
                    changed_by=user,
                ).model_dump()
            )
        if not updates:
            return None
        updates["edit_history"] = edits
        return updates

    before, after = apply_write(case_id, parse_if_match(if_match), build)
    if after.version != before.version:  # a no-op PATCH writes nothing and announces nothing
        changed = [e["field"] for e in after.edit_history[len(before.edit_history) :]]
        publish_case("case.updated", user, after, fields=changed)
    response.headers["ETag"] = etag(after)
    return after.model_dump()


@app.post("/api/cases/{case_id}/status")
def transition_status(
    case_id: str,
    req: StatusTransition,
    user: User,
    response: Response,
    if_match: Annotated[str | None, Header()] = None,
):
    def build(case: Case) -> dict:
        if not can_modify(user, case.assignee):
            raise HTTPException(403, "You can only modify your own cases")
        if req.new_status not in VALID_TRANSITIONS.get(case.status, set()):
            allowed = [s.value for s in VALID_TRANSITIONS.get(case.status, set())]
            raise HTTPException(
                400,
                f"Invalid transition: {case.status.value} → {req.new_status.value}. "
                f"Allowed: {allowed}",
            )
        now = datetime.now(UTC).isoformat()
        history = list(case.status_history)
        history.append(
            {
                "from": case.status.value,
                "to": req.new_status.value,
                "timestamp": now,
                "changed_by": user,
            }
        )
        updates = {"status": req.new_status.value, "status_history": history}
        if req.resolution:
            updates["resolution"] = req.resolution
        if req.resolution_notes:
            updates["resolution_notes"] = req.resolution_notes
        return updates

    before, after = apply_write(case_id, parse_if_match(if_match), build)
    publish_case(
        "case.updated", user, after, status=[before.status.value, after.status.value]
    )
    response.headers["ETag"] = etag(after)
    return after.model_dump()


@app.post("/api/cases/{case_id}/notes")
def add_note(case_id: str, req: NoteAdd, user: User, response: Response):
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "A note cannot be empty")

    # Notes are append-only and independent of case state, so they never take If-Match:
    # a concurrent write is simply retried, and both notes survive.
    def build(case: Case) -> dict:
        notes = list(case.notes)
        notes.append({"text": text, "author": user, "timestamp": datetime.now(UTC).isoformat()})
        return {"notes": notes}

    _, after = apply_write(case_id, None, build, attempts=NOTE_WRITE_ATTEMPTS)
    publish_case("case.updated", user, after, note=True)
    response.headers["ETag"] = etag(after)
    return {"status": "added", "note_count": len(after.notes), "case": after.model_dump()}


@app.delete("/api/cases/{case_id}")
def delete_case(case_id: str, user: User, if_match: Annotated[str | None, Header()] = None):
    pinned = parse_if_match(if_match)
    for attempt in range(MAX_WRITE_ATTEMPTS):
        if attempt:
            backoff(attempt)
        case = db.get_case(case_id)
        if not case:
            raise HTTPException(404, "Case not found")
        if pinned is not None and case.version != pinned:
            raise conflict(case, pinned)
        if not can_delete(user, case.assignee):
            raise HTTPException(403, "You can only delete your own cases")
        try:
            deleted = db.soft_delete(case_id, user, expected_version=case.version)
        except VersionConflictError as e:
            if pinned is not None:
                raise conflict(e.current, pinned) from None
            continue
        if not deleted:
            raise HTTPException(404, "Case not found")
        feed.publish("case.deleted", user, case_id=case_id, product_id=case.product_id)
        return {"status": "deleted"}
    raise HTTPException(409, "Case is being changed by others too fast; try again")


@app.post("/api/import")
async def import_csv(user: User, file: UploadFile = File(...)):
    content = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))

    cases = []
    errors = []
    for i, row in enumerate(reader):
        try:
            gms = GMS(
                shipped_units=float(row.get("shipped_units", 0) or 0),
                unit_price=float(row.get("unit_price", 0) or 0),
                shipping_charge=float(row.get("shipping_charge", 0) or 0),
                gift_wrap_charge=float(row.get("gift_wrap_charge", 0) or 0),
                price_discount=float(row.get("price_discount", 0) or 0),
                shipping_discount=float(row.get("shipping_discount", 0) or 0),
                gift_wrap_discount=float(row.get("gift_wrap_discount", 0) or 0),
            )
            case = Case(
                product_id=row["product_id"],
                category=row.get("category", "general"),
                marketplace=row.get("marketplace", "US").split(","),
                description=row.get("description", ""),
                source=row.get("source", "import"),
                gms=gms,
                assignee=user,
                created_by=user,
            )
            cases.append(case)
        except Exception as e:
            errors.append({"row": i + 2, "error": str(e)})

    result = db.batch_import(cases)
    if result.created:
        # One event for the batch: clients reload rather than receive N case payloads.
        feed.publish("cases.imported", user, count=result.created)
    return {"imported": result.created, "errors": errors + result.errors}


@app.get("/api/export")
def export_cases(
    user: User,
    format: str = "csv",
    assignee: str | None = None,
    category: str | None = None,
    status: str | None = None,
):
    status_list = [Status(s.strip()) for s in status.split(",")] if status else None
    filters = CaseFilters(assignee=assignee, category=category, status=status_list, page_size=10000)
    page = db.list_cases(filters)

    if format == "json":
        return [c.model_dump() for c in page.items]

    output = io.StringIO()
    fieldnames = [
        "case_id", "product_id", "category", "marketplace", "status", "assignee",
        "description", "t4w_gms", "is_high_value", "created_at",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for c in page.items:
        writer.writerow({
            "case_id": c.case_id,
            "product_id": c.product_id,
            "category": c.category,
            "marketplace": ",".join(c.marketplace),
            "status": c.status.value,
            "assignee": c.assignee,
            "description": c.description[:100],
            "t4w_gms": c.gms.t4w_gms,
            "is_high_value": c.gms.is_high_value,
            "created_at": c.created_at,
        })

    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=cases_export.csv"},
    )


@app.get("/api/stats")
def dashboard_stats(user: User):
    stats = db.get_stats()
    return {
        "total_open": stats.total_open,
        "resolved_today": stats.resolved_today,
        "created_today": stats.created_today,
        "high_value_open": stats.high_value_open,
        "by_status": stats.by_status,
        "by_category": stats.by_category,
    }


@app.get("/api/categories")
def list_categories(user: User):
    path = Path(__file__).parent.parent.parent / "data" / "categories.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return []
