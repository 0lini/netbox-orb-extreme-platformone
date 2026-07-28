# SOLID Principles Audit — `netbox-orb-extreme-platformone`

| | |
|---|---|
| **Date** | 2026-07-28 |
| **Commit** | `afda2a1` (branch `claude/software-architecture-analysis-kuervi`) |
| **Scope** | `src/orb_extreme_platformone/**` (31 modules), `tests/**` (16 modules), plus the installed `netboxlabs-orb-worker` and `netboxlabs-diode-sdk` supertypes |
| **Method** | AST analysis of inheritance edges, instantiation sites, and per-callee method usage; supertype contracts read from the installed SDK at `.venv/lib/python3.11/site-packages/worker/backend.py`; the LSP finding **empirically proven** by injecting a regression and running the suite both ways |
| **Baseline** | `uv run pytest -q` → **210 passed, 7 deselected**. Working tree restored clean after all experiments. |

---

## 1. Framing: SOLID against a mostly-functional codebase

This package contains **three classes** (`Backend`, `PlatformOneClient`, `PlatformOneApiError`), **one inheritance edge** into a supertype the project does not own, and **zero declared abstractions** — no `Protocol`, `ABC`, `abstractmethod`, `TypeVar`, or `Generic` anywhere in `src/` (grep-verified). Everything else is module-level functions over plain dicts.

That matters for scoring honesty:

- **SRP and OCP apply directly.** "One reason to change" and "extend without modifying" are about modules, not classes. Findings here are real.
- **LSP is nearly vacuous by inheritance** — one edge, and it is correct. But substitutability also governs *test doubles*, and there the codebase has a provable, currently-exploitable violation (S-01).
- **ISP and DIP score low partly because SOLID's OO framing penalizes idiomatic functional Python.** Depending on a module rather than an injected interface is normal here. I score the parts that demonstrably bite — measured in monkeypatch count and false-green risk — not the absence of Java-style interfaces.

I flag where a "violation" is really a style mismatch rather than a defect, so you can ignore those deliberately rather than by accident.

### Ratings

| Principle | Score | One-line justification |
|---|---:|---|
| **S**ingle Responsibility | **6** / 10 | Module decomposition is deliberate and mostly clean; `PlatformOneClient` carries 4 concerns, `backend.py` carries 5, `transform/common.py` is a utility junk drawer |
| **O**pen/Closed | **7** / 10 | Catalog- and table-driven design is genuinely open for extension; `Backend.run` and two `if/elif` chains are not |
| **L**iskov Substitution | **5** / 10 | The single real inheritance edge is correct — but the test doubles weaken the supertype precondition, and I proved it produces false greens today |
| **I**nterface Segregation | **4** / 10 | Zero declared interfaces; 10 extract functions depend on a concrete client while calling exactly one of its methods |
| **D**ependency Inversion | **3** / 10 | Every dependency is a concrete module import; the test suite pays for it with ~130 monkeypatch substitutions across 10 modules |
| **Overall** | **5** / 10 | Strong procedural design, weak on inversion. The one finding that costs correctness today is S-01. |

---

## 2. Findings

Severity is **1–10, where 10 = most important**.

---

### S-01 · Severity **9** · LSP: test doubles accept kwargs the real Diode classes reject — proven false green

**Principle:** Liskov Substitution (applied to test doubles).
**Location:** `tests/conftest.py:60-67` (`Rec`), substituted into 10 modules via `stub_sdk` (`tests/conftest.py:141-177`)

`Rec` is the base for all 13 Diode SDK stand-ins:

```python
class Rec:
    """Records constructor kwargs so tests can assert on them without the real protobuf SDK."""

    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)          # accepts ANY keyword
        self._kw = kw
```

It **weakens the supertype's precondition** — the classic Liskov violation. The real classes reject unknown fields:

```
real:  Interface(name='x', totally_bogus_field='y')
       → TypeError: Interface.__new__() got an unexpected keyword argument 'totally_bogus_field'
stub:  Interface(name='x', totally_bogus_field='y')
       → accepted: {'name': 'x', 'totally_bogus_field': 'y'}
```

**This is not theoretical. I proved it.** I added one bogus kwarg to `_lag_kwargs` (`lags.py:143`) and ran the suite:

