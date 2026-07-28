# Error Handling Audit — `netbox-orb-extreme-platformone`

| | |
|---|---|
| **Date** | 2026-07-28 |
| **Commit** | `e5625f1` (branch `claude/software-architecture-analysis-kuervi`) |
| **Scope** | `src/orb_extreme_platformone/**`, `tests/**` |
| **Method** | AST enumeration of every `raise` (21) and `except` (14) site; log-call classification by level and context; credential-leak and error-shape behaviour proven with `responses`-backed probes |
| **Baseline** | `uv run pytest -q` → **210 passed, 7 deselected**. Working tree clean after all probes. |
| **Companion** | Retry, circuit breaker, timeout and degradation mechanics are in `2026-07-28-resilience.md`. This audit covers **taxonomy, propagation and observability**; overlapping items are cross-referenced, not repeated |

---

## 1. Executive summary

**Exception hygiene is genuinely good.** Across 14 `except` handlers there is **not one bare `except:` and not one `except Exception:`** — every handler names a specific type. `raise ... from exc` is used correctly at both wrapping sites (`client.py:199`, `client.py:218`). Error bodies are truncated before they reach logs (`truncate_error_body`, `client.py:64-71`). Eleven named tests cover failure paths.

**The gap is categorization, not handling.** `PlatformOneApiError` is a bare `RuntimeError` subclass with no attributes:

```python
class PlatformOneApiError(RuntimeError):
    """Raised on a non-2xx response from a Platform ONE API."""
```

One flat type carries **401, 403, 404, 429, 5xx, unexpected redirect, invalid JSON, non-object JSON, and transport failure** — the status code exists only as text inside a formatted message string. No caller can distinguish "credentials are wrong, stop now" from "gateway hiccup, retry" from "this table does not exist on this tenant". That single design choice blocks every smart recovery behaviour the resilience audit recommends.

A second structural issue: **two incompatible error taxonomies coexist.** `client.py` raises `PlatformOneApiError`; `bootstrap.py` raises `requests.HTTPError` via `raise_for_status()`. Nothing catches the latter.

### Rating: **6 / 10**

| Dimension | Score | Basis |
|---|---:|---|
| Consistency | **6** | Uniform and disciplined inside `client.py`/`extract/`; `bootstrap.py` uses a different, uncaught model |
| Categorization | **3** | One flat class for nine distinct failure modes; no status code carried |
| Concurrency error handling | **4** | No async in the package; the thread-pool equivalent has a proven hole (see R-02) |
| Recovery | **9** | Six degradation layers, well tested — the codebase's strongest quality |
| Error information | **5** | Truncation is length-bounded but content-blind; **zero** `logger.error`/`logger.exception`; 15 of 28 log calls lack policy context |

---

## 2. Findings

Severity is **1–10, where 10 = most important**.

---

### E-01 · Severity **8** · `PlatformOneApiError` carries no status code, so no caller can react to *why* a call failed

**Category:** Error categorization.
**Location:** `client.py:60-61`; raised at `:139, :158, :162, :167, :199, :202, :213, :218, :223`

Nine raise sites, one type, zero structured data. The status code is formatted into the message and then unavailable:

```python
            if resp.status_code >= 400:
                detail = truncate_error_body(resp.text)
                msg = f"Platform ONE API error {resp.status_code} for {path}: {detail}"
                raise PlatformOneApiError(msg)          # client.py:210-213
```

Every consumer therefore treats all failures identically — `retrieve.py:63`, `correlate.py:80`, `correlate.py:98`, `backend.py:295`, `client.py:294` all catch the same flat type and degrade the same way. The consequences are concrete:

| Real failure | Correct response | Actual response |
|---|---|---|
| **401/403** — wrong or revoked credentials | Abort the tick; every subsequent call will fail too | Degrade one table, then repeat for all 15 remaining tables |
| **404** — table absent on this tenant | Skip permanently, stop requesting it | Retried in full every tick, forever |
| **429** — rate limited | Back off, honour `Retry-After` | Treated as a generic `>= 400` at `client.py:210` |
| **502/503/504** — transient | Retry with backoff | Table lost for 24 hours (see R-01) |

