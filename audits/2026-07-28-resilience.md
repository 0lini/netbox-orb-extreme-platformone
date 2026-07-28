# Resilience Audit — `netbox-orb-extreme-platformone`

| | |
|---|---|
| **Date** | 2026-07-28 |
| **Commit** | `e5625f1` (branch `claude/software-architecture-analysis-kuervi`) |
| **Scope** | `src/orb_extreme_platformone/**`, `tests/**`, `agent.yaml` |
| **Method** | Static grep/AST sweep for timeout, retry, backoff, breaker and cache primitives; failure paths exercised empirically with `responses`-backed probes and a fake ConfigState source |
| **Baseline** | `uv run pytest -q` → **210 passed, 7 deselected**. Working tree clean after all probes. |
| **Companion** | Error taxonomy, logging and exception hygiene are covered in `2026-07-28-error-handling.md`; overlapping items are cross-referenced, not repeated |

---

## 1. Workload context (this changes what "resilient" means)

This is a **scheduled batch ETL worker**, not a request-serving service. `agent.yaml:31` runs it on `schedule: "0 2 * * *"` — once daily. It has no inbound traffic, no users waiting on a response, and no downstream consumers it can overload. The Orb Agent host owns scheduling and the Diode push.

That reframes the checklist honestly:

- **Retry with backoff is the highest-value gap.** A transient 503 currently costs an entire ConfigState table **for 24 hours**, until the next tick. There is no second chance within a run.
- **Circuit breakers matter far less than the checklist implies.** There is no cascading load to shed and no hot path to protect. A breaker's real value here is narrow: stopping 8 parallel threads from hammering an already-degraded API. I rate it accordingly (R-04, severity 3) rather than treating its absence as a headline failure.
- **Graceful degradation matters more than usual**, because a partial sync is genuinely useful — Diode ingestion is upsert-style, so a tick missing PoE data still improves NetBox. This is exactly what the codebase optimized for, and it did so very well.

### Rating: **6 / 10**

| Dimension | Score | Basis |
|---|---:|---|
| Timeout handling | **7** | Every outbound call has one; not configurable, and no overall tick deadline |
| Retry logic | **2** | Only HTTP 401 is retried, once. No backoff, no jitter; adapter `max_retries=Retry(total=0)` (verified) |
| Circuit breaker | **1** | None. No failure counting, no trip, no half-open — but see workload context above |
| Bulkhead / isolation | **5** | De-facto 8-thread cap; no explicit pool tuning, no Assets/ConfigState separation |
| Graceful degradation | **9** | Six independent degradation layers, 11 named failure tests. The codebase's strongest quality |

The overall 6 reflects a system that **degrades beautifully but never retries**. Fixing R-01 alone would move it to roughly 8.

---

## 2. What is already strong

Stating this first so a remediation pass does not damage it.

| Layer | Location | Behaviour on failure |
|---|---|---|
| ConfigState device listing | `correlate.py:78-86` | Falls back to Assets-only data (flat site, no ports) |
| Location fetch | `correlate.py:98-103` | Falls back to Assets `site_name`, no Location chain |
| Per-table fan-out | `retrieve.py:62-72` | Failed table recorded in `failed_tables`, siblings continue |
| Per-filter-chunk | `client.py:327-343` | Keeps rows from successful chunks; raises only if **every** chunk failed |
| Cluster member sides | `clusters.py:44-57` | One failed member filter still yields the other side's rows |
| VirtualChassis phase | `backend.py:293-304` | Whole phase degrades to no VC entities, tick continues |

Each degradation logs what the tick loses. Eleven named failure tests cover these paths (`test_run_survives_a_failed_port_table_and_keeps_the_rest`, `test_retrieve_keeps_prior_chunk_rows_when_later_chunk_fails`, `test_run_keeps_virtual_chassis_when_one_cluster_filter_fails`, and others in `tests/test_client.py`).

