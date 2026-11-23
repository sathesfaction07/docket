# Docket

Case lifecycle management for compliance operations. Tracks products through investigation states, computes Trailing 4-Week GMS to flag high-value listings, enforces per-user ownership (admin vs user) with each request identified by its own bearer token, and keeps every open browser in sync: changes are pushed live, and concurrent edits are version-checked so none is silently lost. Storage switches at runtime between DynamoDB (AWS), Firestore (Google Cloud), and an in-memory backend that needs no cloud account.

---

## The Problem

A compliance team handles hundreds of product cases weekly. Each case has a product, a category, marketplace exposure, and financial significance (some products generate $50K/month in sales — those need priority). Cases move through states: someone creates it, someone investigates, someone resolves or escalates. Multiple people work simultaneously. The current workflow: a shared spreadsheet with no access control, no audit trail, no real-time sync, and manual GMS lookups.

---

## How It Works

**Case creation:** User fills in product ID, selects category from a searchable 74-category dropdown (a general marketplace product taxonomy), picks marketplaces, describes the issue, and enters GMS components. The system computes T4W GMS live:

```
GMS = (shipped_units × unit_price) + shipping_charge + gift_wrap_charge 
      + price_discount + shipping_discount + gift_wrap_discount
```

If GMS ≥ $1,000 the case is flagged HIGH VALUE — visually distinct in the UI (gold border, badge) and filterable.

**State machine:** Cases follow a defined lifecycle:
```
NEW → IN_PROGRESS → UNDER_REVIEW → RESOLVED / ESCALATED / CLOSED
RESOLVED → REOPENED → IN_PROGRESS (allows re-investigation)
```

Every transition is recorded: who, when, from what state to what state.

**Access control:**
- Every request is identified by its own bearer token (`Authorization: Bearer <token>`), mapped to a user by `CF_API_TOKENS`. Requests without a valid token get `401`.
- Assignee, author and audit fields are set from that per-request user, never from the server process
- Single-user local mode: `CF_LOCAL_USER=alice` lets requests with *no* token act as `alice`. `setup.sh` uses this by default and binds to `127.0.0.1`; never enable it on a shared host
- Admin: can view, edit, delete any case
- User: can view/search/export everything, but can only edit/delete their own cases

**Immutability rules:**
- Product ID cannot be changed after creation (only the whole case can be deleted)
- Notes are append-only (no editing/deleting past notes)
- Every field edit is tracked: old value, new value, who changed it, when

**Search:** All cases are searchable by any combination of: Product ID (partial), Assignee, Category, Marketplace, Status, High Value flag, Description (contains). Filters combine with AND logic.

---

## Live Collaboration

Several people work the same queue at once, so Docket treats concurrent use as the normal case rather than an edge case.

**Every change is pushed to every open browser.** The API publishes an event for each successful write. Browsers hold a Server-Sent Events stream (`GET /api/events`). An open list updates rows in place and briefly highlights them; an open case updates its status, notes and history without a reload. A toast says who did what ("bob moved SKU-003 NEW → IN_PROGRESS"), and the header shows who else is online.

**A dropped connection does not mean a stale screen.** Each event has an ordered id. On reconnect the browser sends `Last-Event-ID` and the server replays exactly what it missed. When it cannot (the gap is older than its buffer, or the server restarted), it sends `resync` and the browser reloads rather than silently missing changes. A client that falls too far behind gets the same treatment, so one slow tab never holds up the others.

**No write silently overwrites another.** This matters more than the live view. Previously every write read the case, changed it in memory and wrote the whole case back, so two people adding notes at the same moment lost one of them, and two people moving a case out of `NEW` could both succeed. Now every case carries a `version`, and every write is conditional on the version it read: a DynamoDB `ConditionExpression`, a Firestore transaction, or a locked compare in memory. When a write loses that race:

| The write | What happens |
|:--|:--|
| Has `If-Match` (the UI sends it for edits, status changes and deletes) | `409 Conflict` with the current case. The user decided on a state that no longer exists, so they see the new state first. An edit in progress keeps its draft and shows the latest saved text beside it. |
| Has no `If-Match` | The API re-reads and re-decides, including the permission check and the transition rule, then retries. A note is a pure append, so it retries harder and both notes survive. |

The UI pins an edit to the version it **started** from, not the version on screen at save time. Live updates keep the screen current, so pinning at save time would quietly accept whatever changed under the draft. The browser test that found this is described below.

**Measured, not asserted.** In the test suite, eight threads add 24 notes to one case, with a pause between each read and its write so the races really happen. With version checks, every request that returns `200` has its note stored, and the few that exhaust their retries say so with `409`. A control test runs the same load against a backend that ignores versions: all 24 requests return `200`, and notes are still lost. In the run that set the test's bounds, 17 of the 24 were lost.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  React frontend (Tailwind)                                    │
│  List · Detail (status, notes, edit) · Create · Import · Export│
│  Live: SSE via fetch (sends Authorization + Last-Event-ID)    │
└──────────────┬──────────────────────────────▲────────────────┘
       HTTP + Bearer token, If-Match           │ text/event-stream
               ▼                               │
┌──────────────────────────────────────────────┴────────────────┐
│  FastAPI                                                       │
│  Per-request identity │ ownership │ state machine │ GMS        │
│  apply_write(): read → decide → conditional write → retry/409  │
│  ChangeFeed: ordered ids, replay buffer, presence, backpressure │
└──────────────┬────────────────────────────────────────────────┘
               │ update_case(..., expected_version)
     ┌─────────┼──────────────────────┬─────────────────────┐
     ▼                                ▼                     ▼
┌───────────────────────┐  ┌───────────────────────┐  ┌──────────────┐
│ DynamoDB               │  │ Firestore              │  │ In-memory     │
│ ConditionExpression    │  │ transaction            │  │ locked compare│
│ on version             │  │ on version             │  │ (demo, tests) │
└───────────────────────┘  └───────────────────────┘  └──────────────┘
```

Docket runs as **one API server** today. The design already allows more; see [Scaling to more than one server](#scaling-to-more-than-one-server).

---

## Scaling to more than one server

Docket is deliberately single-server today: one API process, one change feed. This section
is the plan for when one server is not enough, so the move is a known piece of work rather
than a redesign.

**What already works across servers.** Concurrency control. Every write is conditional on the
case version, and the *database* enforces it (a DynamoDB `ConditionExpression`, a Firestore
transaction). Two servers writing the same case cannot lose an update. No change is needed.

**What does not.** The live feed. Each server pushes only the changes made *through it*, so a
browser connected to server A never hears about a write that went through server B until it
reloads. Presence ("also here: bob") and `Last-Event-ID` replay are per server for the same
reason. Data stays correct; only the live view would be incomplete.

**The change.** Put a shared feed behind the existing `ChangeFeed.publish` / `subscribe`
interface in `events.py`, so the API code does not change:

| Option | How it works | Fits when |
|:--|:--|:--|
| **Redis** (recommended first step) | Every server publishes to and subscribes from one Redis channel. Move the replay buffer to a Redis Stream (`XADD` with a max length, `XRANGE` after the client's id), so `Last-Event-ID` replay works whichever server a client reconnects to. Keep presence in a Redis set with expiring members. | Any backend. One small managed Redis covers it. |
| **DynamoDB Streams** | A stream consumer (Lambda or a poller) turns each item change into an event and fans it out, through Redis or SNS, to every server. The stream's new image is already the `case` payload. | You are on DynamoDB and want the database to be the only source of events, including writes made outside the API. |
| **Firestore listeners** | Each server runs `on_snapshot` on the collection and publishes what it sees to its own clients. | You are on Firestore. No extra infrastructure; each listener costs reads. |

**Deployment details that bite.**
- Load balancers must not buffer `text/event-stream` responses. The API already sends
  `X-Accel-Buffering: no` for nginx; check the idle timeout is above the 15-second heartbeat.
- Sticky sessions are not needed once replay comes from the shared stream. Until then they
  limit the damage, because a reconnecting client usually lands on the server that has its gap.
- Event ids must come from the shared stream, not from a per-server counter, or replay ids from
  different servers collide. The current `<epoch>:<seq>` format would become the Redis Stream id.

**How to verify it.** Run two API processes behind a local proxy, point one browser at each,
and repeat the two-window demo: a change made through either server must appear in both
windows. `tests/unit/test_events.py` covers the replay and resync rules; the same tests should
run against the Redis-backed feed.

---

## Running

No cloud account needed to try it:

```bash
pip install -e ".[dev]"