This is also the **blocker for R-01**: a retry policy cannot be selective about which failures are worth retrying when the exception does not say what happened.

**Remediation** — carry the status code and derive intent from it. Fully backward compatible: `PlatformOneApiError` stays a `RuntimeError`, and every existing `except PlatformOneApiError` keeps working.

```python
# client.py:60-61 — replace the class
class PlatformOneApiError(RuntimeError):
    """Raised on a failed Platform ONE API call.

    ``status_code`` is the HTTP status when the failure came from a response,
    or ``None`` for transport failures and malformed bodies. Callers use the
    ``is_*`` properties rather than parsing the message.
    """

    def __init__(self, message: str, *, status_code: int | None = None, path: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.path = path

    @property
    def is_auth_failure(self) -> bool:
        """Credentials are wrong or revoked — retrying will not help."""
        return self.status_code in (401, 403)

    @property
    def is_not_found(self) -> bool:
        """Endpoint or table absent for this tenant — permanent for the tick."""
        return self.status_code == 404

    @property
    def is_transient(self) -> bool:
        """Worth retrying: rate limit, gateway error, or a transport failure."""
        return self.status_code is None or self.status_code in (429, 500, 502, 503, 504)
```

Then pass the code at the raise sites — `client.py:210-213`:

```python
            if resp.status_code >= 400:
                detail = truncate_error_body(resp.text)
                msg = f"Platform ONE API error {resp.status_code} for {path}: {detail}"
                raise PlatformOneApiError(msg, status_code=resp.status_code, path=path)
```

and similarly at `:158` (`status_code=resp.status_code`), `:162`, `:202`. Transport and JSON failures (`:199`, `:218`, `:223`) leave `status_code=None`, which `is_transient` already treats as retryable.

**The payoff** — fan-out can now fail fast on a fatal error instead of grinding through 15 doomed tables (`retrieve.py:62-72`):

```python
    for context, (table, rows, exc) in zip(contexts, retrieve_parallel(client, jobs), strict=True):
        if exc is not None:
            if exc.is_auth_failure:
                logger.error(
                    "Policy %s: Platform ONE rejected our credentials (%s); aborting the tick",
                    policy_name, exc,
                )
                raise exc
            failed_tables.append(table)
            logger.warning(...)
            continue
```

**Caveat:** `raise exc` inside `retrieve_ok` changes `extract_device_table_buckets` from never-raising to raising on 401/403. `backend.py:349-351` and `:401` do not currently guard those calls — either add handling there or let it propagate as a tick abort, which for bad credentials is the correct outcome. Decide deliberately; do not leave it implicit.

---

### E-02 · Severity **7** · Credentials can leak into exception messages and logs — proven

**Category:** Error information — production exposure.
**Location:** `client.py:64-71` (`truncate_error_body`), consumed at `:160` and `:211`

`truncate_error_body`'s docstring states the intent plainly:

> *"Keep API error text short so logs/exceptions do not retain full upstream bodies (which can include sensitive diagnostics)."* — `client.py:41-43`

But it only bounds **length**, not **content**. If the upstream echoes the request body in an error response — which some API gateways do — the login password lands in the exception message. Verified:

```
upstream 401 body echoes the request → exception text:
  Platform ONE login failed (401): {"error":"bad creds for {"username":"admin","password":"hunter2"}"}
  contains password? True
```

That string then flows into logs at `retrieve.py:65-71` and `correlate.py:81-86`, which format the exception with `%s`.

The blast radius is bounded — it needs an upstream that echoes credentials, which is unusual — but `client.py:143` sends `{"username": ..., "password": ...}` as the login body, so the material is present in exactly the request whose error text gets logged.

**Remediation** — redact known secret material before truncating:

```python
# client.py:64-71 — replace truncate_error_body
_REDACTED = "[REDACTED]"
# Redact secret-bearing JSON fields an upstream might echo back in an error body.
_SECRET_FIELD_RE = re.compile(
    r'("(?:password|client_secret|access_token|refresh_token|api_token|authorization)"\s*:\s*)"[^"]*"',
    re.IGNORECASE,
)


def truncate_error_body(text: str, *, limit: int = _ERROR_BODY_LIMIT) -> str:
    """Collapse whitespace, redact echoed secrets, and truncate for safe logging."""
    cleaned = " ".join((text or "").split())
    cleaned = _SECRET_FIELD_RE.sub(rf'\1"{_REDACTED}"', cleaned)
    if len(cleaned) <= limit:
        return cleaned
    if limit <= 3:
        return cleaned[:limit]
    return cleaned[: limit - 3] + "..."
```

