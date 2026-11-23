# ADR 001: Abstract storage behind an interface instead of committing to one cloud

## Status

Accepted

## Context

Docket needs to persist cases, query them by status and assignee, and produce dashboard aggregates. The obvious move is to pick a managed NoSQL store and write directly against its SDK.

Two candidates were on the table, and the choice was not clean:

- **DynamoDB** — the natural fit if the surrounding infrastructure is AWS. Cheap and fast for get-by-ID. Filtered queries without a matching index become table scans, and scan cost is charged per request, so it gets expensive exactly when the dataset grows.
- **Firestore** — composite indexes support arbitrary field combinations, so `status + assignee + category` in one query is straightforward. Pricing is per document read rather than per request, which behaves differently under the same access pattern.

Writing directly against either SDK means the query layer, the pagination model and the aggregation code all take on that vendor's shape. Switching later is not a config change, it is a rewrite of every call site.

There was also a practical problem during development. Both options require cloud credentials to do anything at all. That makes the tool impossible to run on a laptop with no AWS or GCP access, and it makes tests depend on network calls or on a local emulator.

## Decision

Define a `DatabaseBackend` ABC in `database/base.py` with seven abstract methods — `create_case`, `get_case`, `list_cases`, `update_case`, `soft_delete`, `batch_import`, `get_stats` — and implement it twice, in `database/dynamodb.py` and `database/firestore.py`.

The interface deliberately traffics in domain types, not vendor types. `list_cases()` takes a `CaseFilters` object and returns a `CasePage` with `items`, `cursor` and `total`. Neither the API layer nor the models know what a DynamoDB `LastEvaluatedKey` or a Firestore `DocumentSnapshot` is. Cursor pagination is exposed as an opaque string because both stores can express it, even though they express it differently underneath.

Backend selection is one setting: `CF_DATABASE_BACKEND=dynamodb|firestore`.

## Consequences

**Good**

- Switching stores is one environment variable and a dependency install, not a rewrite
- The seven-method surface is small enough to be a genuine constraint. If a feature cannot be expressed in it, that is a signal the feature is leaning on vendor-specific behaviour
- A third backend is a tractable amount of work. A local SQLite implementation would make the project runnable and testable with no cloud account at all, which is the main thing currently blocking that
- Aggregation is forced to be explicit. Because `get_stats()` has to work on both stores, it cannot quietly depend on one vendor's aggregation feature

**Bad**

- The interface can only offer what both stores can do. Anything genuinely specific to one of them — DynamoDB transactions, Firestore real-time listeners — is unreachable without widening the ABC or punching through it
- Two implementations means two places for a bug and two things to keep in step
- `DashboardStats` takes `**kwargs`, which is loose for a data class and gives up type checking on the aggregate fields. It was expedient while the stat set was still moving

**Neutral**

- The two backends are not performance-equivalent and the abstraction hides that. The same `list_cases()` call can be a cheap indexed query on one and a scan on the other. The interface makes them look alike; the bill does not
- No integration tests exercise either implementation against a real store. The abstraction is only verified at the unit level, so drift between the two is possible