| Suite | Result with the injected regression |
|---|---|
| `tests/test_transform_lags.py` — the **dedicated LAG suite**, 15 tests, stubbed | **15 passed** — false green |
| `tests/test_backend_run.py` — real SDK, incidental coverage | 1 failed |

The regression was caught only by accident, in a different file, on a path that suite happens to exercise. **Six test modules — `test_transform_{devices,fabric,lags,ports,virtual_chassis,wireless}.py`, 1,910 of 3,782 test LOC — run entirely against stubs that cannot fail this way.**

The exposure is wide: `Interface` alone accepts 41 parameters, `Device` 31, `Entity` 102. Any transform asserting a field name that Diode renamed or never had passes the transform suite and fails in production.

**Remediation** — validate stub kwargs against the real class's signature. `inspect.signature()` works on all 13 stubbed classes (verified).

```python
# tests/conftest.py — add to imports
import inspect

from netboxlabs.diode.sdk import ingester as _ingester

# tests/conftest.py:60-67 — replace Rec
class Rec:
    """Records constructor kwargs so tests can assert on them without the real protobuf SDK.

    Rejects any kwarg the real Diode class would reject, so a stubbed test
    cannot green-light a transform that fails in production.
    """

    def __init__(self, **kw) -> None:
        real = getattr(_ingester, type(self).__name__, None)
        if real is not None:
            unknown = sorted(set(kw) - set(inspect.signature(real).parameters))
            if unknown:
                msg = f"{type(self).__name__} stub got kwargs the real Diode class rejects: {unknown}"
                raise TypeError(msg)
        self.__dict__.update(kw)
        self._kw = kw
```

**Verified end to end:**

| Check | Result |
|---|---|
| Full suite with the fix, no regression injected | **210 passed, 7 deselected** — no existing test breaks |
| `test_transform_lags.py` with the fix + injected bogus kwarg | **13 failed** — regression now caught in its own suite |

The fix is behaviour-neutral on today's code and closes the hole permanently. This is the single highest-value change in this audit.

---

### S-02 · Severity **6** · ISP/DIP: ten extract functions depend on a concrete client, calling one of its methods

**Principle:** Interface Segregation (primary), Dependency Inversion (secondary).
**Location:** `extract/retrieve.py:21,47,79`, `extract/clusters.py:15`, `extract/correlate.py:16,71`, `extract/ports.py:38,74`, `extract/fabric.py:14`, `extract/wireless.py:14`

Every extract function annotates `client: PlatformOneClient` — a 345-line concrete class with two public methods and eight private ones, plus `threading.local()` session state, a token-refresh lock, and login logic. AST analysis of what each actually calls:

| Function | Uses |
|---|---|
| `retrieve_parallel` (`retrieve.py:20`) | `client.retrieve` |
| `extract_inferred_clusters` (`clusters.py:15`) | `client.retrieve` |
| `extract_cs_devices` (`correlate.py:16`) | `client.retrieve` |
| `correlated_records` (`correlate.py:71`) | `client.retrieve` |
| `retrieve_ok`, `extract_device_table_buckets`, `extract_port_tables`, `attach_interface_id_tables`, `extract_fabric_tables`, `extract_wireless_tables` | pass through only |

**Not one extract function calls `get_devices`.** That method is used exclusively at `backend.py:231`. The entire extract layer depends on an interface roughly twice as wide as it needs, and on a concretion rather than an abstraction.

**Remediation** — a one-method `Protocol`. This is textbook ISP and costs nothing at runtime:

```python
# src/orb_extreme_platformone/extract/retrieve.py — replace the client import
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator


class ConfigStateSource(Protocol):
    """The single ConfigState capability the extract layer needs."""

    def retrieve(self, table: str, filters: dict | None = None) -> Iterator[dict]:
        ...
```

Then widen the annotations — `client: ConfigStateSource` at `retrieve.py:21,47,79`, `clusters.py:15`, `correlate.py:16,71`, `ports.py:38,74`, `fabric.py:14`, `wireless.py:14`. `PlatformOneClient` satisfies it structurally with no declaration needed.

**Two caveats.** `client.retrieve` (`client.py:297-304`) also takes keyword-only `page_size` and `filter_chunk_size`; no extract caller passes either, so the narrow Protocol above matches every real call — but widen it if that changes. And `pyproject.toml:145-149` sets `replace-imports-with-any` for `netboxlabs.**`, so `ty` will not verify structural conformance until that exclusion is narrowed; the Protocol still documents the contract and enables lightweight fakes.