with `import re` added at `client.py:23`.

**Defence in depth** — also stop sending the password into a message path at all. `client.py:159-162` already avoids including the request body; the risk is purely the echoed response. For belt and braces, exclude the login path's body from error text entirely:

```python
# client.py:159-162
        if resp.status_code != 200:
            detail = truncate_error_body(resp.text)
            msg = f"Platform ONE login failed ({resp.status_code}): {detail}"
            raise PlatformOneApiError(msg, status_code=resp.status_code, path="/login")
```

**Add a regression test** — this behaviour has no coverage today:

```python
# tests/test_client.py
def test_truncate_error_body_redacts_echoed_credentials() -> None:
    body = '{"error": "bad creds for {\\"username\\": \\"admin\\", \\"password\\": \\"hunter2\\"}"}'
    out = truncate_error_body(body, limit=500)
    assert "hunter2" not in out
    assert "[REDACTED]" in out
```

---

### E-03 · Severity **7** · Two error taxonomies; `bootstrap`'s is caught by nobody

**Category:** Error handling consistency.
**Location:** `bootstrap.py:129-141` vs `client.py:178-228`

The package has two HTTP clients with two incompatible failure models:

| Module | Raises | Caught by |
|---|---|---|
| `client.py` | `PlatformOneApiError` (custom) | 5 handlers across `extract/` and `backend.py` |
| `bootstrap.py` | `requests.HTTPError` — via `raise_for_status()` (`:140`) and an explicit raise (`:136`) | **nothing** |

`bootstrap.ensure_schema` is called bare at `backend.py:227`. Verified consequence:

```
NetBox GET /api/extras/custom-fields/ → 500
  → HTTPError: 500 Server Error: Internal Server Error for url: .../custom-fields/
  → backend.py:227 does NOT catch this; the entire Platform ONE sync aborts
```

A NetBox blip kills a Platform ONE discovery run that does not otherwise need NetBox. The failure also surfaces as a raw `requests` exception — a library type leaking through the package's public surface, where every other error path presents a domain type.

**Remediation** — give bootstrap a domain error class that mirrors `PlatformOneApiError`, so the package presents one taxonomy:

```python
# bootstrap.py — near the top
class NetBoxApiError(RuntimeError):
    """Raised on a failed NetBox REST call during schema bootstrap."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
```

```python
# bootstrap.py:129-141 — map requests failures onto it
def _request(method: str, url: str, token: str, **kwargs) -> requests.Response:
    """NetBox REST call that never follows redirects (token must not leave origin)."""
    kwargs.setdefault("timeout", 30)
    kwargs.setdefault("allow_redirects", False)
    try:
        resp = requests.request(method, url, headers=_headers(token), **kwargs)
    except requests.RequestException as exc:
        msg = f"NetBox request failed for {url}: {exc}"
        raise NetBoxApiError(msg) from exc
    if 300 <= resp.status_code < 400:
        msg = f"NetBox unexpected redirect {resp.status_code} for {url}"
        raise NetBoxApiError(msg, status_code=resp.status_code)
    if resp.status_code >= 400:
        detail = truncate_error_body(resp.text)
        msg = f"NetBox API error {resp.status_code} for {url}: {detail}"
        raise NetBoxApiError(msg, status_code=resp.status_code)
    return resp
```

reusing `truncate_error_body` from `client.py` (import it — `bootstrap` already sits below `client` in no particular order, and both depend only on `urls`; if that import direction is unwanted, move `truncate_error_body` to a shared leaf module alongside the S-04 transport split).

Then handle it explicitly at the call site — see **R-06** in the resilience audit for the `backend.py:227` wrapper and the fail-closed rationale. **Do not duplicate that fix here**; E-03 is the taxonomy half, R-06 is the propagation half, and they should land together.

