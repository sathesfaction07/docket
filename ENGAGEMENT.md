# Engagement readout: Docket

How this project would be scoped, delivered and handed over if it were a customer engagement.
The customer is a composite: a marketplace compliance operations team, not a named company.
Every number marked **measured** comes from this repository's tests and can be reproduced; anything
else is labelled as an estimate or an assumption.

## The customer and what they asked for

A compliance team of a dozen analysts works several hundred product cases a week. Each case has a
product, a category, marketplace exposure and a sales figure that decides its priority. The team's
system of record is a shared spreadsheet.

**The ask:** "Give us a case tracker with access control and an audit trail."

## What discovery found

Sitting with the team showed that the ask was the visible half of the problem:

| Observed | What it meant |
|:--|:--|
| Two analysts often open the same case within minutes | Concurrent edits are normal, not an edge case. A tracker that writes "last save wins" would lose work silently, exactly like the spreadsheet does today |
| Priority is set by hand from a sales lookup | The high-value flag has to be computed on the server from its components, or it can be gamed from the browser |
| Notes are evidence in later disputes | Notes must be append-only, and product IDs immutable, or the audit trail stops meaning anything |
| The team is split across AWS and Google Cloud accounts | Storage has to be swappable without touching the application |

**What was deliberately not built:** SSO integration (bearer tokens per user stand in for it and
the identity layer is one module), and multi-server live sync (designed and documented, not
needed at one team's scale).

## What was delivered

- A FastAPI service with per-request identity, ownership rules and an enforced state machine.
- A React UI that stays live across every open browser through a Server-Sent Events change feed,
  with replay after a dropped connection and a forced resync when replay is impossible.
- Optimistic concurrency on every write (`If-Match` / ETag), enforced by the database: a DynamoDB
  `ConditionExpression`, a Firestore transaction, or a locked compare in memory.
- Three storage backends behind one contract, switched by one environment variable.

## Proof that it works

| Claim | Evidence | Reproduce |
|:--|:--|:--|
| No concurrent note is lost | **Measured:** 8 threads add 24 notes to one case. With version checks every `200` has its note stored. A control run against a backend that ignores versions returns `200` for all 24 and still loses notes (17 of 24 in the run that set the bounds) | `pytest tests/unit/test_live_api.py` |
| The storage contract holds on every backend | The same contract tests run on memory, DynamoDB (moto evaluates condition expressions) and real Firestore | `pytest tests/unit/test_backends.py`; set `CF_TEST_FIRESTORE_PROJECT` for Firestore |
| A dropped browser never shows a stale screen | Replay of exactly the missed events, resync on restart or buffer eviction | `pytest tests/unit/test_events.py` |
| Nobody can act as someone else | Missing, forged and cross-user tokens are refused | `pytest tests/unit/test_identity.py` |

Two defects were found only by running against the real thing, which is the point of deploying
early:

1. **Firestore listing failed on every fresh database.** Queries used `order_by`, which needs a
   composite index per filter combination, and none were shipped. Listing now sends equality
   filters only and sorts in Python, so a new project works with nothing to deploy.
2. **An edit could overwrite a change made while the user was typing.** The edit form pinned the
   version on screen at save time, and the live feed kept advancing it. The fix pins the version
   the edit started from. Unit tests could not see this; two browser sessions on one case did.

## Rollout plan

| Phase | What changes | Gate to move on |
|:--|:--|:--|
| 1. Shadow | Analysts keep the spreadsheet; one pod enters every case in Docket as well | Docket's case list matches the spreadsheet for two weeks; no `409` the team can't explain |
| 2. Primary for one pod | That pod stops using the spreadsheet | No lost notes or edits reported; export covers every report the team built on the spreadsheet |
| 3. Whole team | Spreadsheet becomes read-only | Peak concurrent users stay within one server's feed; if not, do the Redis step below first |

## Risks and open items

- **Identity:** bearer tokens are per user but issued by configuration. Production needs the
  customer's identity provider in front (OIDC through IAP or an API gateway).
- **One server:** the live feed is per process. The README's scaling section describes the move to
  a shared Redis stream, which is the next piece of work if the team grows.
- **Listing reads all matches per page.** Correct and fast at thousands of cases; at much larger
  scale, deploy composite indexes and move ordering back into the query.

## Feedback for the platform team

The fresh-database index failure is a pattern worth fixing upstream: a query that works in the
emulator and in a long-lived project fails on the first call in a new one. A warning at query
construction, or an index file generated from the queries a client actually issues, would have
caught it before deployment.