**Payoff:** the extract layer becomes testable with a five-line fake instead of `responses`-intercepted HTTP:

```python
class FakeSource:
    def __init__(self, rows: dict[str, list[dict]]) -> None:
        self._rows = rows

    def retrieve(self, table: str, filters: dict | None = None):
        yield from self._rows.get(table, [])
```

---

### S-03 · Severity **6** · DIP: concrete SDK imports force ~130 monkeypatch substitutions

**Principle:** Dependency Inversion.
**Location:** module-level SDK imports in 9 `transform/` modules; workaround at `tests/conftest.py:124-177`

Nine transform modules import Diode classes concretely at module scope — e.g. `devices.py:7-14`:

```python
from netboxlabs.diode.sdk.ingester import (
    Device, Entity, Location, Platform, Site, VirtualChassis,
)
```

There is no seam, so tests must rebind the name **in every module that imported it**. `stub_sdk` (`conftest.py:141-177`) loops 13 stub classes × 10 modules — **130 substitution attempts**, guarded by `if name in mod.__dict__`.

The cost is concrete and ongoing:

1. **The module list is manually maintained** (`conftest.py:150-171`). A new transform module that imports an SDK class is silently *not* stubbed — its tests would run against real protobufs and fail confusingly, or worse, pass while asserting on the wrong object shape.
2. **The class list is manually maintained** (`STUB_CLASSES`, `conftest.py:125-139`). A transform importing a 14th SDK class is silently unstubbed.
3. Combined with S-01, the substitution mechanism is both wide and unvalidated.

**Verdict — do not "fix" this with constructor injection.** Threading a factory object through 15 pure mapping functions would damage the codebase's best quality (transform is pure and directly callable). The proportionate fix is to make the substitution mechanism self-maintaining:

```python
# tests/conftest.py:150-177 — replace the hand-maintained module tuple
import pkgutil
import importlib

import orb_extreme_platformone.transform as transform_pkg


@pytest.fixture
def stub_sdk(monkeypatch):
    """Swap real Diode SDK classes for recording stubs in every transform module.

    Discovers modules by walking the package so a new transform module cannot
    be silently left unstubbed.
    """
    modules = [transform_pkg]
    for info in pkgutil.iter_modules(transform_pkg.__path__):
        modules.append(importlib.import_module(f"{transform_pkg.__name__}.{info.name}"))

    for name, cls in STUB_CLASSES.items():
        for mod in modules:
            if name in mod.__dict__:
                monkeypatch.setattr(mod, name, cls)
    return STUB_CLASSES
```

Pair it with a guard that fails if a transform module imports an SDK class with no stub — closing the second gap:

```python
# tests/conftest.py — new test, or an autouse assertion
def test_every_imported_sdk_class_has_a_stub() -> None:
    """A transform module importing an unstubbed Diode class would run unstubbed."""
    from netboxlabs.diode.sdk import ingester

    sdk_names = {n for n in dir(ingester) if n[:1].isupper()}
    for info in pkgutil.iter_modules(transform_pkg.__path__):
        mod = importlib.import_module(f"{transform_pkg.__name__}.{info.name}")
        imported = {n for n in mod.__dict__ if n in sdk_names}
        missing = imported - set(STUB_CLASSES)
        assert not missing, f"{mod.__name__} imports unstubbed Diode classes: {sorted(missing)}"
```

---

### S-04 · Severity **6** · SRP: `PlatformOneClient` owns four independent concerns

**Principle:** Single Responsibility.
**Location:** `client.py:83-345`

One 262-line class with four distinct reasons to change:

| Concern | Lines | Changes when… |
|---|---|---|
| **Token lifecycle** — login, expiry skew, refresh, 401 re-auth | `:101-121`, `:130-176`, `:205-209` | auth mode changes (OAuth, mTLS, rotating keys) |
| **HTTP transport** — thread-local sessions, redirect policy, error→exception mapping | `:123-128`, `:186-226` | retry/backoff or pooling policy changes (see architecture audit F-04, F-05) |
| **Pagination** — two different upstream schemes behind `_paginate` | `:230-282` | an API adds cursor paging |
| **Filter chunking** — splitting oversized ID lists, per-chunk failure isolation | `:284-345` | gateway body limits change |