Also deliberate and correct: **the codebase refuses to invent default values** (`identity.py:100-119`, `port_constants.py:29-31`, `vlans.py:41-45`). Omitting a field is chosen over guessing it. That is a resilience trade-off in favour of data correctness, and it is the right call for a NetBox source of truth.

---

## 3. Findings

Severity is **1–10, where 10 = most important**.

---

### R-01 · Severity **8** · No retry or backoff: one transient blip costs a table for 24 hours

**Category:** Retry logic.
**Location:** `client.py:186-226`; adapter defaults verified as `Retry(total=0, ...)`

The only retry in the package is a single 401 re-login (`client.py:205-209`). Everything else — `requests.RequestException` (`:197`), 429, 500, 502, 503, 504 — raises `PlatformOneApiError` on first failure. The `requests` adapter adds nothing: default `max_retries=Retry(total=0)`, confirmed by inspection.

The degradation machinery then converts that single failure into a **24-hour data gap**, because the next attempt is the next scheduled tick:

```
one 503 on retrieve-asset-poe-power-ports-state
  → PlatformOneApiError                     (client.py:213)
  → failed_tables.append(table)             (retrieve.py:64)
  → all PoE data missing from this tick     (backend.py:386)
  → next attempt: tomorrow 02:00
```

There is no rate-limit handling at all — a 429 is treated as a generic `>= 400` error at `client.py:210`, and `Retry-After` is never read.

**Remediation** — mount a retry adapter on each session. This is the single highest-value change in this audit.

```python
# client.py — imports
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# client.py:46 — near the other tuning constants
# Retry transient upstream failures in-tick: a blip must not cost a whole
# table until the next scheduled run.
_RETRY_TOTAL = 3
_RETRY_BACKOFF_FACTOR = 0.5          # 0.5s, 1s, 2s (urllib3 adds jitter)
_RETRY_STATUSES = (429, 500, 502, 503, 504)

_RETRY = Retry(
    total=_RETRY_TOTAL,
    backoff_factor=_RETRY_BACKOFF_FACTOR,
    status_forcelist=_RETRY_STATUSES,
    allowed_methods=frozenset({"POST"}),
    respect_retry_after_header=True,
    raise_on_status=False,
)

# client.py:123-128 — mount it
    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.mount("https://", HTTPAdapter(max_retries=_RETRY))
            self._local.session = session
        return session
```

**Three things to get right:**

1. **`allowed_methods` must include `POST` explicitly.** urllib3 excludes non-idempotent methods by default, and *every* Platform ONE read in this worker is a POST (`/assets/v1/devices`, all `/configstate/v1/retrieve-*`). Without this line the adapter silently retries nothing.
2. **Idempotency is satisfied.** All retried calls are reads despite the POST verb; `/login` is a POST but idempotent in effect (it mints a fresh token). No retried request mutates upstream state.
3. **`respect_retry_after_header=True`** gives 429 handling for free.

**Worst-case latency impact:** with the existing 60 s timeout, a fully-unreachable endpoint now takes up to 4 attempts. See R-03 for the tick-deadline guard that bounds this.

---

### R-02 · Severity **8** · A malformed-but-200 response aborts the entire tick — proven

**Category:** Graceful degradation — gap in the failure model.
**Location:** `extract/retrieve.py:33-37`, `client.py:241-248`

`_one` catches **only** `PlatformOneApiError`:

```python
    def _one(table, filters) -> tuple[str, list[dict] | None, PlatformOneApiError | None]:
        try:
            return table, list(client.retrieve(table, filters)), None
        except PlatformOneApiError as exc:
            return table, None, exc
```

Any other exception propagates through `fut.result()` (`retrieve.py:43`) and out of `Backend.run` — discarding rows already fetched from healthy sibling tables. **I proved both the mechanism and a realistic trigger.**

**Mechanism** — a fake source raising `KeyError` on one of three jobs:

```
retrieve_parallel(Boom(), [("good1",{}), ("bad",{}), ("good2",{})])
  → ENTIRE FAN-OUT ABORTED -> KeyError: 'unexpected upstream shape'
  ... and the two healthy tables' rows are lost with it
```

