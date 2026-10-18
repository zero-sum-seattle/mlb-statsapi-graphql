# MLB Stats API GraphQL

> **Work in Progress (WIP)** — An experimental, actively developed GraphQL interface to MLB data. Intended for learning and local development; **not production-ready**.

A lightweight Python service that exposes selected MLB Stats API data through [GraphQL](https://graphql.org/). Built with [Strawberry](https://strawberry.rocks/) and [FastAPI](https://fastapi.tiangolo.com/), using [python-mlb-statsapi](https://github.com/zero-sum-seattle/python-mlb-statsapi) as its asynchronous upstream client.

Instead of building separate REST calls for a team and its active roster, request only the fields you need in a single GraphQL operation. The service resolves those fields through the existing Python library; it is **not** a replacement for or fork of that library.

![Baseball stadium — MLB Stats API project](https://user-images.githubusercontent.com/2068393/203456246-dfdbdf0f-1e43-4329-aaa9-1c4008f9800d.jpg)

## Current capabilities

- **Team lookup** by MLB team ID: `id`, `name`, and `abbreviation`
- **Person lookup** by MLB person ID: `id` and `fullName` (the upstream endpoint may include non-players)
- **Regular-season team statistics** by season: wins, losses, runs scored, and runs allowed
- **Active team rosters** with nested player and position fields, loaded only when requested
- **Async upstream access** with a shared per-process `AsyncMlb` client and HTTP connection pool
- **Request-scoped DataLoaders** that deduplicate repeated team, person, active-roster, and team-season statistics lookups
- **Shared upstream concurrency cap** with request-local timing and structured server logs
- **Query safeguards** for depth, cost, aliases, and document size
- **Client-safe GraphQL errors** and an offline test suite

**Not included yet:** authentication, deployment configuration, rate limiting, production metrics, mutations, historical rosters, or a comprehensive MLB GraphQL schema.

## Quick start

**Requirements:** Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
git clone https://github.com/zero-sum-seattle/mlb-statsapi-graphql.git
cd mlb-statsapi-graphql

uv python install 3.12
uv sync --locked --python 3.12
uv run uvicorn mlb_statsapi_graphql.app:app --reload
```

Open **http://127.0.0.1:8000/graphql** to explore queries in GraphiQL. Live requests require outbound HTTPS access to `statsapi.mlb.com`.

### Example: team and active roster

```graphql
query {
  team(id: 136) {
    id
    name
    abbreviation
    roster {
      player {
        id
        fullName
      }
      position {
        abbreviation
      }
    }
  }
}
```

This requests the Seattle Mariners and their **current active roster**. Roster membership varies; it is not a historical or 40-man roster query.

### Example: team season statistics

```graphql
query {
  team(id: 136) {
    name
    stats(season: 2024) {
      season
      wins
      losses
      runsScored
      runsAllowed
    }
  }
}
```

`Team.stats(season: Int!): TeamSeasonStats` loads only when selected. The season
must be a four-digit integer between 1000 and 9999; this validates its structure,
not the availability of MLB data for that year. These are **regular-season** totals
(`gameType="R"`), excluding spring training and postseason games.

| Field | Type | Definition |
| --- | --- | --- |
| `season` | `Int!` | Requested season |
| `wins` | `Int` | Team wins from season-total pitching statistics |
| `losses` | `Int` | Team losses from season-total pitching statistics |
| `runsScored` | `Int` | Runs from season-total hitting statistics |
| `runsAllowed` | `Int` | Runs from season-total pitching statistics, including unearned runs |

The `stats` object is nullable. Missing, empty, or ambiguous statistics return
`null` when none of the four values is available; partial results keep available
values and return `null` for unavailable fields. Actual zeros stay zero. A group
must have a unique matching whole-team season split; multiple matching splits are
never summed or arbitrarily selected. Supplied season, team, and game-type
metadata must match; absent optional metadata is accepted. Upstream failures use
the existing error behavior, rather than being treated as missing data.

Within a request, aliases share one lookup per `(team ID, season)`. Different
seasons or teams require separate lookups, and caches do not survive the request.

### Example: individual person

```graphql
query {
  player(id: 605141) {
    id
    fullName
  }
}
```

You can also call the endpoint directly:

```bash
curl http://127.0.0.1:8000/graphql \
  -H 'Content-Type: application/json' \
  --data '{"query":"{ team(id: 136) { id name abbreviation } }"}'
```

A successful request returns a `data` object containing the selected fields. Missing teams or people resolve to `null`; GraphQL execution errors may still use HTTP 200, so clients should inspect the `errors` array.

## How it works

```text
GraphQL request
      |
      v
FastAPI / Strawberry
      |
      +-- Query validation and complexity limits
      |
      v
Request context + per-request DataLoaders
      |
      v
Shared concurrency semaphore (per application worker)
      |
      v
Shared AsyncMlb client (per application worker)
      |
      v
MLB Stats API
```

The application shares an asynchronous MLB client and connection pool for its lifetime. Each GraphQL request gets fresh DataLoaders, so repeated IDs within that request reuse a result **without retaining cached data across requests**. Roster results already contain player names, avoiding one person lookup per roster entry.

The DataLoaders **deduplicate**, but distinct IDs currently still require individual upstream calls. Concurrent calls are not the same as true HTTP batching.

### Resource limits

Defaults are intentionally simple and configurable:

| Setting | Default | Environment variable |
| --- | ---: | --- |
| Maximum query depth | 6 | `MLB_GRAPHQL_MAX_DEPTH` |
| Maximum estimated query cost | 100 | `MLB_GRAPHQL_MAX_COST` |
| Maximum aliases | 30 | `MLB_GRAPHQL_MAX_ALIASES` |
| Maximum document tokens | 1,000 | `MLB_GRAPHQL_MAX_TOKENS` |
| Maximum concurrent upstream lookups per worker | 10 | `MLB_GRAPHQL_MAX_CONCURRENCY` |

The cost estimator currently charges **5** per distinct root team or person lookup and **10** per distinct requested team roster, plus **5** per distinct `(team ID, season)` whose statistics are requested. A team with one stats season costs **10**; requesting a second season raises that to **15**. These are relative weights, **not** exact request counts or latency predictions. Duplicate selections of the same ID are counted once to reflect DataLoader caching.

Every custom resolver declares `metadata=priced(weight, request_key=..., result_key=...)`
next to its Strawberry field; `max_items` is another optional keyword. Explicit
weight `0` declares a resolver with no upstream work. The estimator resolves fields through graphql-core's schema and
recursively reads these declarations at any depth, honoring the selected
operation, fragments, aliases, directives, and argument/variable defaults.
A schema guard test fails if any custom resolver lacks a declaration.
Strawberry validates documents before the request cost check; standalone
`estimate_cost()` also validates its input before using the same traversal.
The estimator requires Strawberry >=0.335.0 and graphql-core >=3.3.0,<3.4
for its field-collection and variable-coercion APIs.

`request_key(parent_key, args)` returns a hashable loader cache identity: `("team", id)`,
`("player", id)`, `("roster", team_id)`, or `("team_stats", team_id, season)`.
Known identical requests are charged once. `result_key(parent_key, args)` separately
identifies the returned object for descendants; it defaults to unknown (`None`),
as does the request identity. For example, a team lookup uses request key
`("team", id)` to deduplicate upstream work and result key `id` so descendants
can form their own roster and stats request keys. Unknown identities are charged
separately, which can overestimate work across aliases.

List-producing fields may declare `max_items` only when their resolver contract
guarantees a finite maximum. Downstream costs multiply by the maximum number of
possible parent objects (`parent_count`; for nested list layers, bounds multiply).
List element identities are unknown; globally known identical request keys still deduplicate.
Without a bound, unknown downstream requests have infinite estimated cost and
are rejected by the normal `QUERY_COMPLEXITY_LIMIT` enforcement before execution.
Free descendants remain selectable. No roster bound is assumed: MLB roster
rules alone do not guarantee an API response size. Existing roster queries keep
their costs, but adding a costed descendant requires a defensible bound or a
provably shared request identity. Bounds are declarations, not runtime truncation;
maintainers must keep them consistent with resolver behavior. Abstract return
types conservatively sum possible branches, which can also overestimate.

Example override:

```bash
MLB_GRAPHQL_MAX_COST=150 uv run uvicorn mlb_statsapi_graphql.app:app --reload
```

All environment limits must be positive integers; invalid concurrency configuration
fails when the application is created. For example:

```bash
MLB_GRAPHQL_MAX_CONCURRENCY=5 uv run uvicorn mlb_statsapi_graphql.app:app --reload
```

The concurrency default of **10** is a modest starting point for this educational
service, allowing several independent lookups while reducing bursts. It is not a
benchmark-derived capacity or an MLB quota. Tune it for your workload.

One **semaphore** (a fixed pool of permits) is created during FastAPI lifespan.
Team, person, active-roster, and team-season statistics lookups all acquire from that same pool across
requests. A permit is held through the entire library method, including retries,
backoff, and parsing, and released on success, failure, or cancellation. Request
cleanup cancels and awaits unfinished DataLoader lookups before logging totals.
The shared `AsyncMlb` client retains its existing ownership, retry, timeout, and
cleanup behavior.

### Request observations

Each request has fresh statistics and a generated `request_id`. The
`mlb_statsapi_graphql.upstream` logger emits JSON message bodies at **INFO** level,
with the same fields attached to Python logging records for custom formatters.
Enable that logger and attach an INFO-capable handler in your application's
logging configuration; Uvicorn's default logging configuration alone does not
enable these application INFO records. For local development, a launcher can use:

```python
import logging
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(message)s")
uvicorn.run("mlb_statsapi_graphql.app:app")
```

No metrics service or global logging configuration is added by this package.
Records contain no query text, upstream response bodies, or exception messages.
Existing server-side error traceback logs remain available.

| Event / field | Definition |
| --- | --- |
| `mlb_upstream_lookup` | One completed or cancelled logical lookup, with `request_id`, `lookup_kind` (`team`, `person`, `roster`, `team_stats`), `admitted`, and `outcome` (`success`, `error`, `cancelled`) |
| `queue_wait_seconds` | Time from reaching admission to acquiring a permit; for cancellation while waiting, time spent waiting until cancellation |
| `lookup_seconds` | Time after admission until the library method returns, raises, or is cancelled; includes library retries, retry delays, JSON decoding, and model parsing |
| `mlb_graphql_request` | Request cleanup summary with the same `request_id` |
| `lookup_count` | Logical lookups admitted for this request, including admitted failures and cancellations; **not HTTP attempts** |
| `error_count` | Admitted library calls that raised an exception, including unexpected exceptions |
| `cancellation_count` | Lookups cancelled while waiting or running; waiting cancellations do not increment `lookup_count` |

The summary's timing fields sum the per-lookup intervals. They use a **monotonic
clock**, which cannot jump when wall-clock time is adjusted. Overlapping lookups
mean summed durations are not request latency. Cache hits add no lookup or timing;
missing entities still count as successful lookups. Requests rejected before
execution report zero lookups and zero timing. Request IDs correlate records,
without sharing counters or cached data across requests.

The cap is **per application worker**: four workers configured with 10 permits can
admit up to 40 logical lookups. It is not rate limiting. Waiting work is **not
bounded**, so a backlog can still consume memory and increase latency. Existing
HTTP timeouts apply to transport phases/attempts, not to queue wait plus retries
and execution as an overall request deadline. Cancellation cleanup applies when
the server cancels execution; a client disconnect does not necessarily cause that.
Batching, shared caches, dashboards, and transport-level attempt instrumentation
remain outside this milestone.

### Error behavior

| Error code | Meaning |
| --- | --- |
| `BAD_USER_INPUT` | An ID must be positive, or a season must be between 1000 and 9999 |
| `UPSTREAM_ERROR` | An MLB lookup or response failed |
| `QUERY_COMPLEXITY_LIMIT` | Estimated operation cost exceeds the configured limit |

Unexpected resolver errors are masked from clients and logged server-side. GraphQL syntax, validation, depth, alias, and token-limit errors may use standard GraphQL error messages rather than these codes.

## Development

Run the offline tests and build the package:

```bash
uv sync --locked
uv run --locked pytest
uv build
```

Tests mock the upstream `AsyncMlb` client; they do not require live MLB API access. GitHub Actions runs the test suite for pull requests and changes to `main`.

### Project layout

| Module | Responsibility |
| --- | --- |
| `app.py` | FastAPI application, GraphQL route, and client lifecycle |
| `schema.py` / `types.py` | GraphQL schema and public types |
| `resolvers.py` | Root query resolution |
| `context.py` / `loaders.py` | Request context and DataLoader caching |
| `complexity.py` / `settings.py` | Query cost estimation and configurable limits |
| `upstream.py` | Shared admission, request statistics, and structured lookup logs |
| `team_stats.py` | Select unambiguous upstream season totals |
| `errors.py` | Shared client-safe error helpers |

Source files live under `src/mlb_statsapi_graphql/`; tests live under `tests/`.

## Roadmap

- [x] **M0** — GraphQL service foundation and team lookups
- [x] **M1** — Person queries and nested active rosters
- [x] **M1.1** — Input validation and error handling cleanup
- [x] **M2.1** — Request-scoped DataLoader caching
- [x] **M2.2** — Query depth, cost, alias, and token limits
- [x] **M2.3** — Bounded upstream concurrency and observability
- [x] **M3.1** — Season-based team statistics
- [ ] **Future** — Assess broader queries, real batching where safe, operational hardening, and deployment

Roadmap items are goals, not delivery commitments. The project is evolving and breaking changes remain possible.

## Limitations and project status

This repository is a **WIP learning project**, not a production service. The GraphQL schema exposes a small subset of the capabilities of [python-mlb-statsapi](https://github.com/zero-sum-seattle/python-mlb-statsapi). Live responses depend on MLB availability and may change over time. Active roster data may be empty or unavailable; the upstream library may treat a roster 404 as an empty list.

Every custom resolver must explicitly declare its query cost; the schema guard test enforces this requirement, including explicit zero-cost declarations.

Feedback and contributions are welcome through GitHub issues and pull requests.

---

*Unofficial project; not affiliated with or endorsed by Major League Baseball.*