The tell is `retrieve` (`:297-345`): 48 lines where chunking, per-chunk error accumulation, logging, and delegation to pagination all interleave. Its docstring needs 19 lines to explain the combined behaviour.

**Remediation** — the seam is clean because pagination and chunking are already independent of auth. Split the transport core out; the public surface stays identical:

```python
# src/orb_extreme_platformone/http.py  (new)
"""Authenticated POST transport for Platform ONE: token lifecycle + error mapping."""

class PlatformOneTransport:
    """Owns credentials, sessions, and turning HTTP failures into PlatformOneApiError."""

    def __init__(self, *, base_url, api_token=None, username=None, password=None, timeout=60): ...
    def post(self, path: str, params: dict, body: dict) -> dict: ...   # was _post
```

`PlatformOneClient` then holds a `PlatformOneTransport` and keeps only pagination + chunking — `_post` calls become `self._transport.post(...)`. `client.py:104` (`require_https_url`), `:110-121` (headers/expiry), `:123-176` (session/login) and `:178-228` (`_post`) move wholesale; nothing else changes.

**Sequencing note:** this refactor touches the same lines as architecture-audit F-04 (shared executor / session close) and F-05 (retry adapter). Do the split *first*, then land both HTTP fixes inside `PlatformOneTransport` where they belong. `tests/test_client.py` (335 lines) drives the public API and should pass unchanged.

---

### S-05 · Severity **5** · OCP: `Backend.run` must be modified to add a discovery domain

**Principle:** Open/Closed.
**Location:** `backend.py:212-275`, plus `:23-37` and `:58-68`

The pipeline's *data* is open for extension — adding a ConfigState table is a one-line entry in `extract/tables.py`, and `PORT_ENTITY_TABLE_KEYS` (`port_constants.py:14`) derives from it so nothing drifts. That part is genuinely well done.

Adding a **domain** is not. The three phases are hardcoded as sibling calls in `run`:

```python
vc_entities, vc_memberships = self._virtual_chassis_entities(client, scoped, policy_name)   # :250
port_entities, primary_ips_by_cs_id, fabric_by_cs_id = self._port_entities(...)              # :254
radio_entities = self._radio_entities(client, scoped, policy_name)                           # :259
```

A fourth domain (LLDP neighbours, stacking, power inventory) requires editing **four existing regions**: `run`'s body (`:250-273`), the import block (`:23-37`), `__all__` (`:58-68`), and a new `_<domain>_entities` static method — on top of the genuinely-new catalog, extract, and transform modules.

**Remediation** — the three phase methods already share a shape (`client, records, policy_name` → entities). Make the phase list data:

```python
# backend.py — above class Backend
@dataclass(frozen=True, slots=True)
class DiscoveryPhase:
    """One per-device discovery domain: fetch its tables, map its entities."""

    name: str
    predicate: Callable[[dict], bool]
    extract: Callable[[PlatformOneClient, list[str], str], tuple[dict, list[str]]]
    to_entities: Callable[..., list[Entity]]
    degradation: str
```

**I am not proposing this as a drop-in.** The three phases are not actually uniform: `_port_entities` returns three values that later phases consume (`primary_ips_by_cs_id`, `fabric_by_cs_id`), `_virtual_chassis_entities` feeds `vc_memberships` back into `devices_to_entities`, and the emission order between them is load-bearing (architecture audit F-08). A registry that ignored those couplings would break NetBox writes.

**The honest recommendation: leave `run` as it is for now.** Three phases with real inter-phase data flow are clearer written out than hidden behind a registry that needs escape hatches for two of the three. Revisit when a fourth *independent* domain actually arrives — at which point the extraction is driven by a real second example rather than speculation. Recording this as a known limit, not a defect to fix today.

---

### S-06 · Severity **4** · SRP: `transform/common.py` is a utility junk drawer

**Principle:** Single Responsibility.
**Location:** `transform/common.py:1-175`

One module, five unrelated reasons to change:

| Concern | Lines |
|---|---|
| NetBox schema constants (`CF_*`, `PROVENANCE_TAGS`, `MANUFACTURER`) | `:21-30` |
| Diode nested-ref builders (`_device_ref`, `_device_identity_fields`) | `:37-82` |
| Interface kwarg assembly (`_interface_custom_fields`, `_interface_identity_kwargs`) | `:85-130` |
| Scalar type coercion (`_coerce_bool`, `_coerce_int`) | `:92-104`, `:147-155` |
| Format normalization (`_normalized_mac`, `_compact_token`, `_explicit_cidr`) | `:133-144`, `:158-175` |