**Realistic trigger** — a **200 OK** ConfigState body with an unexpected shape. `_paginate` (`client.py:241-248`) does no shape validation:

| Response body (all HTTP 200) | Outcome |
|---|---|
| `{"AssetPortState": [], "Pagination": {"total_pages": 1}}` | tolerated |
| `{"AssetPortState": [], "Pagination": []}` | tolerated |
| `{"AssetPortState": [], "Pagination": {"total_pages": "2"}}` | **`TypeError: '>=' not supported between instances of 'int' and 'str'`** → aborts tick |
| `{"AssetPortState": 5, "Pagination": {"total_pages": 1}}` | **`TypeError: 'int' object is not iterable`** → aborts tick |

An upstream that starts returning `total_pages` as a JSON string takes down the whole sync, and the carefully-built degradation architecture never engages.

**Remediation** — two layers.

**(a) Validate shape where it is read** (`client.py:241-248`):

```python
        page = 1
        while True:
            payload = self._post(path, {page_param: page, size_param: size}, body)
            records = payload.get(response_key) or []
            if not isinstance(records, list):
                msg = (
                    f"Platform ONE API returned non-list {response_key!r} for {path}: "
                    f"{type(records).__name__}"
                )
                raise PlatformOneApiError(msg)
            yield from records
            last_page = _coerce_page_count(total_pages(payload, page), default=page)
            if page >= last_page:
                break
            page += 1
```

with a small coercion helper beside `_chunked` (`client.py:51`):

```python
def _coerce_page_count(value, *, default: int) -> int:
    """Page counts arrive as ints; tolerate digit strings, reject anything else."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default
```

**(b) Make the thread worker fail-safe regardless** (`retrieve.py:33-37`) — defence in depth, so an unforeseen shape still degrades one table instead of the tick:

```python
    def _one(table: str, filters: dict) -> tuple[str, list[dict] | None, PlatformOneApiError | None]:
        try:
            return table, list(client.retrieve(table, filters)), None
        except PlatformOneApiError as exc:
            return table, None, exc
        except Exception as exc:  # noqa: BLE001 — one bad table must not abort the fan-out
            logger.exception("ConfigState retrieve-%s raised an unexpected error", table)
            return table, None, PlatformOneApiError(f"unexpected error for {table}: {exc}")
```

`logger.exception` preserves the traceback for diagnosis while the return value keeps the existing degradation contract. The `noqa` is required — `ruff select = ["ALL"]` enables `BLE001` (blind-except), and this is a deliberate, documented exception.

**Add regression tests** — neither path is currently covered:

```python
# tests/test_client.py
@responses.activate
def test_retrieve_rejects_non_list_records_as_api_error() -> None:
    responses.add(responses.POST, _cs_url("asset-port-state"),
                  json={"AssetPortState": 5, "Pagination": {"total_pages": 1}}, status=200)
    client = PlatformOneClient(api_token="t")
    with pytest.raises(PlatformOneApiError, match="non-list"):
        list(client.retrieve("asset-port-state", {"asset_device_id": ["x"]}))
```

---

### R-03 · Severity **6** · No tick deadline; worst-case runtime is unbounded in practice

**Category:** Long-running operation limits.
**Location:** `client.py:99,107`, `backend.py:188-194`, `client.py:241-248`

Per-request timeouts exist and are applied consistently — `timeout=self._timeout` at `client.py:153` and `:194`, `kwargs.setdefault("timeout", 30)` at `bootstrap.py:131`. That part is solid. What is missing is any bound on the **aggregate**.

Three compounding factors:

1. **The timeout is not configurable.** `_build_client` (`backend.py:189-194`) never passes `timeout`, so every deployment gets the hardcoded 60 s from `client.py:99`. `agent.yaml` has no knob for it.
2. **Pagination is unbounded** (`client.py:241-248`) — termination depends entirely on the server reporting a sane `total_pages`. Also filed as F-13 in the architecture audit.
3. **Filter chunks are sequential** (`client.py:327-343`). 20,000 interface IDs → 100 chunks × (60 s timeout + retries after R-01) with no ceiling. Also filed as F-07/B2.