**Note:** `tests/test_bootstrap.py` (129 lines) asserts on `requests.HTTPError` today — those assertions need updating to `NetBoxApiError` in the same change.

---

### E-04 · Severity **6** · Nothing is ever logged at ERROR; total failures are silent

**Category:** Error logging completeness.
**Location:** all 28 log calls in `src/`

Measured level distribution:

```
21  logger.warning
 7  logger.info
 0  logger.error
 0  logger.exception
 0  logger.critical
```

**Zero ERROR-level records exist in the entire package.** When a tick dies — bad credentials, malformed upstream response (R-02), NetBox bootstrap failure (E-03) — the exception propagates to the Orb host and this package logs nothing at all about it. An operator with an alert on `level >= ERROR` sees silence on total failure, while routine single-table degradation floods the same pipeline at WARNING.

The severity signal is inverted: partial degradation (recoverable, expected) and total failure (unrecoverable, page-worthy) are indistinguishable downstream.

**Remediation** — reserve WARNING for degradation that the tick survives, and add ERROR for failures that end it.

```python
# backend.py:212 — wrap the tick body so a fatal error is recorded before it propagates
    def run(self, policy_name: str, policy: Policy, **_kwargs) -> Iterable[Entity]:
        try:
            return self._run(policy_name, policy)
        except Exception:
            logger.exception("Policy %s: tick failed and produced no entities", policy_name)
            raise
```

with the existing body moved verbatim into a private `_run(self, policy_name, policy)`. `logger.exception` emits at ERROR **with the traceback**, and the bare `raise` preserves the original exception and its context for the host. The `except Exception` here is deliberate and narrow in effect — it only logs and re-raises, changing no control flow.

`ruff select = ["ALL"]` will flag the broad catch; annotate it:

```python
        except Exception:  # noqa: BLE001 — log-and-reraise so total failure is not silent
```

**Also worth adding** — a tick-summary INFO so success is observable, not just failure:

```python
# backend.py — at the end of _run, before returning
        logger.info(
            "Policy %s: tick complete; %d entities from %d in-scope device(s)%s",
            policy_name,
            len(entities),
            len(scoped),
            f" ({len(failed)} table(s) degraded)" if failed else "",
        )
```

**Caveat:** this requires threading the failed-table list up from `_port_entities`/`_radio_entities`, which currently log it locally at `backend.py:386` and `:408`. If that plumbing is unwanted, keep the summary to entity and device counts.

---

### E-05 · Severity **5** · Thread-pool errors: only one exception type is contained

**Category:** Concurrency error handling.
**Location:** `extract/retrieve.py:33-37`

**There is no async code in this package** — zero `async def`, `await`, or `asyncio` (AST-verified), so "unhandled promise rejections", async middleware wrappers and event-emitter error handling do not apply. The equivalent surface is the `ThreadPoolExecutor` at `retrieve.py:39-43`, and it has a proven hole: `_one` catches only `PlatformOneApiError`, so any other exception propagates through `fut.result()` and aborts the entire fan-out, discarding rows from healthy sibling tables.

**Fully analysed with proof and remediation as R-02 in the resilience audit** — including the two realistic triggers (a 200 OK body with `total_pages` as a string, or a non-list records key) and the four-line fail-safe for `_one`. Not repeated here.

**The error-handling-specific addition:** when the fail-safe lands, the synthetic error it produces should be distinguishable from a genuine API error, so an unexpected-shape bug is not silently filed alongside ordinary upstream flakiness. Building on E-01's class:

```python
# extract/retrieve.py:33-37 — with E-01's status_code parameter available
        except Exception as exc:  # noqa: BLE001 — one bad table must not abort the fan-out
            logger.exception("ConfigState retrieve-%s raised an unexpected error", table)
            return table, None, PlatformOneApiError(
                f"unexpected error for {table}: {exc}", status_code=None,
            )
```

`logger.exception` gives the traceback at ERROR (addressing E-04 for this path), while `status_code=None` marks it transient so E-01's classifier does not mistake it for an auth failure.

---

### E-06 · Severity **4** · Chunked-retrieve failure discards all but the first error

**Category:** Error information — diagnostic loss.
**Location:** `client.py:325-343`