# In-memory backend, two people
CF_DATABASE_BACKEND=memory CF_API_TOKENS='tok-alice:alice,tok-bob:bob' \
  uvicorn case_forge.api:app --port 8000

# Frontend (a second terminal)
python -m http.server 3000 --directory frontend
```

Open `http://localhost:3000` in two browser windows and sign in as `tok-alice` in one and `tok-bob` in the other. The token is kept per tab, so the two windows really are two people. Change a case in one and watch the other.

```bash
# DynamoDB
CF_DATABASE_BACKEND=dynamodb CF_ADMIN_USERS=alice \
  CF_API_TOKENS='tok-alice:alice,tok-bob:bob' uvicorn case_forge.api:app --port 8000

# Firestore
pip install "docket[firestore]"
CF_DATABASE_BACKEND=firestore CF_GCP_PROJECT=my-project \
  CF_API_TOKENS='tok-alice:alice' uvicorn case_forge.api:app --port 8000
```

Tables created before versioning existed keep working: items with no `version` attribute load and update as version 1.

---

## API

Every `/api` call needs `-H "Authorization: Bearer $TOKEN"` (omitted below for brevity) unless the server runs in single-user local mode. Case reads and writes return an `ETag` of the case version (`"v3"`). Send it back as `If-Match` to make a write conditional on the version you saw.

```bash
# Create case
curl -X POST localhost:8000/api/cases -H "Content-Type: application/json" \
  -d '{"product_id":"SKU-001","category":"Toys & Games","marketplace":["US","CA"],"description":"Missing CPSIA cert","gms":{"shipped_units":200,"unit_price":14.99}}'

# Search (combinable filters). The response's as_of_event is the feed position it reflects.
curl "localhost:8000/api/cases?status=NEW,IN_PROGRESS&is_high_value=true&category=Electronics"

# Update (owner or admin). 409 with the current case if it moved past v3.
curl -X PATCH localhost:8000/api/cases/{id} -H 'If-Match: "v3"' -d '{"category":"Baby Products"}'

# Transition status (re-validated against the latest state on every retry)
curl -X POST localhost:8000/api/cases/{id}/status -H 'If-Match: "v4"' -d '{"new_status":"IN_PROGRESS"}'

# Add note (append-only; never needs If-Match)
curl -X POST localhost:8000/api/cases/{id}/notes -d '{"text":"Contacted merchant, awaiting response"}'

# Delete (soft)
curl -X DELETE localhost:8000/api/cases/{id} -H 'If-Match: "v5"'

# Live events: case.created / case.updated / case.deleted / cases.imported,
# plus hello, presence and resync. Reconnect with Last-Event-ID to replay what you missed.
curl -N localhost:8000/api/events
curl -N localhost:8000/api/events -H "Last-Event-ID: 3f9a1c2e:41"

# Who is connected
curl localhost:8000/api/presence

# Import CSV / export / stats
curl -X POST localhost:8000/api/import -F "file=@cases.csv"
curl "localhost:8000/api/export?format=csv&status=RESOLVED" > resolved.csv
curl localhost:8000/api/stats
```

---

## Testing

```bash
pytest tests/ -v
pytest tests/ --cov=src
```