Everything in `transform/` imports it (13 of 15 modules), so any change ripples package-wide. It is also the module carrying the architecture audit's F-02 layering inversion (`:16`, `from orb_extreme_platformone import bootstrap`) — not a coincidence: junk drawers attract dependencies.

**Remediation** — split along the existing seams. This also resolves F-02, since only the constants group touches `bootstrap`:

```
transform/common.py  →  transform/schema_refs.py   # CF_*, PROVENANCE_TAGS, MANUFACTURER  (:21-30)
                        transform/device_refs.py   # _device_ref, _device_identity_fields (:37-82)
                        transform/interface_refs.py# _interface_* kwargs builders          (:85-130)
                        transform/scalars.py       # _coerce_*, _normalized_mac,
                                                   # _compact_token, _explicit_cidr
```

Keep `common.py` as a re-export shim so the 13 importers need no edit in the same commit:

```python
# transform/common.py — after the split
from .device_refs import _device_identity_fields, _device_ref  # noqa: F401
from .interface_refs import _interface_custom_fields, _interface_identity_kwargs  # noqa: F401
from .scalars import _coerce_bool, _coerce_int, _compact_token, _explicit_cidr, _normalized_mac  # noqa: F401
from .schema_refs import CF_CLUSTER_ID, CF_DEVICE_ID, MANUFACTURER, PROVENANCE_TAGS  # noqa: F401
```

**Sequencing:** do this *after* naming-audit N-01/N-07 (which rename several of these helpers) to avoid two churns over the same lines.

---

### S-07 · Severity **4** · SRP: `backend.py` mixes five responsibilities

**Principle:** Single Responsibility.
**Location:** `backend.py:71-194` (module functions) and `:197-409` (the class)

| Responsibility | Location | Changes when… |
|---|---|---|
| Policy/env config resolution | `_cfg:84`, `_cfg_or_env:88`, `_build_client:188` | config keys or precedence change |
| Scope parsing/validation | `_scope_sites:96` | policy scope syntax changes |
| Record indexing | `_records_by_cs_id:118`, `_device_names:142`, `_fanout_context:164` | the record shape changes |
| Tick orchestration | `Backend.run:212` | phase ordering or a new domain |
| Per-domain fan-out | `_virtual_chassis_entities:278`, `_port_entities:329`, `_radio_entities:390` | a domain's tables change |

`Backend` itself is *not* a God class — 4 methods, 3 of them `@staticmethod` that never touch `self`. The accretion is at module level.

**Remediation** — extract the two responsibilities with no dependency on the others:

```python
# src/orb_extreme_platformone/config.py  (new)  ← _cfg, _cfg_or_env, _build_client, _scope_sites
"""Policy-config and environment resolution for the worker tick."""

# src/orb_extreme_platformone/extract/index.py  (new)  ← _records_by_cs_id, _device_names, _fanout_context
"""ConfigState fan-out indexes built from correlated device records."""
```

The indexing helpers belong in `extract/` — they operate on the record shape `correlate.py:112-119` produces and are pure. That leaves `backend.py` as orchestration plus the three fan-out methods, roughly 200 lines.

The three `@staticmethod` fan-outs could become module functions (they use no class state), but keeping them on `Backend` documents that they are tick phases. Either is defensible.

---

### S-08 · Severity **3** · OCP: two `if/elif` chains resist extension

**Principle:** Open/Closed.

Most mapping in this codebase is correctly table-driven and open for extension — `PLATFORM_BY_FUNCTION` (`identity.py:15`), `ROLE_BY_FUNCTION` (`identity.py:97`), `VERIFIED_OPER_DUPLEX` / `VERIFIED_POE_CLASSIFICATION` (`port_constants.py:35-45`), `_TYPE_BY_SPEED_AND_CONNECTOR` (`port_constants.py:50`), `_RADIO_TYPE_BY_MODE` (`wireless_rf.py:7`). Adding a vendor code is a dict entry.

Two places break the pattern:

**`_auth_from_encryption` (`wireless_auth.py:8-37`)** — 7 `if` nodes, 5 returns, 40 string literals, matching by substring across four token groups. Supporting a new encryption label means editing the chain and reasoning about ordering (`"wpa2"` must not be caught by the `"wpa"` branch).