A degraded-but-responding upstream can therefore keep a "daily" tick running for hours, overlapping the next scheduled run.

**Remediation** — make the timeout configurable and add a wall-clock deadline.

```python
# backend.py:188-194
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0

def _build_client(config) -> PlatformOneClient:
    return PlatformOneClient(
        base_url=_cfg_or_env(config, "PLATFORMONE_API_URL", default=DEFAULT_BASE_URL),
        api_token=_cfg_or_env(config, "PLATFORMONE_API_TOKEN"),
        username=_cfg_or_env(config, "PLATFORMONE_USERNAME"),
        password=_cfg_or_env(config, "PLATFORMONE_PASSWORD"),
        timeout=float(_cfg_or_env(config, "PLATFORMONE_TIMEOUT", default=DEFAULT_REQUEST_TIMEOUT_SECONDS)),
    )
```

```yaml
# agent.yaml — under the policy config block
          # PLATFORMONE_TIMEOUT: 60        # per-request seconds
          # PLATFORMONE_TICK_BUDGET: 3600  # abort the tick after this many seconds
```

For the deadline, a monotonic budget checked at the pagination and chunk loops is the smallest change that actually bounds runtime:

```python
# client.py — constructor gains an optional budget
        self._deadline: float | None = (
            time.monotonic() + tick_budget if tick_budget else None
        )

    def _check_deadline(self, path: str) -> None:
        if self._deadline is not None and time.monotonic() > self._deadline:
            msg = f"Platform ONE tick budget exhausted before completing {path}"
            raise PlatformOneApiError(msg)
```

Call it at the top of the `while True` in `_paginate` (`client.py:242`) and inside the chunk loop (`client.py:327`). Raising `PlatformOneApiError` means the existing degradation paths absorb it — the tick returns partial data rather than hanging.

**Caveat:** budget exhaustion mid-fan-out degrades *every remaining* table, not one. That is the intended trade (bounded runtime over completeness) but should be logged distinctly so operators can tell a budget abort from an upstream outage.

---

### R-04 · Severity **3** · No circuit breaker — low value for this workload, but one narrow case exists

**Category:** Circuit breaker pattern.
**Location:** absent (grep-verified: no `circuit`, `breaker`, `half.open` anywhere in `src/`)

There is no failure counting across calls, no trip threshold, no open/half-open state, and no recovery probe. `failed_tables` (`retrieve.py:64`) records failures for **logging only** (`backend.py:71-81`); nothing reads it to change behaviour.

**I am not recommending a full circuit breaker.** For a once-daily batch worker there is no cascading load to shed, no hot path to protect, and no downstream consumer to spare. A breaker that trips would mostly mean "skip work we were about to do anyway", and adding cross-tick state to a stateless worker is a real complexity cost for little gain.

**The one case with genuine value:** when Platform ONE is hard-down, the worker currently issues the full request volume anyway — 8 parallel threads × chunked retrieves × (after R-01) 4 attempts each with backoff. That is both slow and impolite to a service already in trouble.

**Proportionate remediation** — an in-tick fail-fast, not a stateful breaker. Roughly 15 lines, no persistence:

```python
# extract/retrieve.py — module level
# When this many consecutive jobs fail in one tick, treat the API as down and
# stop issuing new requests. Resets naturally: state lives only for the tick.
_CONSECUTIVE_FAILURE_LIMIT = 5


class _FailFast:
    """Counts consecutive failures within a tick so a hard outage stops early."""

    def __init__(self, limit: int = _CONSECUTIVE_FAILURE_LIMIT) -> None:
        self._limit = limit
        self._consecutive = 0

    @property
    def tripped(self) -> bool:
        return self._consecutive >= self._limit

    def record(self, *, failed: bool) -> None:
        self._consecutive = self._consecutive + 1 if failed else 0
```

