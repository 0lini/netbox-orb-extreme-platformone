# Audits

Four reviews of this worker, all taken at commit `2a54acd`, plus the record of
what was done about them.

| Report | Scope | Rating at audit time |
|---|---|---|
| [`2026-07-28-software-design-analysis.md`](2026-07-28-software-design-analysis.md) | Architecture, layering, coupling, bottlenecks | Modularity **8/10** |
| [`2026-07-28-naming-and-readability.md`](2026-07-28-naming-and-readability.md) | Naming, consistency, readability, signatures | — |
| [`2026-07-28-solid-principles.md`](2026-07-28-solid-principles.md) | SRP / OCP / LSP / ISP / DIP | Overall **5/10** |
| [`2026-07-28-resilience.md`](2026-07-28-resilience.md) | Timeouts, retry, breaker, bulkhead, degradation | **6/10** |
| [`2026-07-28-error-handling.md`](2026-07-28-error-handling.md) | Taxonomy, propagation, observability | **6/10** |

The findings were then worked through in nine commits, `29b1ad6..920857a`.

## What changed

**Correctness**

- `python -m orb_extreme_platformone` produced no output at all; the console
  script ran a full API sweep and printed nothing (F-01).
- The Diode SDK test doubles accepted any keyword argument while the real
  protobuf classes reject unknown fields, so six transform suites could go
  green on a transform that fails in production (S-01).
- A 200 OK response with `total_pages` as a string, or a non-list records key,
  raised `TypeError` out of a pool thread and aborted the whole tick (R-02).
- Entity emission order was documented in comments but untested; reordering two
  lines kept the suite green while breaking serial and custom-field writes (F-08).

**Security**

- `truncate_error_body` bounded length but not content, so an upstream echoing
  the request body put the login password into exception messages and logs (E-02).

**Resilience**

- Nothing was retried except a single 401 re-login, so one transient 503 cost an
  entire ConfigState table for 24 hours (R-01).
- Interface-IP chunks were fetched sequentially while the thread pool idled (R-07).
- A new `ThreadPoolExecutor` per fan-out meant no connection reuse across
  phases (F-04).

**Structure**

- `transform/common.py` imported `bootstrap` for six string constants, dragging
  `requests` into the pure mapping layer (F-02); `transform` also reached into
  `extract.tables` (F-03). Both now go through leaf modules, held by
  `tests/test_architecture.py`.
- `PlatformOneClient` carried four concerns; transport split out to `http.py` (S-04).
- `PlatformOneApiError` was one flat type for nine failure modes, so no caller
  could tell permanent from transient (E-01).

**Deliberately not done**, with reasons recorded in the reports: the `run()`
phase registry (S-05 — inter-phase data flow makes it worse), the unused
`IngestSink` hook (S-10), response caching (R-07 — NetBox is already the
durable store), and renaming `function` (N-13 — it mirrors the upstream field).

## Re-running the analysis

The reports cite exact `file:line` locations against `2a54acd`. Line numbers
have moved since; the identifiers are still the reliable anchor.