**`_channel_frequency_mhz` (`wireless_rf.py:53-67`)** — the band chain, already covered as naming-audit N-11 with a table-driven fix. Not repeated here.

**Remediation for the auth chain** — ordered rules keep the precedence explicit and make additions data:

```python
# wireless_auth.py — module level
# Ordered: first matching rule wins. More specific tokens must precede
# their prefixes ("wpa2"/"wpa3" before bare "wpa").
_AUTH_TYPE_RULES: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"8021x", "enterprise", "radius", "eap", "dot1x"}), "wpa-enterprise"),
    (frozenset({"psk", "ppsk", "sae", "personal", "wpa2", "wpa3", "wpa"}), "wpa-personal"),
)
_CIPHER_RULES: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"tkip"}), "tkip"),
    (frozenset({"wpa2", "wpa3", "aes", "ccmp", "gcmp", "sae"}), "aes"),
)


def _match_rule(compact: str, rules) -> str | None:
    return next((value for tokens, value in rules if any(t in compact for t in tokens)), None)
```

**Caveat — this is not a pure drop-in.** The original has exact-match special cases the token rules do not cover: `compact in {"typewpa", "wpaeap"}` for the type (`:26`) and `compact in {"wpa", "wpaeap", "typewpa"}` forcing `tkip` (`:31`). Those must be kept as explicit pre-checks before the rule scan, or the mapping changes. `tests/test_transform_wireless.py` covers these cases — treat a green run as the acceptance criterion.

Given the chain has exactly one call site and is fully covered by tests, **this is optional polish**, not a defect.

---

### S-09 · Severity **3** · Inconsistent error contract between sibling functions

**Principle:** Liskov Substitution (in spirit — duck-typed callables).
**Location:** `client.py:284-296` vs `client.py:266-282`

Two sibling private methods with near-identical names and incompatible failure contracts:

```python
def _retrieve_pages(self, table, filters, *, page_size) -> Iterator[dict]:      # :266  — RAISES
    ...

def _retrieve_chunk(self, table, filters, *, page_size) -> list[dict] | PlatformOneApiError:   # :284
    """Fetch one filter chunk to a list, or return the API error."""
    try:
        return list(self._retrieve_pages(table, filters, page_size=page_size))
    except PlatformOneApiError as exc:
        return exc                                                              # RETURNS the error
```

`_retrieve_chunk` returns an exception **as a value** — an unlabelled Result type. Callers must `isinstance`-check (`:329`), and a caller who reasonably assumes the sibling's raising contract would treat a `PlatformOneApiError` instance as a row list. The reason is sound (`:322-324`: preserve rows from earlier chunks), but the contract is invisible in the name.

**Remediation** — make the Result explicit, or rename to advertise it. Minimal version:

```python
# client.py:284 — rename so the contract is in the name
def _retrieve_chunk_or_error(
    self, table: str, filters: dict, *, page_size: int,
) -> list[dict] | PlatformOneApiError:
    """Fetch one filter chunk to a list, or return (not raise) the API error.

    Returning the error keeps rows already fetched from earlier chunks; see
    the caller in ``retrieve``.
    """
```

Update the single call site (`client.py:328`). `extract/retrieve.py:33-37` already models the same idea better, returning `(table, rows, exc)` — worth converging on that shape if this is touched again.

---

### S-10 · Severity **2** · The SDK's `IngestSink` abstraction is inherited but unused

**Principle:** Interface Segregation / Dependency Inversion (informational).
**Location:** supertype at `worker/backend.py:17-46` and `:52-72`; subclass at `backend.py:197`

The worker SDK defines a `@runtime_checkable Protocol`:

```python
class IngestSink(Protocol):
    def ingest(self, entities: Iterable[Entity], **kwargs) -> None: ...
    def record_failure(self, error: Exception, **kwargs) -> None: ...
```

and `Backend.__init__` stores it as `self.ingest_sink` (`worker/backend.py:72`). `orb_extreme_platformone.Backend` does not override `__init__`, so it inherits correctly — **no LSP violation**, and `__main__.py:71`'s bare `Backend()` is valid against the `None` default.

Two observations, both informational:

1. **`self.ingest_sink` is never read** (grep-verified). The worker supports ingesting outside the scheduled `run()` cycle — e.g. an HTTP-triggered sync — and this backend does not use it. That is a legitimate scope decision; `run()`-only is the simpler contract.
2. **This is the one place the project already depends on a proper abstraction**, and it comes from the SDK rather than the codebase. It is the model S-02 proposes copying.

**No change recommended.** Noted so a future reviewer does not mistake the unused attribute for an oversight. If per-tick failure reporting is ever wanted, `record_failure` is the hook — `backend.py:295-304` and `retrieve.py:64-71` currently only log degradations.

---

## 3. What is already done well

Worth stating so a remediation pass does not damage it:

| Practice | Evidence |
|---|---|
| **Table-driven mapping** (OCP, done right) | `PORT_TABLES`/`WIRELESS_TABLES`/`FABRIC_DEVICE_TABLES` (`extract/tables.py`), `VERIFIED_*` maps (`port_constants.py:31-45`). Adding a table or vendor code requires no logic change |
| **Derived key sets prevent drift** | `PORT_ENTITY_TABLE_KEYS` (`port_constants.py:14`) and `_INTERFACE_ID_SOURCE_KEYS` (`extract/ports.py:15`) are computed from catalogs, not restated |
| **Correct single inheritance** | `Backend(WorkerBackend)` matches the supertype's `run(self, policy_name, policy, **kwargs) -> Iterable[Entity]` and implements `describe()` as a `@classmethod` — satisfying the SDK's `__init_subclass__` checks (`worker/backend.py:74-103`) |
| **Pure transform layer** | 15 modules of plain functions over dicts, directly callable with no construction ceremony. Do not sacrifice this to constructor injection (see S-03) |
| **Uniform degradation** | Every `except PlatformOneApiError` logs what the tick loses and continues (`retrieve.py:64-71`, `correlate.py:80-86`, `backend.py:295-304`) |
| **Lazy `Backend` export** | `__init__.py:15-24` keeps SDK imports out of version metadata; CI smoke-tests it against the real `load_class` (`ci.yml`, package job) |

---

## 4. Suggested sequencing

| Order | Findings | Rationale |
|---|---:|---|
| 1 | **S-01** | Verified behaviour-neutral (210 pass) and closes a live false-green hole. Do this first, alone |
| 2 | **S-03** | Same file as S-01; makes the stub mechanism self-maintaining while it is already open |
| 3 | **S-02** | Additive `Protocol`; no behaviour change, unlocks cheap extract-layer fakes |
| 4 | **S-04** | Do *before* architecture-audit F-04/F-05 so both HTTP fixes land in `PlatformOneTransport` |
| 5 | **S-06**, **S-07** | Module splits; run *after* naming-audit N-01/N-07 to avoid double churn on the same lines |
| 6 | **S-09**, **S-08** | Optional polish, fully test-covered |
| — | **S-05**, **S-10** | **No action.** Recorded as known limits with rationale |

---

## 5. Unable to verify

| Claim | Why | What would prove it |
|---|---|---|
| Whether `ty` would actually enforce the S-02 `Protocol` | `pyproject.toml:145-149` sets `replace-imports-with-any = ["netboxlabs.**", "worker.**"]`, so SDK-typed values degrade to `Any` and structural checks may be skipped | Add the Protocol, narrow the exclusion, and run `uv run ty check --error-on-warning` |
| Whether any *current* transform passes a kwarg the real SDK rejects on a path only stubbed tests reach | The strengthened `Rec` passes all 210 tests today, so no such bug exists on covered paths — but coverage is 90%, not 100% | `pytest --cov --cov-report=term-missing` with the S-01 fix applied, inspecting uncovered `transform/` branches |
| Whether `inspect.signature()` stays available on Diode classes across SDK versions | Verified for all 13 stubbed classes on the version in `uv.lock`; the classes are protobuf-backed wrappers whose introspection surface could change | Pin the behaviour with a test asserting `inspect.signature` works for every entry in `STUB_CLASSES` |
| Whether a fourth discovery domain would fit the S-05 phase abstraction | Only three phases exist, and two have inter-phase data flow; no second independent example to generalize from | An actual fourth domain (LLDP neighbours, power inventory) — design the registry against it, not before |
| Whether `IngestSink` is expected to be used by workers of this kind | The SDK marks it "for integrations that ship entities outside `run()`"; no in-repo requirement for HTTP-triggered sync | The Orb Agent integration guide, or a product requirement for on-demand sync |