Thread it through `retrieve_parallel` so a tripped state short-circuits remaining jobs with a synthetic error, preserving the existing `(table, None, exc)` contract. **Caveat:** `_FailFast` would be mutated from pool threads, so `record` needs a `threading.Lock` — or, simpler and preferable, evaluate it only on the main thread between fan-out phases in `backend.py`, where the phases are already sequential.

**Recovery testing** is currently not possible because there is nothing to recover. If the fail-fast lands, `tests/test_backend_run.py` should gain a test asserting that a tick with ≥5 consecutive table failures stops issuing requests — assertable via `len(responses.calls)`.

---

### R-05 · Severity **5** · Connection pools are untuned and thread-local sessions are never reused

**Category:** Bulkhead — connection pool limits.
**Location:** `client.py:123-128`, `extract/retrieve.py:39-43`

Verified default adapter settings: `pool_connections=10, pool_maxsize=10, max_retries=Retry(total=0)`. Nothing in the codebase overrides them.

Combined with the architecture audit's F-04 (new `ThreadPoolExecutor` per fan-out → new threads → new thread-local `requests.Session` each), the practical result is:

- **No connection reuse across fan-out phases.** Roughly 5 phases per tick, up to 8 threads each — every one paying a fresh TLS handshake.
- **No explicit ceiling.** Up to 8 concurrent threads × `pool_maxsize=10` = up to 80 sockets against one host, bounded only by how fast requests complete.
- **No isolation between the Assets API and ConfigState.** Both go through the same client and the same pools, so a ConfigState fan-out saturating connections can starve an Assets page fetch. In practice the phases are sequential, so this is latent rather than active.

There *is* a de-facto bulkhead: `workers = min(len(jobs), 8)` (`retrieve.py:39`) caps concurrency at 8, and phases run sequentially so only one pool is live at a time. That is accidental rather than designed — the number is an unnamed inline literal with no comment.

**Remediation** — size the pool to the known concurrency and name the cap. Combine with F-04's shared executor:

```python
# extract/retrieve.py:39
# Concurrency cap for ConfigState fan-out. Must match the client's
# pool_maxsize so threads never queue on a connection.
RETRIEVE_MAX_WORKERS = 8

    workers = min(len(jobs), RETRIEVE_MAX_WORKERS)
```

```python
# client.py:123-128 — size pools to that cap and mount R-01's retry together
from orb_extreme_platformone.extract.retrieve import RETRIEVE_MAX_WORKERS  # or duplicate the constant

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = HTTPAdapter(
                pool_connections=RETRIEVE_MAX_WORKERS,
                pool_maxsize=RETRIEVE_MAX_WORKERS,
                max_retries=_RETRY,
            )
            session.mount("https://", adapter)
            self._local.session = session
        return session
```

**Caveat — do not import `extract` from `client`.** That would invert the layering (`client` is below `extract`; see architecture audit §2). Define the constant in `client.py` and have `retrieve.py` import it, or duplicate it with a comment cross-referencing the other. The former is cleaner.

---

### R-06 · Severity **4** · Bootstrap failure aborts the whole tick — proven

**Category:** Graceful degradation — missing fallback.
**Location:** `backend.py:227`, `bootstrap.py:129-141`

`bootstrap.ensure_schema(netbox_url, netbox_token)` is called bare at `backend.py:227`, with no exception handling. `bootstrap.py:140` uses `resp.raise_for_status()`, so any NetBox failure raises `requests.HTTPError` — a type **no caller in the package catches**. Verified:

```
NetBox GET /api/extras/custom-fields/ → 500
  → HTTPError: 500 Server Error: Internal Server Error for url: https://.../custom-fields/
  → backend.py:227 does NOT catch this; the entire Platform ONE sync aborts
```

A transient NetBox blip therefore costs the entire Platform ONE discovery run — even though the sync itself does not need NetBox at all (Diode is the ingest path; NetBox REST is only used for one-time schema setup).

There is also no retry on those 9–27 sequential calls (architecture audit F-15), and a **TOCTOU window**: `_ensure_all` (`bootstrap.py:157-160`) does `_lookup` then `POST`, so two concurrent bootstraps both see `None`, both POST, and the loser gets a duplicate-name 400 that aborts its tick.