```python
                errors: list[PlatformOneApiError] = []
                ...
                if errors and completed == 0:
                    raise errors[0]
```

When every chunk fails, `errors` may hold 50 exceptions and only `errors[0]` survives. Each failure *is* logged individually at `:331-337`, so the information is not entirely lost — but the exception that propagates, and therefore the one that reaches `failed_tables` and any future ERROR record, represents one chunk out of many. If chunk 1 failed with a 401 and chunks 2–50 with 503s, the operator sees an auth error and misdiagnoses a gateway outage (or the reverse).

**Remediation** — summarize rather than sample. With E-01's `status_code` available, the summary can be genuinely useful:

```python
# client.py:341-343
                if errors and completed == 0:
                    codes = sorted({exc.status_code for exc in errors if exc.status_code is not None})
                    msg = (
                        f"ConfigState retrieve-{table}: all {len(chunks)} filter chunks failed "
                        f"(status codes: {codes or 'transport/parse errors'}); "
                        f"first error: {errors[0]}"
                    )
                    raise PlatformOneApiError(
                        msg,
                        status_code=errors[0].status_code,
                        path=f"/configstate/v1/retrieve-{table}",
                    ) from errors[0]
```

`from errors[0]` preserves the original traceback chain. `tests/test_client.py:209` (`test_retrieve_raises_when_every_filter_chunk_fails`) asserts the raise happens; check whether it also matches on message text before changing the string.

---

### E-07 · Severity **4** · Logger names are hardcoded and collapse three modules into one

**Category:** Error logging completeness — filterability.
**Location:** `client.py:48`, `retrieve.py:14`, `clusters.py:12`, `correlate.py:13`, `common.py:19`, `backend.py:44`

Six logger instantiations, two conventions:

```python
logger = logging.getLogger("orb_extreme_platformone.client")     # client.py:48    hardcoded
logger = logging.getLogger("orb_extreme_platformone.extract")    # retrieve.py:14  hardcoded
logger = logging.getLogger("orb_extreme_platformone.extract")    # clusters.py:12  hardcoded — same name
logger = logging.getLogger("orb_extreme_platformone.extract")    # correlate.py:13 hardcoded — same name
logger = logging.getLogger("orb_extreme_platformone.transform")  # common.py:19    hardcoded
logger = logging.getLogger(__name__)                             # backend.py:44   idiomatic
```

Two consequences:

1. **Three `extract/` modules share one logger name**, so an operator cannot raise the level on `correlate` alone while silencing the chatty per-chunk warnings from `retrieve`. Likewise every `transform/` module logs as `orb_extreme_platformone.transform` — `vlans.py:56`, `lags.py:59`, `port_join.py:36,68`, `virtual_chassis.py:85,92,102`, `wireless_auth.py:86` and `devices.py:135` are indistinguishable by logger name.
2. **The hardcoded strings can drift from the module path** on any rename, silently breaking downstream logging config.

**Remediation** — one-line change per module, no behaviour change for anyone filtering on the package prefix:

```python
logger = logging.getLogger(__name__)
```

This yields `orb_extreme_platformone.extract.correlate`, `orb_extreme_platformone.transform.vlans`, and so on. Existing config targeting `orb_extreme_platformone.extract` continues to match by prefix, since `logging` uses dotted-hierarchy propagation.

**Caveat:** `transform/common.py:19` exports `logger` for import by nine sibling modules (`devices.py:31`, `vlans.py:7`, `lags.py:7`, …). Changing only `common.py` would name all of them `orb_extreme_platformone.transform.common` — worse, not better. Either give each module its own `logger = logging.getLogger(__name__)` and drop the shared import, or leave `transform/` as-is. The `extract/` modules have no such coupling and can be fixed independently.

---

### E-08 · Severity **3** · Fifteen of 28 log calls carry no policy context

**Category:** Error logging completeness — attribution.
**Location:** 15 sites, enumerated below

The codebase threads `policy_name` into log messages as `"Policy %s: ..."` — 13 calls do this correctly. Fifteen do not:

| Module | Lines |
|---|---|
| `backend.py` | `:109`, `:112`, `:130` |
| `client.py` | `:331` |
| `clusters.py` | `:51` |
| `correlate.py` | `:37` |
| `transform/` | `devices.py:135`, `lags.py:59`, `port_join.py:36`, `port_join.py:68`, `virtual_chassis.py:85`, `:92`, `:102`, `vlans.py:56`, `wireless_auth.py:86` |

