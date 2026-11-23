# Docket — Technical Architecture

## Overview

Docket is a case lifecycle management system for compliance operations. It tracks products through investigation states (NEW → IN_PROGRESS → UNDER_REVIEW → RESOLVED/ESCALATED/CLOSED), computes Trailing 4-Week GMS to automatically flag high-value listings ($1,000+ threshold), enforces role-based access control (admin vs user ownership), and provides pluggable storage backends (DynamoDB or Firestore) switchable at runtime via environment variable.

**Design principle:** Ownership-first access control. Users own cases by default (assignee = the user identified by the request's bearer token), admins bypass ownership checks, and the storage layer is an abstraction — switching clouds costs one environment variable.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      BROWSER (React 18 SPA)                               │
│                                                                         │
│  ┌───────────────────┐   ┌────────────────┐   ┌──────────────────────┐ │
│  │ Dashboard Stats    │   │ Case List      │   │ Detail / Create      │ │
│  │ - Open count       │   │ - Filters      │   │ - Form validation    │ │
│  │ - Resolved today   │   │ - High Value   │   │ - Live GMS compute   │ │
│  │ - Created today    │   │   badge        │   │ - Category search    │ │
│  │ - By status/cat    │   │ - Pagination   │   │ - Status transitions │ │
│  └─────────┬─────────┘   └───────┬────────┘   └──────────┬───────────┘ │
│            │                      │                        │             │
│            └──────────────────────┼────────────────────────┘             │
│                                   │ HTTP                                 │
└───────────────────────────────────┼──────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      FASTAPI (Port 8000)                                  │
│                                                                         │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │ Endpoints:                                                         │  │
│  │   GET  /health                → Service health + backend type      │  │
│  │   GET  /api/me                → Current user + role                │  │
│  │   POST /api/cases             → Create case (assignee=current_user)│  │
│  │   GET  /api/cases?filters     → List + filter by status/category   │  │
│  │   GET  /api/cases/{id}        → Fetch case by ID                   │  │
│  │   PATCH /api/cases/{id}       → Update (ownership check)           │  │
│  │   POST /api/cases/{id}/status → State transition + validation      │  │
│  │   POST /api/cases/{id}/notes  → Append note (immutable)            │  │
│  │   DELETE /api/cases/{id}      → Soft delete (ownership check)      │  │
│  │   POST /api/import            → Batch CSV import                   │  │
│  │   GET  /api/export?filters    → CSV/JSON export                    │  │
│  │   GET  /api/stats             → Dashboard aggregates               │  │
│  │   GET  /api/categories        → 74 product categories from JSON    │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌─────────────────┐       ┌──────────────────┐                        │
│  │ Auth Middleware │       │ Pydantic Models   │                        │
│  │ - is_admin()    │       │ - CaseCreate      │                        │
│  │ - can_modify()  │       │ - CaseUpdate      │                        │
│  │ - can_delete()  │       │ - GMS computed    │                        │
│  │   (owner check) │       │   properties      │                        │
│  └─────────────────┘       │ - Status enum     │                        │
│                            │ - VALID_TRANSITIONS│                        │
│                            └──────────────────┘                        │
└─────────────────────────────────┬───────────────────────────────────────┘
                                  │
                 ┌────────────────┴────────────────┐
                 │  DatabaseBackend (ABC)          │
                 │  - create_case()                │
                 │  - get_case()                   │
                 │  - list_cases()                 │
                 │  - update_case()                │
                 │  - soft_delete()                │
                 │  - batch_import()               │
                 │  - get_stats()                  │
                 └────────────────┬────────────────┘
                                  │
                 ┌────────────────┴────────────────┐
                 │                                 │
                 ▼                                 ▼
┌─────────────────────────────┐   ┌─────────────────────────────┐
│   DynamoDBBackend            │   │   FirestoreBackend           │
│   - boto3 resource           │   │   - google-cloud-firestore   │
│   - table.put_item()         │   │   - collection.document()    │
│   - table.scan()             │   │   - query.where()            │
│   - table.update_item()      │   │   - batch.set()              │
│   - FilterExpression (Attr)  │   │   - order_by()               │
│   - JSON↔Decimal serializer  │   │   - start_after() pagination │
└─────────────┬───────────────┘   └─────────────┬───────────────┘
              │                                 │
              ▼                                 ▼
┌─────────────────────────────┐   ┌─────────────────────────────┐
│   AWS DynamoDB               │   │   Google Firestore           │
│   Table: Docket           │   │   Collection: cases          │
│   PK: case_id                │   │   Doc ID: case_id            │
│   Scan + FilterExpression    │   │   Composite indexes          │
│   No GSIs implemented        │   │   Writes in a transaction    │
└─────────────────────────────┘   └─────────────────────────────┘
```

---

## Components

### 1. Frontend (`frontend/`)

**index.html** — Single-page app shell with Tailwind via CDN (v3), React 18 UMD, Babel standalone for JSX transpilation. Dark mode forced via `<html class="dark">`. Form controls are styled in a literal `<style>` block rather than with utility classes, because the CDN build generates utilities at runtime and can miss React-mounted markup — see Form Control Visibility below and ADR-002.

**App.jsx** — Single-file React app with 4 views: list, detail, create, import. State managed in top-level `<App>` via hooks. API calls use native `fetch()` with localhost:8000 hardcoded.

| View | Key Features |
|:--|:--|
| Dashboard | 4 stat cards: total open, resolved today, created today, high value open. Loads from `/api/stats` |
| Case List | Filters (product ID, assignee, category, status, high-value-only). Click row → detail. Gold left border for high-value cases |
| Case Detail | Status badge, product ID, GMS total, description, status timeline (all transitions), notes (append-only), delete button (if owner or admin) |
| Create Form | 74-category searchable dropdown (filters as you type), GMS live calculator (updates on every keystroke), HIGH VALUE badge appears at $1,000+ |
| Import CSV | File upload → `/api/import`, shows success count + error list |
| Export | Fetches `/api/export?format=csv&filters`, triggers download |

### 2. API Layer (`src/case_forge/api.py`)

FastAPI app with CORS middleware (allow-all origins for local dev). All endpoints use Pydantic models for request/response validation. Observability via Python's `logging` module (structured JSON format, configured in `logging.basicConfig()`).

| Method | Path | Auth Required | Purpose |
|:--|:--|:--|:--|
| GET | `/health` | No | Returns uptime, backend type, current user |
| GET | `/api/me` | Token | Returns current user login + role (admin/user) |
| POST | `/api/cases` | No | Creates case, sets assignee=current_user, adds initial status_history entry |
| GET | `/api/cases` | No | List + filter by product_id, assignee, category, marketplace, status (comma-separated), is_high_value, description_contains, created_after, created_before. Pagination via cursor |
| GET | `/api/cases/{id}` | No | Fetch single case by case_id. 404 if not found or soft-deleted |
| PATCH | `/api/cases/{id}` | Yes (owner/admin) | Update any field except product_id. Each change appends to edit_history with old/new values |
| POST | `/api/cases/{id}/status` | Yes (owner/admin) | Transition to new_status. Validates against VALID_TRANSITIONS. Adds entry to status_history |
| POST | `/api/cases/{id}/notes` | No | Appends note with author=current_user, timestamp. Notes are immutable (no edit/delete) |
| DELETE | `/api/cases/{id}` | Yes (owner/admin) | Soft delete: sets is_deleted=True, deleted_by, deleted_at. Case disappears from queries |
| POST | `/api/import` | No | Accepts CSV file (multipart). Parses rows → CaseCreate → batch_import(). Returns created count + errors |
| GET | `/api/export` | No | Accepts same filters as `/api/cases`. Returns CSV (default) or JSON. StreamingResponse with utf-8-sig BOM |
| GET | `/api/stats` | No | Aggregates: total_open, resolved_today, created_today, high_value_open, by_status dict, by_category dict |
| GET | `/api/categories` | No | Reads `data/categories.json` (74 product categories), returns as JSON array |

### 3. Auth (`src/case_forge/auth.py`)

Three functions, all stateless:

```python
def is_admin(user: str) -> bool
def can_modify(user: str, case_assignee: str) -> bool
def can_delete(user: str, case_assignee: str) -> bool
```

Admin list comes from `settings.admin_users` (comma-separated string, default `"admin"`). The current user is resolved per request from `Authorization: Bearer <token>` via `settings.token_map` (`CF_API_TOKENS`, `token:user` pairs, compared in constant time). `CF_LOCAL_USER` enables single-user local mode, where a request with no header acts as that user. An earlier version took the user from `getpass.getuser()` — the server's OS user — which made every client the same identity and the ownership checks vacuous. Production deployment would need OAuth/JWT integration (see Known Gaps).

### 4. Models (`src/case_forge/models.py`)

**GMS** — Pydantic model with 7 float fields (shipped_units, unit_price, shipping_charge, gift_wrap_charge, price_discount, shipping_discount, gift_wrap_discount). Two computed properties:
- `t4w_gms: float` — sum formula: `(units × price) + shipping + gift_wrap + price_disc + ship_disc + gift_disc`, rounded to 2 decimals
- `is_high_value: bool` — True if t4w_gms ≥ 1000.0

**Status** — Enum with 7 states: NEW, IN_PROGRESS, UNDER_REVIEW, RESOLVED, ESCALATED, CLOSED, REOPENED.

**VALID_TRANSITIONS** — Dict mapping current status to set of allowed next statuses:
- NEW → {IN_PROGRESS}
- IN_PROGRESS → {UNDER_REVIEW, ESCALATED}
- UNDER_REVIEW → {RESOLVED, ESCALATED, CLOSED}
- RESOLVED → {REOPENED}
- ESCALATED → {CLOSED}
- REOPENED → {IN_PROGRESS}
- CLOSED → {} (terminal state)

**Case** — Full document model with UUID primary key (`case_id`), product_id (immutable), category, marketplace (list), description, source, assignee, status, gms (nested GMS model), resolution, resolution_notes, notes (list of dicts), status_history (list of dicts), edit_history (list of EditRecord dicts), timestamps (created_at, updated_at), soft-delete flags (is_deleted, deleted_by, deleted_at), created_by.

### 5. Storage Backends (`src/case_forge/database/`)

**base.py** — Abstract base class `DatabaseBackend` with 7 methods. Three helper classes: `BatchResult` (created count + errors list), `CasePage` (items + cursor + total), `DashboardStats` (7 aggregate fields).

**dynamodb.py** — boto3 implementation:
- Uses `boto3.resource("dynamodb")` + `Table` API (not low-level client)
- `create_case()` → `table.put_item()` with `attribute_not_exists(case_id)`, so a create never overwrites
- `get_case()` → `table.get_item()`, filters out soft-deleted
- `list_cases()` → `table.scan()` with `FilterExpression` (Attr chaining with `&`). Description filtering done client-side (scan returns items, then Python filters by substring)
- `update_case()` / `soft_delete()` → one `update_item()` that SETs the fields plus `version + 1`, with a `ConditionExpression` on the expected version (see Concurrency and Live Sync); `ReturnValues=ALL_NEW` returns the written case without a second read
- High-value filtering and counting read `gms.is_high_value`, the nested attribute create actually writes. They used to read a top-level `is_high_value` that only updates wrote, so freshly created high-value cases were missing from both
- `batch_import()` → `table.batch_writer()` context manager (auto-batches in chunks of 25)
- `get_stats()` → `table.scan()`, filters by is_deleted=False, aggregates in Python (no DynamoDB aggregation queries)
- Decimal serialization: Python floats → Decimal for DynamoDB (floats not supported), converts via `json.dumps()` + `json.loads(..., parse_float=Decimal)`

**firestore.py** — google-cloud-firestore implementation:
- Uses `firestore.Client()` + `collection()` API
- `create_case()` → `collection.document(case_id).create()`, which fails if the id exists (`set()` would overwrite)
- `get_case()` → `document(case_id).get()`, checks `doc.exists`
- `list_cases()` → `collection.where(...).order_by(...).limit(...)` chaining. Firestore supports `where("status", "in", [list])` for multi-value filters. Product ID and description filters done client-side (Firestore "in" limited to 10 values, partial string match not indexed)
- `update_case()` / `soft_delete()` → a `@transactional` read-compare-update on `version`
- `batch_import()` → `db.batch()` (max 500 ops per batch, auto-commits when threshold reached)
- `get_stats()` → `collection.where("is_deleted", "==", False).stream()`, aggregates in Python
- No Decimal serialization needed — Firestore stores floats natively

**memory.py** — In-process implementation of the same contract (lock + compare-and-swap, deep copies in and out). Backs `CF_DATABASE_BACKEND=memory` for demos and the API/concurrency tests.

**__init__.py** — Factory function `get_database(name: str, **kwargs)` with BACKENDS registry dict. Raises ValueError if unknown backend name.

| Backend | Key Design | Consistency | Query Strategy | When to Use |
|:--|:--|:--|:--|:--|
| DynamoDB | Scan-based (no GSIs implemented). FilterExpression on every list_cases(). PK = case_id only. | Eventual (default) | Scan entire table + filter in request. Client-side description/product_id substring match. | AWS-native deployments. <10K cases. Serverless Lambda + DynamoDB combo. |
| Firestore | Document model. Composite indexes for common filters (status, assignee, category). where() + order_by() chaining. | Strong by default | Indexed queries for structured fields. Client-side partial string match. | Google Cloud deployments. `on_snapshot()` listeners are a natural shared feed for multi-instance live sync. Multi-region replication out-of-box. |
| Memory | Dict behind a lock. | Strong (single process) | Python filtering and sorting. | Demos, local development, tests. Data lasts until the process stops. |

---

## Concurrency and Live Sync

### The defect this replaces

Every mutating endpoint used to read the case, change it in memory, and write the whole case
back with no condition. Two concurrent writers therefore produced lost updates:

* two notes added at once: both requests read N notes, both write N+1, and one note vanishes
  while both clients get `200`;
* two status changes at once: both read `NEW`, both pass the `NEW → IN_PROGRESS` check, and
  both write, leaving a history the state machine says is impossible.

Measured under contention (eight threads, 24 notes, one case), the unconditional path returned
`200` for all 24 requests and stored 7.

### Optimistic concurrency

`Case.version` starts at 1 and increments on every write. The storage contract is
`update_case(case_id, updates, expected_version)` and `soft_delete(case_id, by, expected_version)`:
the write applies only if the stored case is still at `expected_version`. Otherwise it raises
`VersionConflictError` carrying the current case.

| Backend | How the condition is enforced |
|:--|:--|
| DynamoDB | One `update_item` with `ConditionExpression`: `attribute_exists(case_id) AND is_deleted = false AND version = :expected`. Items written before versioning have no `version`; for `expected == 1` the condition also accepts `attribute_not_exists(version)`. `create_case` uses `attribute_not_exists(case_id)` so a create can never overwrite. |
| Firestore | `@transactional`: read, compare `version`, `update`. Firestore retries the transaction if the document changes between read and commit. `create_case` uses `create()`, which fails on an existing id, instead of `set()`, which overwrites. |
| Memory | A lock around compare-and-swap; values are deep-copied in and out so callers cannot mutate stored state. |

Because the condition is evaluated by the database, it holds across any number of API instances.

`api.apply_write(case_id, if_match, build)` is the single write path:

1. read the case (404 if missing);
2. if the client sent `If-Match` and the version differs, answer `409` with the current case;
3. call `build(case)`, which enforces ownership and the transition rules against **this** read
   and returns the updates (or `None` for a no-op, which writes nothing and announces nothing);
4. conditionally write. On `VersionConflictError`, a pinned request gets `409`; an unpinned one
   backs off with jitter and repeats from step 1, re-making the whole decision.

Notes are pure appends, so they never take `If-Match` and get more attempts (10 against the
default 3). Under the same eight-thread load, 22–23 of 24 notes now succeed, and every `200`
is stored.

### Change feed (`events.py`)

* `publish()` is called after each successful write, from FastAPI's sync worker threads. It
  assigns `id = "<epoch>:<seq>"`, appends to a 1,000-event ring buffer, and hands the event
  to each subscriber's event loop with `call_soon_threadsafe`.
* `GET /api/events` (`event_stream`) subscribes, replays the events after `Last-Event-ID`,
  sends `hello` (who you are, who is online), then streams live events with a 15-second
  heartbeat comment. Events already sent during replay are skipped by `seq`.
* `resync` is sent instead of a replay when the gap cannot be filled: the id is from another
  epoch (the server restarted, so `seq` restarted), it is older than the buffer, it is from
  the future, or it is malformed. It is also sent when a subscriber's bounded queue (500)
  overflows. Publishers never block on a slow client.
* Presence: subscribing and unsubscribing broadcast the online list as ephemeral events (no
  id, not buffered), so they never count as a missed change. The newcomer learns the list from
  its own `hello`.
* `GET /api/cases` returns `as_of_event`, the feed position the list reflects. The client
  ignores events at or before it.

The browser reads the stream with `fetch`, not `EventSource`, because `EventSource` cannot send
an `Authorization` header. It reconnects with exponential backoff and jitter, and sends
`Last-Event-ID` itself.

### Client rules that matter

* **An edit is pinned to the version it started from.** Live updates advance the case on
  screen, so pinning the on-screen version at save time would accept whatever changed under
  the draft. This was found by driving the UI, not by the unit tests. The detail view now
  warns as soon as the case moves past the draft's base version, and on `409` keeps the
  draft and shows the latest saved text beside it.
* A change the tab did not make itself is shown as news on the open case, including your own
  edit from another device. Tabs keep the token in `sessionStorage`, so two tabs can be two
  people.

### Scaling past one process

The feed is per process. With several API instances, each one's clients see only that
instance's writes. Live updates would be incomplete, but no data would be lost, because the
version checks live in the database. The `publish`/`subscribe` interface is the seam for a
shared feed:

* **DynamoDB Streams** → a consumer that calls `publish` on every instance (or fans out through
  a broker). Stream records carry the new image, which is exactly the `case` payload.
* **Firestore** `on_snapshot` on the collection, per instance.
* **Redis pub/sub** between instances, with the ring buffer moved to a Redis stream so
  `Last-Event-ID` replay works across instances.

---

## Data Flow

```
1. User fills Create form in browser (product ID, category, marketplace, description, GMS components)
2. Browser computes GMS live: ((units × price) + charges + discounts), shows "HIGH VALUE" badge if ≥$1000
3. User clicks "Create Case"
4. Frontend POST /api/cases with JSON body: {product_id, category, marketplace, description, source="manual", gms={...}}
5. FastAPI endpoint validates via CaseCreate Pydantic model (required fields, types)
6. API constructs Case model: generates UUID, sets assignee=the request's user (from its bearer token), status=NEW, adds initial status_history entry
7. GMS Pydantic model computes t4w_gms and is_high_value as @computed_field properties
8. API calls db.create_case(case) → backend.create_case()
9. DynamoDBBackend serializes Case to dict (floats → Decimal), calls table.put_item()
10. DynamoDB stores document with PK=case_id
11. Backend returns Case model, API returns JSON response with 201 status
12. Frontend receives response, calls loadCases() + loadStats(), navigates to list view
13. User sees new case in list with status badge (blue "NEW"), GMS total, gold border if high-value
```

---

## Data Model and Auth Design

### Partition and Sort Key Design

DynamoDB backend uses **case_id as partition key only** — no sort key, no GSIs. This means:
- Get-by-ID: O(1) via `table.get_item(Key={"case_id": id})` — fast
- List-all-cases: requires `table.scan()` with FilterExpression — scans entire table, then filters server-side by DynamoDB, then filters again client-side for substring matches (product_id, description). At <10K cases this is acceptable (scan completes in <500ms). Above 10K, you'd need GSIs:
  - GSI-1: PK=assignee, SK=created_at (query cases by owner, sorted by time)
  - GSI-2: PK=status, SK=created_at (query by status, sorted by time)
  - GSI-3: PK=category#is_high_value, SK=created_at (query high-value cases in category)
- Query by status + assignee + category: impossible efficiently — must scan. Combining 3 filters (status, assignee, category) means you'd ideally use a composite key, but DynamoDB limits you to 2 key attributes per index. Solution: use Firestore (supports composite indexes with arbitrary field combinations) or accept scan latency.

Firestore backend has no explicit key design — doc ID is case_id. Queries use **composite indexes** (configured in `firestore.indexes.json` or auto-created):
- where("assignee", "==", X).where("status", "in", [Y, Z]).order_by("created_at")
- where("category", "==", X).where("gms.is_high_value", "==", True)

Both backends filter `is_deleted=False` on every query — soft-deleted cases are invisible but retained for audit.

### Storage Layer Abstraction

The `DatabaseBackend` ABC defines 7 methods. Why abstract it?
1. **Local dev vs cloud**: DynamoDB requires AWS credentials + table provisioning. Firestore requires Google Cloud project. A SQLite backend would be trivial to add (inherit from ABC, implement 7 methods, register in BACKENDS dict). The application code (`api.py`) never imports `DynamoDBBackend` directly — it calls `get_database(name)`.
2. **Testing**: Unit tests mock `DatabaseBackend` instead of boto3. API and concurrency tests run against `MemoryBackend`, which implements the same contract, and the DynamoDB backend runs against moto.
3. **Cost**: DynamoDB charges per request (scan is expensive at scale). Firestore charges per document read (where() queries are cheaper than full scans). If you hit DynamoDB's cost ceiling, switching to Firestore is one env var + pip install.

What each backend costs you:
- **DynamoDB**: Scan-based queries are slow at >10K cases (need GSIs to fix, not implemented). Decimal serialization is awkward (boto3 rejects Python floats). Live sync works on every backend through the API's change feed; spanning several API instances would need DynamoDB Streams (see Concurrency and Live Sync).
- **Firestore**: Requires Firestore-specific index configuration (composite indexes must be pre-declared or auto-created on first query). google-cloud-firestore is a heavy dependency (100+ MB). Strong consistency is default but costs more than eventual.

### Pydantic Validation Boundary

Pydantic models (`CaseCreate`, `CaseUpdate`, `StatusTransition`) enforce the contract between client and server:
- **Client error (HTTP 422)**: Missing required field, wrong type (string for int), invalid enum value (status="INVALID"), negative GMS component. FastAPI auto-returns 422 with detailed error dict — no code in `api.py` handles this.
- **Server error (HTTP 500)**: Backend throws exception (DynamoDB throttling, Firestore permission denied, boto3 credentials missing). FastAPI catches unhandled exceptions → 500.
- **Business rule error (HTTP 400)**: Invalid state transition (NEW → RESOLVED), ownership violation (user tries to edit admin's case). These are `raise HTTPException(400, msg)` in `api.py`.

Pydantic computed fields (`GMS.t4w_gms`, `GMS.is_high_value`) run **on read and write**. When you deserialize a Case from DynamoDB JSON, Pydantic recomputes `is_high_value` from the stored GMS components. This means you can never have a stale high-value flag — it's always derived from the 7 GMS floats. The database stores the components, not the computed total.

### Auth Token Design (What's NOT Implemented)

Current auth: static per-user bearer tokens from `CF_API_TOKENS`; `settings.admin_users` is a comma-separated string from env var. No login form, no sessions, no expiry, no JWT.

Production deployment would need:
1. **OAuth2 + JWT**: FastAPI has `OAuth2PasswordBearer` and `JWTBearer` dependencies. You'd add a `/api/login` endpoint (returns JWT signed with HS256), verify JWT in a `get_current_user()` dependency (injected into every endpoint), store user roles in JWT claims.
2. **Password storage**: bcrypt hash + salt. Never store plaintext. Use passlib or argon2-cffi.
3. **Refresh tokens**: JWT expiry (15 min access token, 7-day refresh token). Refresh endpoint rotates tokens (old refresh invalidated, new pair issued). Requires token revocation list (Redis or database).
4. **CORS tightening**: Replace `allow_origins=["*"]` with explicit domain whitelist.
5. **HTTPS enforcement**: Redirect HTTP → HTTPS. Set `Secure` and `HttpOnly` flags on session cookies.

Why not implement these now? This is a portfolio project demonstrating case management, state machines, and multi-cloud storage — not an auth framework showcase. Adding a full auth stack would triple the codebase size and obscure the core logic.

### Form Control Visibility (ADR-002)

Text inputs originally rendered with a near-white background and near-white text — present and focusable, but invisible while typing. Two causes stacked:

1. **User-agent styles on form controls are stubborn.** Browsers ship their own `background-color` and `color` for `input`, `textarea` and `select`, and those defaults do not always lose to a low-specificity utility class the way an ordinary `div` would.
2. **Tailwind here is the CDN build, which generates utilities at runtime.** The browser script scans the DOM and emits CSS on the fly. For markup React mounts after that scan, a utility such as `bg-white/3` can be absent entirely, leaving the control on its default background. The failure depends on mount timing, so it presents as an intermittent rendering glitch rather than a CSS problem.

Both paths land on the same result, which is what made it hard to spot.

Fix — declare form controls in a literal `<style>` block with `!important` on the three properties that were losing:

```css
input, textarea, select {
    background: #111827 !important;
    border: 1px solid #1f2937 !important;
    color: #e5e7eb !important;
}
input::placeholder, textarea::placeholder { color: #4b5563 !important; }
select option { background: #111827; color: #e5e7eb; }
```

`select option` needs its own rule because native dropdown items are rendered by the OS and do not inherit the parent `select` colours — without it, the open dropdown reverts to light styling on a dark theme.

Card surfaces still use translucency, but written as literal `rgba(255,255,255,0.03)` in the same stylesheet rather than as a generated utility, so no visible surface depends on the runtime scan.

Six `!important` declarations is normally a smell. The honest fix is a real Tailwind build step with a form plugin, which removes both the runtime-generation problem and the need for the overrides. That is deferred deliberately: a build step costs the "open the file and it runs" property. See ADR-002.

---

## Configuration

All settings via environment variables with `CF_` prefix (Pydantic `SettingsConfigDict`):

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CF_")

    database_backend: str = "dynamodb"  # "dynamodb" | "firestore"
    aws_region: str = "us-east-1"
    dynamodb_table: str = "Docket"
    gcp_project: str = ""
    firestore_collection: str = "cases"

    api_tokens: str = ""      # "tok-alice:alice,tok-bob:bob" -> settings.token_map
    local_user: str = ""      # single-user local mode; loopback only
    admin_users: str = "admin"
    cors_origins: str = "http://localhost:3000"

    @property
    def admin_list(self) -> list[str]:
        return [u.strip() for u in self.admin_users.split(",") if u.strip()]

    log_level: str = "INFO"
```

Override any field via env var: `CF_DATABASE_BACKEND=firestore CF_ADMIN_USERS=alice,bob uvicorn ...`

---

## Running

No console entry point in `pyproject.toml` — start API directly with uvicorn:

```bash
# Install
pip install -e ".[dev]"

# DynamoDB backend (default, requires AWS credentials configured)
CF_DATABASE_BACKEND=dynamodb \
CF_AWS_REGION=us-east-1 \
CF_DYNAMODB_TABLE=Docket \
CF_ADMIN_USERS=admin,alice \
uvicorn case_forge.api:app --port 8000 --reload

# Firestore backend (requires google-cloud-firestore installed)
pip install -e ".[firestore]"
CF_DATABASE_BACKEND=firestore \
CF_GCP_PROJECT=my-project-id \
CF_FIRESTORE_COLLECTION=cases \
uvicorn case_forge.api:app --port 8000

# Frontend (separate terminal, port 3000)
cd frontend
python -m http.server 3000
# Open http://localhost:3000
```

---

## Testing

```bash
pytest tests/ -v                          # All tests (14 tests total)
pytest tests/unit/ -v                     # Fast, no I/O (2 files: test_auth.py, test_models.py)
pytest tests/ --cov=src --cov-report=html # Coverage report
```

Tests cover:
- **test_models.py**: GMS computation (boundary at $1000, all 7 components, zero values), status transition validation (NEW→IN_PROGRESS allowed, NEW→RESOLVED denied, CLOSED→* denied)
- **test_auth.py**: Admin detection, can_modify/can_delete ownership checks (admin bypasses, non-admin restricted to own cases)

No integration tests (would need local DynamoDB or Firestore emulator), no API tests (would need `TestClient` from FastAPI). See Known Gaps.

---

## Known Gaps

1. **Two ADRs, not full coverage**: `docs/adr/` now records the storage abstraction (ADR-001) and the form-control CSS decision (ADR-002). Still unrecorded: soft delete vs hard delete, and the choice to ship without GSIs.
2. **No Firestore tests**: API tests run through `TestClient`, and the DynamoDB backend runs against moto, but the Firestore transaction path needs the Firestore emulator and is untested.
3. **No GSIs on DynamoDB**: Current implementation scans entire table on every list_cases(). GSIs for assignee, status, category would reduce query time from O(n) to O(log n).
4. **Live sync is single-process**: the change feed lives in the API process, so with several instances each client sees only its own instance's writes. Version checks are unaffected because the database enforces them. See Concurrency and Live Sync for the shared-feed options.
5. **No pagination UI**: Backend returns cursor, frontend ignores it. List view shows first 50 cases only.
6. **No password reset, no OAuth**: Auth is static per-user bearer tokens from configuration. Production needs login form, JWT, refresh tokens, password reset flow (see Auth Token Design section).
7. **No audit log export**: edit_history and status_history are stored per-case, but there's no `/api/audit` endpoint to query "all changes by user X in last 30 days" across all cases.
8. **No rate limiting**: API has no throttling. A user can POST /api/cases 1000 times in 1 second. Production needs per-user rate limits (10 req/sec) via middleware.
9. **No HTTPS in frontend API calls**: `const API = "http://localhost:8000"` is hardcoded. Production needs `https://` + env var injection (Vite/Webpack env or build-time substitution).
10. **No CloudFormation/Terraform**: Deploying to AWS requires manual DynamoDB table creation (`aws dynamodb create-table ...`). Missing: IaC templates for table + Lambda + API Gateway.