| Suite | What it pins down |
|:--|:--|
| `test_models.py`, `test_auth.py` | GMS arithmetic and the $1,000 boundary, the state machine, ownership rules |
| `test_identity.py` | Per-request identity through the API: missing or forged tokens, one user acting on another's case |
| `test_backends.py` | The storage contract, run against the in-memory backend **and** DynamoDB (via moto, which evaluates condition expressions): version increments, stale writes refused with the current case, no upserts, legacy items without a version, and the high-value filter and count |
| `test_events.py` | The feed: replay of exactly the missed events, resync on restart or eviction, backpressure, heartbeats, presence, cross-thread publishing |
| `test_live_api.py` | Multi-user behaviour through HTTP: ETags and `If-Match`, deterministic races injected between read and write, real threaded contention with a control run, and the event each write announces |

Firestore's transaction path is written to the same contract but needs the Firestore emulator to test; it is not covered here.

**Found in the browser, not by the tests.** The first version of the edit form sent `If-Match` with the version on screen at save time. Because live updates keep the screen current, a change made while you were typing advanced that version, and the save went through over it. The unit tests could not see this because they have no live screen. It surfaced while driving two sessions against one case. The fix was to pin the version an edit started from, and that is now how the UI behaves.

---

## Configuration

| Variable | Default | What it controls |
|:--|:--|:--|
| `CF_DATABASE_BACKEND` | `dynamodb` | `dynamodb`, `firestore`, or `memory` (no cloud; data lasts until the API stops) |
| `CF_AWS_REGION` | `us-east-1` | DynamoDB region |
| `CF_DYNAMODB_TABLE` | `Docket` | Table name |
| `CF_GCP_PROJECT` | (empty) | Firestore project ID |
| `CF_ADMIN_USERS` | `admin` | Comma-separated users with full access |
| `CF_API_TOKENS` | (empty) | Comma-separated `token:user` pairs; each request's user comes from its bearer token |
| `CF_LOCAL_USER` | (empty) | Single-user local mode: requests with no token act as this user. Loopback only |
| `CF_CORS_ORIGINS` | `http://localhost:3000` | Comma-separated origins allowed to call the API |

---

## Tradeoffs and Decisions I Made Along the Way

**DynamoDB/Firestore instead of PostgreSQL**

Cases are document-shaped (nested GMS components, arrays of notes, arrays of status history). The access patterns are: get by ID, query by status, query by assignee, filter by category. All of these map cleanly to DynamoDB GSIs or Firestore composite indexes. No joins, no transactions across tables, no complex aggregations that need SQL. The tradeoff: full-text search on description isn't native to either — I handle it client-side after filtering by structured fields. At <10K active cases, this is fast enough.

**Soft delete instead of hard delete**

Compliance operations have audit requirements. If someone deletes a case, the question "what was that case about and who deleted it?" needs an answer months later. Soft delete preserves the data with a `deleted_by` and `deleted_at` stamp. The case disappears from all queries (filtered by `is_deleted=false`) but the record exists for audit.

**GMS computation in the model, not in the frontend**

The frontend shows the GMS total live as you type (immediate feedback). But the backend recomputes it on save — never trusts the client-sent total. This prevents a user from editing the HTML to fake a high-value flag. The `is_high_value` boolean is a computed property of the GMS components, not a user-settable field.

**Product ID immutable after creation**

If you could change the product ID, the audit trail becomes incoherent — notes reference "SKU-001" but now it's "SKU-002". Instead: if the product ID was wrong, delete the case and create a new one. The delete is logged. The new case starts fresh. No ambiguity about what entity the history refers to.

**State machine enforced in the API, not the frontend**

The frontend disables invalid transition buttons. But the API independently validates: if you POST `{"new_status": "RESOLVED"}` on a case that's currently `NEW`, you get a 400 with the list of allowed transitions. Defense in depth — the UI is a convenience, the API is the authority.

**Append-only notes**

An associate writes "Contacted merchant at 2pm, they confirmed will send cert by Friday." If they could edit that note later, there's no proof of what was originally communicated. Append-only means the timeline is reliable — you can add clarifications but you can't rewrite history.

---

## Stack

FastAPI, boto3 (DynamoDB), google-cloud-firestore (optional), Pydantic, React 18 + Tailwind.