An Orb agent can run multiple policies (`agent.yaml:24` nests policies under `policies.worker`), so a warning like

```
Multiple port_configs rows share join key 'if-uuid-1' (2 rows); using the first
```

cannot be attributed to a policy, a device, or a site.

**Remediation** — do **not** thread `policy_name` through 15 pure transform functions; that would push orchestration context into the mapping layer and undo its best property. Use a `logging` filter instead, set once at the tick boundary:

```python
# backend.py — near the logger
import contextvars

_policy_name: contextvars.ContextVar[str] = contextvars.ContextVar("policy_name", default="-")


class _PolicyContextFilter(logging.Filter):
    """Attach the current policy name to every record from this package."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.policy = _policy_name.get()
        return True


logging.getLogger("orb_extreme_platformone").addFilter(_PolicyContextFilter())
```

Set it at the top of `run` (`backend.py:213`):

```python
        _policy_name.set(policy_name)
```

Operators then include `%(policy)s` in their format string and every record carries it, including the 15 that never mention it.

**Two caveats.** `contextvars` do **not** propagate into `ThreadPoolExecutor` workers automatically — `retrieve.py:41`'s `pool.submit` would log the default `"-"`. Fix by capturing the context at submit time:

```python
# extract/retrieve.py:41
        import contextvars
        ctx = contextvars.copy_context()
        futures = [pool.submit(ctx.run, _one, table, filters) for table, filters in jobs]
```

And a `Filter` added to a logger only applies to records logged *through that logger*, not through its children. Attach it to a `Handler` instead if child-logger records must carry the field — which is the more robust placement once E-07's `__name__` loggers land.

---

## 3. What is already strong

| Practice | Evidence |
|---|---|
| **No bare or blanket excepts** | 14 handlers, all typed. Zero `except:`, zero `except Exception:` (AST-verified) |
| **Correct exception chaining** | `raise ... from exc` at `client.py:199` and `:218`; context preserved for debugging |
| **Error bodies bounded before logging** | `truncate_error_body` (`client.py:64-71`), applied at `:160` and `:211`, tested at `test_client.py:47` and `:229` |
| **Redirects fail closed** | `allow_redirects=False` on every outbound call (`client.py:154`, `:195`, `bootstrap.py:132`), with explicit 3xx rejection — a 307 cannot replay the login body to another host |
| **URL validation raises early and specifically** | `urls.py` — 5 distinct `ValueError` messages for empty, hostless, userinfo-bearing, query/fragment-bearing, and non-HTTPS inputs (`test_urls.py`, 70 lines) |
| **Fail-closed on missing bootstrap credentials** | `backend.py:218-225`, covered by `test_bootstrap_true_without_netbox_creds_fails_closed` |
| **Degradation is uniform and explained** | Every `except PlatformOneApiError` logs what the tick loses via a `degradation` string (`retrieve.py:64-71`, `correlate.py:81-86`, `backend.py:298-304`) |
| **Failure paths are tested** | 11 named tests: transport failure, login failure, invalid JSON, non-2xx, 401 re-login, per-chunk failure, all-chunks-failed, failed port table, failed cluster fetch, one-sided cluster filter failure |

**Also checked and found fine:** `client.py:227-228` raises `AssertionError` for an unreachable branch. Explicit `raise AssertionError(...)` is *not* stripped by `python -O` (only bare `assert` statements are) — verified. No change needed.

---

## 4. Error category coverage

The checklist's HTTP-status framing assumes a service that *returns* errors. This package is a batch worker that *consumes* them — it has no inbound surface, so there is no response to categorize. The relevant question is whether it correctly interprets the statuses it receives:

| Category | Received & handled? | Location | Gap |
|---|---|---|---|
| **400** validation | Partially | `client.py:210` | Folded into the generic `>= 400` path. ConfigState's documented "empty filter body" error (code 1727, `client.py:308-310`) is handled by *never sending* an empty filter rather than by parsing the response |
| **401** authentication | **Yes** — the one categorized case | `client.py:205-209` | Retried once with re-login. But after that retry it degrades like any other error instead of aborting (E-01) |
| **403** authorization | No | `client.py:210` | Indistinguishable from 500. `tests/test_backend_run.py` mocks a 403 but asserts only that degradation occurs |
| **404** not found | No | `client.py:210` | Retried in full every tick forever (E-01) |
| **429** rate limit | **No** | `client.py:210` | No `Retry-After` handling anywhere. See R-01 |
| **5xx** server | No | `client.py:210` | Not retried at all. See R-01 |
| **Transport** (DNS, TLS, timeout) | Yes, as a class | `client.py:197-199` | Correctly wrapped, but conflated with response errors under one type (E-01) |
| **Malformed body** | Partially | `client.py:214-225` | Invalid JSON and non-object JSON are caught; wrong-shaped valid JSON is not (see R-02) |

**E-01 is the single change that unlocks all of these.** Once `status_code` is carried, each row above becomes a property check rather than a code change.

---

## 5. Suggested sequencing

| Order | Findings | Rationale |
|---|---:|---|
| 1 | **E-02** | Self-contained, security-relevant, ~10 lines plus a test |
| 2 | **E-01** | Foundational — E-06, R-01 and R-04 all depend on the status code existing |
| 3 | **E-03** + **R-06** | Land the taxonomy and the propagation fix together; both touch `bootstrap.py` and `tests/test_bootstrap.py` |
| 4 | **E-04**, **E-05** (with **R-02**) | Both add `logger.exception`; do in one pass |
| 5 | **E-06** | Trivial once E-01 provides `status_code` |
| 6 | **E-07**, **E-08** | Observability polish; E-08's filter placement is cleaner after E-07's `__name__` loggers |

**Cross-audit note:** E-01 is a prerequisite for the resilience audit's R-01 retry classifier, and SOLID-audit S-04 moves the raise sites into a new `PlatformOneTransport` class. **Order: S-04 → E-01 → R-01**, or the same nine raise sites get edited three times.

---

## 6. Not applicable, with evidence

| Checklist item | Status |
|---|---|
| **Centralized error handler / async middleware wrapper** | **N/A** — no web framework, no request pipeline. There is no inbound surface: `Backend.run` is invoked by the Orb host, and `pyproject.toml:35-41` declares no server dependency. E-04's log-and-reraise wrapper is the closest equivalent |
| **Unhandled promise rejections** | **N/A** — no async code. Zero `async def`/`await`/`asyncio` in `src/` (AST-verified). The thread-pool equivalent is E-05/R-02 |
| **Event emitter error handling** | **N/A** — no event emitters or callback registration anywhere in `src/` |
| **Development vs production error detail** | **N/A by design** — errors are never rendered to an end user. All output is Python logging, whose verbosity the Orb host controls. `truncate_error_body` bounds detail identically in every environment, which is correct for a worker whose logs are operator-facing in all contexts |
| **User-friendly error messages** | **Partially N/A** — the audience is operators, not end users. Messages are already operator-oriented and actionable; `backend.py:219-222` is the best example, naming both remedies ("provide both or set `BOOTSTRAP: false`") |

---

## 7. Unable to verify

| Claim | Why | What would prove it |
|---|---|---|
| Whether Platform ONE ever echoes credentials in an error body | E-02's probe used a synthetic response; no real 401 body is captured in any fixture | A recorded (redacted) 401 from a failed production login |
| Whether Platform ONE returns 429, and with what headers | No 429 in any fixture or test; `client.py:210` treats it generically | The Platform ONE rate-limit documentation, or a captured 429 |
| Whether 403 vs 401 are used distinctly by the upstream | `tests/test_backend_run.py` mocks a 403 but asserts only degradation, not semantics | The Assets/ConfigState OpenAPI error responses, currently behind the vendor login wall |
| Whether the Orb host logs or swallows an exception propagating out of `run()` | Determines how much E-04's ERROR record actually adds | `worker/policy/` runner source, or an observed production tick failure |
| Whether ConfigState error code 1727 has siblings worth parsing | `client.py:308-310` names it, but no response-body error-code handling exists | The ConfigState error-code table from the vendor portal |