**Remediation** — decide deliberately whether bootstrap is fatal. Given `BOOTSTRAP: true` is an explicit opt-in for a first run (`agent.yaml:34`), failing loudly is defensible — but it should be an *explicit* choice with a clear message, not an unhandled `HTTPError` from a transitively-imported library:

```python
# backend.py:226-227
            logger.info("Policy %s: running bootstrap (custom fields + provenance tags)", policy_name)
            try:
                bootstrap.ensure_schema(netbox_url, netbox_token)
            except requests.RequestException as exc:
                msg = (
                    f"Bootstrap failed against NetBox ({exc}); custom fields and tags may be "
                    "incomplete. Fix NetBox connectivity and re-run with BOOTSTRAP: true, "
                    "or set BOOTSTRAP: false to sync without schema setup."
                )
                raise RuntimeError(msg) from exc
```

with `import requests` added to `backend.py`. This keeps the fail-closed behaviour (correct — syncing into a NetBox without custom fields would silently drop provenance) while making the failure legible and the intent explicit.

**Alternative, if partial sync is preferred:** log a warning and continue. Do not pick this without confirming that Diode ingestion tolerates missing custom-field definitions — see §5.

---

### R-07 · Severity **3** · No caching or cross-tick state; every tick refetches everything

**Category:** Graceful degradation — cached responses / fallback data sources.
**Location:** absent (grep-verified: no `lru_cache`, no persistence layer; `devices.py:229` `location_cache` is an in-tick dedup dict, not a resilience cache)

Every tick fetches the full estate from scratch. There is no last-known-good snapshot, so when ConfigState is down the worker degrades to Assets-only data (`correlate.py:78-86`) rather than to *yesterday's* ConfigState data — losing port, VLAN, LAG and location detail that was known 24 hours ago.

**I am not recommending a cache.** Three reasons, and they are decisive:

1. **Diode ingestion is upsert-style.** A tick that omits port data does not delete existing NetBox ports — the previous tick's data is already persisted downstream. NetBox *is* the cache.
2. **Re-pushing stale data is actively harmful** for a source-of-truth system: it would assert as "currently observed" something last seen a day ago, masking a real outage.
3. The container is ephemeral (fresh clone per run), so any cache would need external storage — a large new dependency for negative value.

**Recorded as a deliberate non-gap.** The one thing worth adding is visibility, so operators can distinguish "device has no ports" from "we could not fetch ports this tick":

```python
# backend.py:71-81 — extend _log_failed_tables to state the data impact
def _log_failed_tables(policy_name: str, failed_tables: list[str], *, domain: str = "") -> None:
    """Warn once when any ConfigState table degraded during a fan-out."""
    if not failed_tables:
        return
    label = f"ConfigState {domain}degradation" if domain else "ConfigState degradation"
    logger.warning(
        "Policy %s: %s this tick; failed tables: %s. "
        "NetBox retains the previous tick's values for these — they are not cleared.",
        policy_name,
        label,
        ", ".join(failed_tables),
    )
```

---

### R-08 · Severity **3** · No feature flags to disable a failing discovery domain

**Category:** Graceful degradation — feature flags.
**Location:** `agent.yaml:34-58`, `backend.py:250-259`

The only runtime switches are `BOOTSTRAP` (`agent.yaml:34`) and `classification` (`agent.yaml:52`), which filters *devices*, not *domains*. If the wireless tables start returning malformed data or the fabric CF sync misbehaves, an operator has no way to turn that phase off short of editing code and redeploying — the phases are hardcoded calls at `backend.py:250-259`.

**Remediation** — a policy-level domain toggle, additive and low-risk:

```python
# backend.py — near DEFAULT_CLASSIFICATION
DISCOVERY_DOMAINS = ("virtual_chassis", "ports", "wireless")


def _enabled_domains(config) -> set[str]:
    """Domains to run this tick; policy `disable_domains` opts phases out."""
    disabled = _cfg(config, "disable_domains", None) or ()
    if isinstance(disabled, str):
        disabled = [disabled]
    unknown = sorted(set(disabled) - set(DISCOVERY_DOMAINS))
    if unknown:
        logger.warning("Ignoring unknown disable_domains entries: %s", ", ".join(unknown))
    return set(DISCOVERY_DOMAINS) - set(disabled)
```

Guard each phase (`backend.py:250-259`):

```python
        domains = _enabled_domains(config)
        vc_entities, vc_memberships = (
            self._virtual_chassis_entities(client, scoped, policy_name)
            if "virtual_chassis" in domains else ([], {})
        )
```

```yaml
# agent.yaml
          # disable_domains: ["wireless"]   # skip a misbehaving phase without a redeploy
```

**Caveat:** disabling `ports` also disables the primary-IP and fabric-CF sources, since both are returned by `_port_entities` (`backend.py:329-387`). Document that coupling in `agent.yaml` rather than pretending the phases are independent.

---

## 4. Suggested sequencing

| Order | Findings | Rationale |
|---|---:|---|
| 1 | **R-01**, **R-02** | The two severity-8 items. R-02(b) is 4 lines and closes a proven tick-killer; R-01 is the single biggest availability win |
| 2 | **R-05** | Same `_session` method as R-01 — do them in one edit |
| 3 | **R-03** | Depends on R-01 landing first (retries change the latency budget the deadline must bound) |
| 4 | **R-06**, **R-08** | Independent; both small and operator-facing |
| 5 | **R-04** | Only after R-01 — retries change failure counting semantics |
| — | **R-07** | **No action.** Recorded as a deliberate non-gap with rationale |

**Cross-audit note:** R-01 and R-05 touch the same lines as architecture-audit F-04/F-05, and SOLID-audit S-04 proposes extracting `PlatformOneTransport` from `PlatformOneClient`. **Do S-04 first**, then land R-01/R-05 inside the new transport class — otherwise this work is written twice.

---

## 5. Not applicable, with evidence

| Checklist item | Status |
|---|---|
| **Database query timeouts** | **N/A** — no database. The package has no ORM, no DB driver, and no persistence layer; `pyproject.toml:35-41` declares only `requests`, `netboxlabs-diode-sdk`, `netboxlabs-orb-worker`. All state lives in NetBox via Diode, which the Orb host owns |
| **Async / promise rejection handling** | **N/A** — no async code. Zero `async def`, `await`, or `asyncio` in `src/` (AST-verified). Concurrency is `ThreadPoolExecutor` only; the equivalent gap is R-02 |
| **Diode push retries** | **Out of scope** — `Backend.run` returns entities and the Orb `PolicyRunner` owns the Diode client and its retry behaviour (`backend.py:4-6`). Nothing in this package can influence it |

---

## 6. Unable to verify

| Claim | Why | What would prove it |
|---|---|---|
| Whether Platform ONE returns `429` and honours `Retry-After` | No 429 appears in any fixture or test; `client.py:210` treats it as a generic `>= 400` | A captured 429 response, or the Platform ONE rate-limit documentation |
| Realistic tick wall-clock, and whether R-03's budget would ever trip | No profiling harness; estate size unknown | Timing instrumentation on a production tick, or a `responses`-backed benchmark at N=20,000 interface IDs |
| Whether Diode ingestion tolerates custom-field definitions that do not yet exist | Determines whether R-06's alternative (warn-and-continue) is safe | A dry run against a NetBox without the `platformone_*` fields, inspecting whether Diode drops the entity or just the field |
| Whether two Orb agents ever bootstrap the same NetBox concurrently | Determines if the `bootstrap.py:157-160` TOCTOU window matters in practice | Deployment topology — is more than one agent ever pointed at one NetBox with `BOOTSTRAP: true`? |
| Whether the 8-worker cap is right for the upstream's rate limits | `retrieve.py:39` has no comment explaining the choice | Platform ONE's documented concurrency/rate limits, or load testing against a real tenant |
