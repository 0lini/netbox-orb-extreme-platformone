# Readability & Naming Audit — `netbox-orb-extreme-platformone`

| | |
|---|---|
| **Date** | 2026-07-28 |
| **Commit** | `8c0f391` (branch `claude/software-architecture-analysis-kuervi`) |
| **Scope** | `src/orb_extreme_platformone/**` (31 modules, 3,946 LOC), `tests/**` (16 modules, 3,782 LOC), `pyproject.toml` lint config |
| **Method** | AST walk over all 31 source modules for identifier casing, parameter shape, return arity, docstring/code ratio, ternary nesting, boolean-operand count; plus `ruff` run under an isolated strict config to quantify what the project's `ignore` list currently hides |
| **Baseline** | `uv run ruff check src/` → **clean**. `uv run pytest -q` → **210 passed, 7 deselected**. |

---

## 1. Executive summary

Mechanical naming hygiene in this codebase is **excellent and machine-enforced**. `ruff` runs with `select = ["ALL"]`, which means `pep8-naming` (N), builtin-shadowing (A), and flake8-comprehensions are all live — and the tree passes clean. I verified independently rather than trusting the gate:

| Check | Result |
|---|---|
| camelCase in functions, args, or variables | **0** across 31 modules |
| Builtin shadowing (`id`, `type`, `filter`, …) | **0** |
| Classes not noun-phrased | **0** (`Backend`, `PlatformOneClient`, `PlatformOneApiError`) |
| Constants not `UPPER_CASE` | **0** |
| Private-underscore convention breaks | **0** |
| British spellings (`behaviour`, `colour`, `-ise`, `analyse`, `centre`, `licence`) | **0** — consistently American |
| Nested/chained ternaries | **0** (27 ternaries, all single-level) |
| Identifiers ≤ 2 chars | **3** (`cs` ×2, `ts` ×1) |

So the "cryptic `u` vs `user`" class of problem is essentially absent. **The real readability cost is elsewhere: semantic overloading of good-looking names.** Three identifiers each carry multiple distinct meanings, and the domain has three different things all called some variant of "device id". Those cost more reader-time than any casing issue would.

A second theme: **`pyproject.toml:105-134` suppresses ten lint rules that are doing real work.** Re-running `ruff` with those suppressions lifted surfaces **156 findings** in `src/` alone:

| Rule | Hidden count | What it means |
|---|---|---|
| `ANN001` | 27 | Unannotated function arguments |
| `PLR2004` | 18 | Magic value comparisons |
| `ANN202` | 8 | Private functions with no return type |
| `PLR0913` | 8 | More than 5 arguments |
| `C901` | 4 | Cyclomatic complexity > 10 |
| `D205`/`D401`/`D102`/`D103`/`D107` | 9 | Docstring form |
| `FBT001`/`FBT002`/`FBT003` | 4 | Boolean parameters |
| `ANN003` | 2 | Unannotated `**kwargs` |
| `PLR0912` | 1 | > 12 branches |

`D213` accounts for a further 75, but that one is a pure style preference the project has legitimately settled the other way — ignore it.

### On "too many comments = code smell" — not the case here

I measured it rather than asserting it. Across `src/`: **2,426 code lines, 156 comments, 785 docstring lines — ratio 0.39.** That is healthy. Only **one** function has a docstring longer than its body (`resolve_location`, 9 doc : 7 code at `identity.py:158`), and the largest docstrings sit on the largest functions (`ports_to_entities` 24:64, `radios_to_entities` 17:119).

More importantly, the comment *content* is load-bearing. This worker maps a vendor API whose OpenAPI specs sit behind a login wall, and the comments overwhelmingly record **why a value is deliberately not asserted** — `port_constants.py:29` ("Verified in-repo today: oper_speed 4, connector_type 1/2"), `lags.py:29-32` (why `AssetLagConfig.enabled` is untrusted), `common.py:45-53` (why nested Device stubs need site/role/device_type). That knowledge has no other home in the repo. **Do not treat this comment density as a defect to reduce.** The one place a comment genuinely substitutes for a name is N-11.

---

## 2. Findings

Severity is **1–10, where 10 = most important**.

---

### N-01 · Severity **7** · `key` carries five distinct meanings across `transform/`

**Category:** Naming — semantic overloading.

The single largest readability cost in the package. `key` is the most common loop variable in `transform/`, and it means something different in each of these:

| Referent | Locations |
|---|---|
| **`asset_interface_id`** — the ConfigState join key | `port_join.py:20`, `physical_ports.py:155`, `lags.py:81,186`, `wireless.py:156,163,169`, `ips.py:166` |
| **Catalog transform-key** (`"port_configs"`, `"lag_states"`, …) | `extract/retrieve.py:104,113-115`, `extract/ports.py:30,52,59`, `ips.py:206` |
| **Output dict field name** (`"primary_ip4"` / `"primary_ip6"`) | `ips.py:35` |
| **Composite tuple** `(asset_device_id, port_name)` | `ips.py:24`, `port_join.py:66-67` |
| **Lookup key into a mode map** | `wireless_rf.py:74` |
| **Row field name** being scanned | `fabric.py:15` |

`physical_ports.py:155-162` needs a comment to say what `key` is — the tell that the name is failing:

```python
    for key in sorted(set(configs) | set(states)):
        ...
        # `key` is asset_interface_id (required on port config/state).
        if key in lag_interface_ids:
```

**Remediation** — rename by referent; the comments then become redundant and can be deleted.

```python
# physical_ports.py:155  (and lags.py:186, wireless.py:156-169, ips.py:166)
    for interface_id in sorted(set(configs) | set(states)):
        config = _first_row(configs, interface_id, table="port_configs")
        state = _first_row(states, interface_id, table="port_states")
        ...
        if interface_id in lag_interface_ids:
            continue

# extract/retrieve.py:104  — catalog keys
    for (table_key, (_, filter_field)), rows in retrieve_ok(...):
        for row in rows:
            device_id = str(row.get(filter_field) or "")
            if device_id in tables_by_device:
                tables_by_device[device_id][table_key].append(row)

# ips.py:35  — output field name
    for version, cidr in candidates:
        field = "primary_ip4" if version == 4 else "primary_ip6"
        result.setdefault(field, cidr)
```

Rename the parameters too: `_first_row(grouped, key, *, table)` → `_first_row(grouped, interface_id, *, table)` at `port_join.py:26` and `:45`.

---

### N-02 · Severity **7** · Three different things are called "device id"

**Category:** Domain terminology — ambiguity.

The two upstream APIs each have a device identifier, they are different types with different lifetimes, and the codebase uses four names for the three concepts:

| Name | Actually is | Type | Count |
|---|---|---|---|
| `device_id` (Assets row field) | Assets API device id → `CF_DEVICE_ID` | int **or** str | `correlate.py:66,107`, `devices.py:82-83` |
| `cs_device_id` | ConfigState `AssetDevice.id` UUID | str | 15 sites |
| `asset_device_id` | **the same UUID**, as a ConfigState *filter field name* | str | 28 sites |
| `device_id` / `device_ids` (local var) | **also the same UUID** | str | `retrieve.py:96,113`, `backend.py:173,354,361`, `ports.py:33,69` |

So `device_id` at `correlate.py:107` and `device_id` at `retrieve.py:113` are different identifiers from different APIs — and `asset_device_id` refers to the *ConfigState* id, while the *Assets* id is the one whose row is called `asset`. The naming actively points the wrong way.

`extract/clusters.py:15` gets it right (`asset_device_ids: list[str]`) while `extract/retrieve.py:79` and `backend.py:173` call the identical values `device_ids` — inconsistent within one layer.

**Remediation** — one rename plus a glossary entry (§3.4). `asset_device_id` is fixed by the upstream API and must stay in dict lookups; only *local variables and parameters* change:

```python
# extract/retrieve.py:78-96
def extract_device_table_buckets(
    client: PlatformOneClient,
    cs_device_ids: list[str],          # was: device_ids
    catalog: TableCatalog,
    ...
) -> tuple[dict[str, dict[str, list[dict]]], list[str]]:
    """...

    ``cs_device_ids`` are ConfigState AssetDevice UUIDs, not Assets device ids.
    """
    tables_by_device: dict[str, dict[str, list[dict]]] = {
        cs_device_id: {key: [] for key in catalog} for cs_device_id in cs_device_ids
    }
```

Apply the same rename at `backend.py:173,340,361,392`, `extract/ports.py:74`, `extract/fabric.py:16`, `extract/wireless.py:16`. `extract/clusters.py:15` already reads correctly.

---

### N-03 · Severity **6** · `ANN001`/`ANN202`/`ANN003` suppression hides 37 missing annotations

**Category:** Return-type clarity / signature legibility.
**Location:** `pyproject.toml:123-125`

The suppression is described as "Keep existing helper-style APIs without full annotation/docstring churn" (`pyproject.toml:122`). Measured cost: **27 unannotated arguments, 8 unannotated private return types, 2 unannotated `**kwargs`.**

The expensive subset is the **callable parameters**, where the reader has no way to know the expected signature:

| Parameter | Location | Actual contract |
|---|---|---|
| `predicate` | `backend.py:118,164`, `ips.py:43` | `Callable[[dict], bool]` — but `ips.py:43` is `Callable[[dict, IPv4Interface \| IPv6Interface], bool]`, a *different* arity under the same name |
| `key_fn` | `extract/correlate.py:29` | `Callable[[dict], str \| None]` |
| `total_pages` | `client.py:239` | `Callable[[dict, int], int]` |
| `config` | `backend.py:84,88,188` | `Policy.config` — a pydantic model read via `getattr` |

`_cfg` and `_cfg_or_env` (`backend.py:84-93`) have neither parameter nor return annotations, and they gate every configuration value in the worker.

**Remediation** — annotate the callables first; that is most of the value for a handful of lines.

```python
# backend.py:12-20 (imports)
if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

# backend.py:84-93
def _cfg(config: object, key: str, default: object = None) -> object:
    return getattr(config, key, default) if config is not None else default


def _cfg_or_env(config: object, key: str, *, default: object = None) -> object:
    """Policy config wins when set (including empty string); else environment."""
    value = _cfg(config, key, None)
    if value is not None:
        return value
    return os.environ.get(key, default)

# backend.py:118 / :164
    records: list[dict], *, predicate: Callable[[dict], bool],

# client.py:239
    total_pages: Callable[[dict, int], int],

# extract/correlate.py:29
def index_unique(items: Iterable[dict], key_fn: Callable[[dict], str | None], *, label: str) -> dict:
```

Then narrow the suppression instead of dropping it wholesale, so new code is held to the standard while existing helpers are grandfathered:

```toml
# pyproject.toml — remove "ANN001", "ANN003", "ANN202" from the global ignore list
[tool.ruff.lint.per-file-ignores]
# Grandfathered: helper-style APIs predating the annotation requirement.
"src/orb_extreme_platformone/transform/*.py" = ["ANN001"]
```

`ips.py:43`'s `predicate` should additionally be renamed — see N-07.

---

### N-04 · Severity **6** · `device: object` in five signatures is a type lie

**Category:** Signature clarity.
**Location:** `physical_ports.py:60,85,139`, `lags.py:105,162`

```python
def _iface_base_kwargs(
    *,
    device: object,          # ← always a Device from _device_ref()
    name: str,
```

Every caller passes the `Device` built by `_device_ref` (`common.py:37`, invoked at `ports.py:60` and `wireless.py:235`). Annotating it `object` tells the reader nothing, permits any argument, and gives `ty` nothing to check — while the neighbouring `wireless.py:64` writes the same parameter as bare `device` (unannotated). Three spellings for one concept.

The likely motive is avoiding a runtime import of the Diode SDK. `TYPE_CHECKING` solves that at zero runtime cost — and `physical_ports.py:5` already imports `Interface` from the same module at runtime, so the SDK is loaded regardless.

**Remediation** — `physical_ports.py:5` and `lags.py:5` already do `from netboxlabs.diode.sdk.ingester import Entity, Interface`; extend the import and use it:

```python
# physical_ports.py:5
from netboxlabs.diode.sdk.ingester import Device, Entity, Interface

# physical_ports.py:60, :85, :139  — and lags.py:105, :162
    device: Device,
```

Apply to `wireless.py:64` (`device` → `device: Device`) so all three sites agree.

---

### N-05 · Severity **5** · Eight functions exceed five parameters; the worst takes eleven

**Category:** Function signature — parameter count.
**Location:** `pyproject.toml:113` disables `PLR0913`

| Params | Function | Location |
|---|---|---|
| **11** | `_physical_port_entities` | `physical_ports.py:137` |
| 9 | `_port_kwargs` | `physical_ports.py:83` |
| 9 | `_lag_kwargs` | `lags.py:103` |
| 9 | `_lag_entities` | `lags.py:160` |
| 7 | `_paginate` | `client.py:230` |
| 6 | `_iface_base_kwargs`, `retrieve_ok`, `extract_device_table_buckets` | `physical_ports.py:58`, `retrieve.py:46`, `retrieve.py:78` |

All are keyword-only, which mitigates call-site confusion — but `ports.py:90-102` still has to spell out ten arguments, and six of them (`configs`, `states`, `vlans`, `capabilities`, `poe_states`, `poe_configs`, `interface_ips`) are the *same* join-table bundle also passed to `_lag_entities` at `ports.py:77-87`. That is one concept travelling as seven parameters, twice.

**Remediation** — bundle the join tables. This turns 11 params into 5 and 9 into 4:

```python
# new — transform/port_join.py, after _by_key
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class JoinedPortTables:
    """One switch's ConfigState port tables, each grouped by asset_interface_id."""

    configs: dict[str, list[dict]]
    states: dict[str, list[dict]]
    vlans: dict[str, list[dict]]
    poe_states: dict[str, list[dict]]
    poe_configs: dict[str, list[dict]]
    interface_ips: dict[str, list[dict]]
    capabilities: dict[tuple[str, str], dict]

# physical_ports.py:137
def _physical_port_entities(
    *,
    device: Device,
    tables: JoinedPortTables,
    lag_names: set[str],
    lag_interface_ids: set[str],
    membership: dict[str, str],
) -> tuple[list[Entity], dict[str, str]]:
```

`ports.py:66-73` already builds all seven in one block, so the construction site is a single `JoinedPortTables(...)` call. Then remove `"PLR0913"` from `pyproject.toml:113`; `_paginate` (7) and the two `retrieve.py` helpers (6) would need `# noqa: PLR0913` or a raised `max-args`:

```toml
[tool.ruff.lint.pylint]
max-args = 7   # keyword-only builders in transform/ and extract/
```

---

### N-06 · Severity **5** · Multi-value tuple returns are unlabelled positional contracts

**Category:** Return-type clarity.

| Arity | Function | Return |
|---|---|---|
| **5** | `_lag_entities` (`lags.py:160`) | `tuple[list[Entity], set[str], set[str], dict[str, str], dict[str, str]]` |
| **4** | `_fanout_context` (`backend.py:164`) | `tuple[dict[str, dict], list[str], dict[str, str], dict[str, dict]]` |
| **3** | `_port_entities` (`backend.py:329`) | `tuple[list[Entity], dict[str, dict[str, str]], dict[str, dict]]` |

`_lag_entities` is the sharpest case: positions 2 and 3 are both `set[str]` (`lag_names`, `lag_interface_ids`) and positions 4 and 5 are both `dict[str, str]` (`membership`, `emitted_keys`). Swapping either pair at the unpack site (`ports.py:77`) type-checks cleanly and silently corrupts LAG membership. The type annotation cannot catch it and neither can `ty`.

**Remediation** — `NamedTuple` gives names, keeps tuple unpacking working at every existing call site, and costs nothing at runtime:

```python
# lags.py — above _lag_entities
from typing import NamedTuple

class LagResult(NamedTuple):
    """LAG entities plus the join bookkeeping physical-port mapping needs."""

    entities: list[Entity]
    lag_names: set[str]
    lag_interface_ids: set[str]
    membership: dict[str, str]
    emitted_keys: dict[str, str]

# lags.py:171 — signature
) -> LagResult:
# lags.py:215 — return
    return LagResult(entities, lag_names, lag_interface_ids, membership, emitted_keys)
```

`ports.py:77` (`lag_entities, lag_names, lag_interface_ids, membership, emitted_keys = _lag_entities(...)`) keeps working unchanged, and `.membership` becomes available where clearer. Apply the same to `_fanout_context` (`FanoutContext`) and `_port_entities` (`PortPhase`).

---

### N-07 · Severity **4** · Cryptic or under-specified helper names

**Category:** Naming — descriptiveness.

| Name | Location | Problem | Suggested |
|---|---|---|---|
| `_cfg` | `backend.py:84` | Abbreviation with no payoff; sits next to the fully-spelled `_cfg_or_env` | `_policy_value` / `_policy_or_env` |
| `_one` | `extract/retrieve.py:33` | Means "run one retrieve job"; reads as a number | `_run_job` |
| `_by_key` | `port_join.py:17` | By *which* key? (answer: `asset_interface_id`) | `_group_by_interface_id` |
| `_first_str` | `fabric.py:12` | Near-homonym of `_first_row` with different semantics — see N-12 | `_first_non_empty_field` |
| `predicate` | `ips.py:43` | Two-argument callable sharing a name with the one-argument `predicate` at `backend.py:118,164` | `matches` |
| `cs` | `correlate.py:58,64,110` | 2-char; `cs_device` is used elsewhere for the same value | `cs_device` |
| `ts` | `__main__.py:74` | 2-char | `timestamp` |
| `_record` | `tests/transform_helpers.py` | Shadows the `record` domain concept | `_make_record` |

`_by_key` is the highest-value rename: it is imported by four modules (`lags.py:10`, `physical_ports.py` via `port_join`, `ports.py:14`, `wireless.py:12`) and its docstring at `port_join.py:12-14` exists purely to answer the question the name should have answered.

```python
# port_join.py:12-23
def _interface_id_of(record: dict) -> str:
    """Join key across ConfigState port tables: the row's asset_interface_id."""
    return str(record.get("asset_interface_id") or "")


def _group_by_interface_id(records: list[dict]) -> dict[str, list[dict]]:
    """Group rows by asset_interface_id, dropping rows that carry none."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        interface_id = _interface_id_of(record)
        if interface_id:
            grouped[interface_id].append(record)
    return grouped
```

---

### N-08 · Severity **4** · Test helper names flip adjective position within one file

**Category:** Naming consistency.
**Location:** `tests/backend_helpers.py:32-63`

```python
def _mock_empty_clusters() -> None:                          # :32  adjective first
def _mock_empty_port_and_lag_tables(...) -> None:            # :39  adjective first
def _mock_empty_fabric_tables() -> None:                     # :51  adjective first
def _mock_interface_id_tables_empty() -> None:               # :57  adjective LAST
def _mock_port_tables_empty() -> None:                       # :63  adjective LAST
```

Five sibling helpers, two conventions, one file. Callers in `test_backend_run.py` must remember which spelling each uses; autocomplete on `_mock_empty` finds three of the five.

**Remediation** — standardize on adjective-first (matches the majority and reads as a phrase):

```python
def _mock_empty_interface_id_tables() -> None:   # was _mock_interface_id_tables_empty (:57)
def _mock_empty_port_tables() -> None:           # was _mock_port_tables_empty (:63)
```

Then update the call sites (`grep -rn '_mock_interface_id_tables_empty\|_mock_port_tables_empty' tests/`). Also spell out `_mock_cs` (`:27`) → `_mock_configstate` for consistency with N-02's glossary.

---

### N-09 · Severity **4** · Boolean positional arguments

**Category:** Function signature — boolean parameters.
**Location:** `pyproject.toml:131-133` disables `FBT001`, `FBT002`, `FBT003`

Four violations, all in the config path:

```python
def _env_bool(name: str, default: bool = False) -> bool:      # __main__.py:21   FBT001+FBT002
    ...
"BOOTSTRAP": _env_bool("BOOTSTRAP", False),                   # __main__.py:46   FBT003
if _cfg(config, "BOOTSTRAP", False):                          # backend.py:215   FBT003
```

At the two call sites the bare `False` is unreadable — `_cfg(config, "BOOTSTRAP", False)` gives no hint that the third positional is the default rather than, say, a strictness flag.

**Remediation** — make the parameter keyword-only. `_cfg`'s signature is fixed in N-03; extend it there:

```python
# __main__.py:21
def _env_bool(name: str, *, default: bool = False) -> bool:

# __main__.py:46
    "BOOTSTRAP": _env_bool("BOOTSTRAP", default=False),

# backend.py:84  (building on N-03)
def _cfg(config: object, key: str, default: object = None) -> object:
# backend.py:215
if _cfg(config, "BOOTSTRAP", default=False):
```

`default` is already keyword-only on `_cfg_or_env` (`backend.py:88`), so this also removes an inconsistency between the two sibling helpers. Then drop `FBT001`/`FBT002`/`FBT003` from `pyproject.toml:131-133` — with these four fixed the count goes to zero, and the rules start protecting new code.

---

### N-10 · Severity **3** · NetBox enum strings are inline while integer codes get named constants

**Category:** Magic strings — inconsistency.

`port_constants.py` carefully names the integer code maps (`VERIFIED_OPER_DUPLEX`, `VERIFIED_POE_CLASSIFICATION`, `OPER_STATE_UP`) and one string (`VIRTUAL_INTERFACE_TYPE = "virtual"`, line 20) — then the sibling NetBox enum values are written raw:

| Literal | Location | NetBox field |
|---|---|---|
| `"lag"` | `lags.py:143`, `physical_ports.py:180` | `Interface.type` |
| `"active"` / `"offline"` | `devices.py:43,45` | `Device.status` |
| `"active"` | `ips.py:137` | `IPAddress.status` |
| `"active"` / `"disabled"` | `wireless_auth.py:43,45` | `WirelessLAN.status` |
| `"pse"` | `physical_ports.py:28` | `Interface.poe_mode` |
| `"tagged"` / `"access"` | `vlans.py:79,81` | `Interface.mode` |
| `"ap"` | `wireless.py:85` | `Interface.rf_role` |
| `"open"`, `"auto"`, `"wep"` | `wireless_auth.py:19,21` | `WirelessLAN.auth_type` / `auth_cipher` |

The comment at `port_constants.py:16-19` explicitly describes the `"virtual"`/`"lag"` pair together — but only `"virtual"` got a constant.

**Remediation** — the minimum useful fix is the one already half-done:

```python
# port_constants.py:20-21
VIRTUAL_INTERFACE_TYPE = "virtual"
LAG_INTERFACE_TYPE = "lag"

# lags.py:143
    kwargs["type"] = LAG_INTERFACE_TYPE
# lags.py:211
            interface_type=LAG_INTERFACE_TYPE,
# physical_ports.py:180
            kwargs["lag"] = Interface(device=device, name=lag_parent, type=LAG_INTERFACE_TYPE)
```

The status/mode/auth literals appear once or twice each next to a docstring that explains the mapping, so naming them buys less; leaving those inline is defensible. `"lag"` is the one worth doing because it appears three times across two modules and must agree with `VIRTUAL_INTERFACE_TYPE`'s sibling usage.

---

### N-11 · Severity **3** · The band-matching expression is the least readable code in the package

**Category:** Complex boolean expression / magic strings.
**Location:** `wireless_rf.py:53-67`

```python
    normalized = _compact_token(band)
    if "6g" in normalized or normalized in {"6", "band6"}:
        offset = 5950.0
    elif (
        "2.4" in normalized
        or "2,4" in normalized
        or "24g" in normalized
        or normalized in {"2g", "band24", "band2.4"}
    ):
        offset = 2407.0
    elif "5g" in normalized or normalized in {"5", "band5"}:
        offset = 5000.0
    else:
        return None
    return offset + 5.0 * channel_number
```

A four-operand boolean mixing substring tests with set membership, three unnamed float offsets, and a two-line comment (`:52-53`) explaining what the matching is for. This is the one place where a comment is genuinely standing in for a name.

**Remediation** — a table makes the rule inspectable and the offsets self-naming:

```python
# wireless_rf.py — module level, replacing the inline chain
# IEEE 802.11 channel-center base frequencies (MHz): centre = base + 5 * channel.
_BAND_BASE_FREQUENCY_MHZ = {"6ghz": 5950.0, "2.4ghz": 2407.0, "5ghz": 5000.0}

# Band labels arrive as BAND_5_GHZ / "5 GHz" / "5g"; _compact_token collapses
# separators, so match on the surviving substrings and exact short forms.
_BAND_ALIASES: tuple[tuple[str, frozenset[str], str], ...] = (
    ("6g",  frozenset({"6", "band6"}),                   "6ghz"),
    ("24g", frozenset({"2g", "band24", "band2.4"}),      "2.4ghz"),
    ("5g",  frozenset({"5", "band5"}),                   "5ghz"),
)


def _band_key(normalized: str) -> str | None:
    """Resolve a compacted band label to a _BAND_BASE_FREQUENCY_MHZ key."""
    if "2.4" in normalized or "2,4" in normalized:
        return "2.4ghz"
    for needle, exact, key in _BAND_ALIASES:
        if needle in normalized or normalized in exact:
            return key
    return None
```

`_channel_frequency_mhz`'s body then reduces to:

```python
    key = _band_key(_compact_token(band))
    if key is None:
        return None
    return _BAND_BASE_FREQUENCY_MHZ[key] + 5.0 * channel_number
```

**Note the ordering constraint:** `"2.4"`/`"2,4"` must be tested before the `_BAND_ALIASES` loop, and `6g` before `5g`, to preserve the original precedence exactly. `tests/test_transform_wireless.py` covers the band cases — run it to confirm the refactor is behaviour-neutral.

---

### N-12 · Severity **3** · `_first_str` and `_first_row` are near-homonyms with different semantics

**Category:** Naming — collision.
**Location:** `fabric.py:12` vs `port_join.py:26`

```python
def _first_str(rows: list[dict], *keys: str) -> str | None:      # fabric.py:12
    """Return the first non-empty string for any of ``keys`` across rows."""

def _first_row(grouped: dict[str, list[dict]], key: str, *, table: str) -> dict:   # port_join.py:26
    """First row for a join key, or `{}` when the key is absent."""
```

`_first_str` iterates rows × keys and returns a *field value*; `_first_row` indexes a grouping by one key and returns a *row*. Different inputs, different outputs, four shared characters. `_first_str`'s callers read misleadingly — `fabric.py:46` `system_id = _first_str(configs, "sys_id")` looks like a row lookup.

**Remediation:**

```python
# fabric.py:12
def _first_non_empty_field(rows: list[dict], *field_names: str) -> str | None:
    """First non-empty string value for any of ``field_names``, scanning rows in order."""
    for row in rows:
        for field_name in field_names:
            value = row.get(field_name)
            if value is None or value == "":
                continue
            text = str(value).strip()
            if text:
                return text
    return None
```

Four call sites to update: `fabric.py:41,42,46,47`.

---

### N-13 · Severity **2** · `function` names an OS family, not a callable

**Category:** Domain terminology.
**Location:** `identity.py:40,45,50,62,73,100`, plus 16 pass-through sites

`function` is the Assets API's own field name (`Device.function`, values `"FABRIC ENGINE"`, `"SWITCH ENGINE"`, `"AP"`), so the code faithfully mirrors upstream — the right default. The cost is that in a Python file, `is_switch(function)` and `platform_for(function)` read as though a callable is being inspected, and `role_for(function)` compounds it.

**Verdict: do not rename.** Fidelity to the upstream field name is worth more than the momentary misreading, and a rename would break the correspondence between `asset.get("function")` and the parameter it feeds. Handle it with a glossary entry (§3.4) and a one-line clarification where the term is introduced:

```python
# identity.py:40-42
def is_switch(function: str | None) -> bool:
    """Whether an Assets ``Device.function`` value is a switch OS family.

    ``function`` is Platform ONE's term for the OS family string
    ("FABRIC ENGINE", "AP", ...) — not a Python callable.
    """
```

---

### N-14 · Severity **2** · 18 magic-value comparisons, three of them semantic

**Category:** Magic numbers.
**Location:** `pyproject.toml:115` disables `PLR2004`

Most of the 18 are self-evident and should stay inline — HTTP status arithmetic (`client.py:156,200,210`, `bootstrap.py:134`) and coordinate bounds (`devices.py:66,68`) read better as literals than as `_HTTP_REDIRECT_MIN`. Three carry meaning the number does not convey:

| Literal | Location | Means |
|---|---|---|
| `2` | `virtual_chassis.py:22,25` | Minimum distinct names required to form a chassis name |
| `4` | `ips.py:35` | IP address family version |
| `3` | `client.py:69` | Length of the `"..."` ellipsis in `truncate_error_body` |

**Remediation:**

```python
# virtual_chassis.py — module level
# A chassis name needs two distinct names so a shared placeholder ("Default")
# cannot collapse every chassis to one NetBox name.
_MIN_DISTINCT_NAMES = 2

# virtual_chassis.py:22, :25
    if len(peers) >= _MIN_DISTINCT_NAMES:
    ...
    if len(members) >= _MIN_DISTINCT_NAMES:

# ips.py:31-37
_IPV4_VERSION = 4

def _pick_primary_cidr(candidates: list[tuple[int, str]]) -> dict[str, str]:
    """Keep the first CIDR per address family from ranked candidates."""
    result: dict[str, str] = {}
    for version, cidr in candidates:
        field = "primary_ip4" if version == _IPV4_VERSION else "primary_ip6"
        result.setdefault(field, cidr)
    return result

# client.py:64-71
_ELLIPSIS = "..."

def truncate_error_body(text: str, *, limit: int = _ERROR_BODY_LIMIT) -> str:
    """Collapse whitespace and truncate an HTTP error body for safe logging."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    if limit <= len(_ELLIPSIS):
        return cleaned[:limit]
    return cleaned[: limit - len(_ELLIPSIS)] + _ELLIPSIS
```

Keep `PLR2004` suppressed globally, or scope it: `"src/orb_extreme_platformone/client.py" = ["PLR2004"]` for the HTTP arithmetic.

---

### N-15 · Severity **2** · Four functions exceed cyclomatic complexity 10

**Category:** Readability — function length/branching.
**Location:** `pyproject.toml:111-112` disables `C901` and `PLR0912`

| Complexity | Function | Location |
|---|---|---|
| **15** (+ 14 branches) | `radios_to_entities` | `wireless.py:118` |
| 13 | `primary_ips_from_tables` | `ips.py:47` |
| 12 | `extract_inferred_clusters` | `clusters.py:15` |
| 11 | `_vlan_fields` | `vlans.py:33` |

`radios_to_entities` is the outlier: 119 lines of body with a nested `for device_id → for key → for state_row` structure plus two near-identical SSID loops (`wireless.py:191-207` and `:208-219`), which differ only in whether `enabled=` is passed.

**Remediation** — extract the duplicated SSID pass; this alone drops complexity below the threshold:

```python
# wireless.py — new helper above radios_to_entities
def _absorb_ssid_rows(
    rows: list[dict],
    *,
    device_id: str,
    wlans: dict[str, dict],
    name_to_key: dict[str, str],
    ssids_by_radio: dict[tuple[str, str], list[str]],
    encryption_by_ssid: dict[str, object] | None = None,
    use_enabled: bool = False,
) -> None:
    """Merge one SSID table's rows into the WLAN map and radio→SSID links."""
    for row in rows:
        ssid = _ssid_name(row)
        if not ssid:
            continue
        encryption = (
            (encryption_by_ssid or {}).get(ssid) if encryption_by_ssid is not None
            else row.get("encryption")
        )
        _ensure_wlan(
            wlans, ssid,
            enabled=row.get("enabled") if use_enabled else None,
            encryption=encryption,
        )
        _link_ssid_radios(
            device_id=device_id, ssid=ssid, if_names=row.get("if_names"),
            name_to_key=name_to_key, ssids_by_radio=ssids_by_radio,
        )
```

`wireless.py:191-219` then collapses to two calls. **Caveat:** `use_enabled` is exactly the boolean parameter N-09 argues against — prefer splitting into `_absorb_ssid_config_rows` / `_absorb_ssid_state_rows` sharing a private core, or accept the flag as keyword-only and document it. `tests/test_transform_wireless.py` (351 lines) covers this path; re-run it to confirm the refactor is behaviour-neutral.

Leave `C901` suppressed until these four are addressed, then re-enable.

---

## 3. Naming convention guide

Derived from the conventions the codebase already follows — this documents the house style rather than imposing a new one. Suggested home: `docs/naming-conventions.md`, referenced from `CONTRIBUTING.md`.

### 3.1 Casing and visibility

| Kind | Convention | Example in repo |
|---|---|---|
| Module | `snake_case`, singular noun for a concept, plural for a collection | `identity.py`, `tables.py`, `physical_ports.py` |
| Class | `PascalCase` noun phrase | `PlatformOneClient`, `PlatformOneApiError` |
| Function / method | `snake_case` | `correlated_records`, `ports_to_entities` |
| Variable / parameter | `snake_case` | `cs_device_id`, `failed_tables` |
| Module constant | `UPPER_SNAKE_CASE` | `PORT_TABLES`, `VERIFIED_OPER_DUPLEX` |
| Module-private | leading `_` | `_ERROR_BODY_LIMIT`, `_by_key` |
| Type alias | `PascalCase` | `TableCatalog` (`retrieve.py:17`), `LagRow` (`lags.py:69`) |

Enforced by `ruff select = ["ALL"]` (rules `N`, `A`). Currently zero violations — keep it that way.

### 3.2 Function name patterns

These suffix/prefix patterns are already consistent across the tree. **Follow them; they are the codebase's strongest naming asset.**

| Pattern | Contract | Examples |
|---|---|---|
| `<x>_to_entities` | Maps domain rows → `list[Entity]` | `devices_to_entities`, `ports_to_entities`, `radios_to_entities`, `virtual_chassis_to_entities` |
| `_<x>_entities` | Private entity builder returning `list[Entity]` | `_lag_entities`, `_physical_port_entities`, `_orphan_ip_entities` |
| `_<x>_kwargs` | Builds a `dict` of Diode constructor kwargs — never emits | `_port_kwargs`, `_lag_kwargs`, `_site_kwargs`, `_wlan_kwargs` |
| `extract_<x>` | Performs I/O against Platform ONE, returns raw rows | `extract_port_tables`, `extract_wireless_tables`, `extract_inferred_clusters` |
| `<x>_by_<y>` | Returns a `dict` keyed by `<y>` | `_capabilities_by_port`, `_interface_names_by_id`, `records_by_cs_id` |
| `is_<x>` / `_is_<x>` | Returns `bool`, no side effects | `is_switch`, `is_ap`, `_is_extreme_reserved_vlan` |
| `_coerce_<type>` | Parse-or-`None`; never raises, never invents | `_coerce_int`, `_coerce_bool` |
| `<x>_for(...)` | Derives one `<x>` from an input, `None` when unknown | `role_for`, `platform_for`, `_status_for`, `_vlan_records_for` |
| `require_<x>` | Validates and returns, or raises `ValueError` | `require_https_url` |
| `ensure_<x>` | Idempotent side effect, returns `None` | `ensure_schema`, `_ensure_all`, `_ensure_wlan` |
| `_first_<x>` | First match or empty sentinel | `_first_row` (→ `_first_non_empty_field` per N-12) |

**New rule (from N-01/N-07):** a `_by_key`-style name must say *which* key — `_group_by_interface_id`, not `_by_key`.

### 3.3 Constant name patterns

| Prefix | Meaning | Examples |
|---|---|---|
| `CF_<NAME>` | NetBox custom-field name | `CF_DEVICE_ID`, `CF_ISIS_AREA` |
| `VERIFIED_<X>` | Vendor code → NetBox value map, **only** for codes confirmed against real hardware | `VERIFIED_OPER_DUPLEX`, `VERIFIED_POE_CLASSIFICATION` |
| `<X>_TABLES` | ConfigState catalog: `transform_key → (retrieve-table, filter_field)` | `PORT_TABLES`, `WIRELESS_TABLES`, `FABRIC_DEVICE_TABLES` |
| `DEFAULT_<X>` | Fallback used when config omits a value | `DEFAULT_BASE_URL`, `DEFAULT_CLASSIFICATION` |
| `_<X>_LIMIT` / `_<X>_SIZE` | Pagination / truncation bound | `_ERROR_BODY_LIMIT`, `CONFIGSTATE_PAGE_SIZE` |

The `VERIFIED_` prefix is a genuinely good invention — it encodes the project's core discipline (never assert an unverified mapping) directly in the identifier. Keep using it for any new vendor-code table.

### 3.4 Domain glossary (resolves N-02, N-13)

This is the highest-value section of the guide. **Every term below is currently ambiguous somewhere in the tree.**

| Term | Means | Type | Never confuse with |
|---|---|---|---|
| **asset** | An Assets-API `Device` row (the `record["asset"]` dict) | `dict` | ConfigState `AssetDevice` |
| **`device_id`** | Assets-API device id → `CF_DEVICE_ID` | `int` \| `str` | any ConfigState UUID |
| **`cs_device_id`** | ConfigState `AssetDevice.id` UUID | `str` (UUID) | Assets `device_id` |
| **`asset_device_id`** | The **same** ConfigState UUID, spelled as ConfigState's *filter field name* | `str` (UUID) | Assets `device_id` — despite the "asset" prefix |
| **`asset_interface_id`** | ConfigState interface UUID; the join key across all port tables | `str` (UUID) | port `name` |
| **inferred device** | ConfigState `InferredDevice.id` — a *third* id space, remapped to `cs_device_id` in `clusters.py:31-36` | `str` (UUID) | both of the above |
| **`function`** | Assets `Device.function` — the **OS family** string (`"FABRIC ENGINE"`, `"AP"`) | `str` | a Python callable |
| **`classification`** | Assets device-class filter (`ALL`, `SWITCH`, `WIRELESS`) | `str` | `function` |
| **table key** | Catalog key into `PORT_TABLES` etc. (`"port_configs"`) | `str` | `asset_interface_id` |
| **record** | The joined `{asset, cs_device_id, cs_device, location}` dict | `dict` | an Assets row alone |

**Rules:**
1. A local variable holding a ConfigState UUID is named `cs_device_id` / `cs_device_ids` — never bare `device_id` / `device_ids`.
2. A variable holding an Assets device id is named `asset_device_id`… **no** — that spelling is taken by ConfigState. Use `assets_device_id`, or read it inline as `asset["device_id"]`.
3. When a dict key comes from an upstream API, keep the API's spelling in the *lookup* (`row.get("asset_device_id")`) but name the *variable* by the glossary (`cs_device_id = row.get("asset_device_id")`).

### 3.5 Signature rules

1. **Max 5 positional-or-keyword parameters.** Beyond that, make them keyword-only; beyond 7 total, bundle into a frozen dataclass (N-05).
2. **No boolean positional parameters** — always keyword-only with an explicit name at the call site (N-09).
3. **Annotate every callable parameter** with its full `Callable[[...], ...]` signature (N-03).
4. **Return ≥ 3 values as a `NamedTuple`**, not a bare tuple (N-06). Two values may stay a plain tuple when the types differ.
5. **Optional parameters use `None` as the sentinel**, never a mutable default. The codebase is already clean on this — `poe_state: dict | None = None` (`physical_ports.py:63`), normalized with `or {}` at use.
6. **Annotate concrete SDK types, not `object`** — import under `TYPE_CHECKING` if a runtime import is undesirable (N-04).

### 3.6 Comment and docstring rules

Current state is healthy (0.39 comment+doc : code; only 1 function where docs exceed body). Keep it by following what the codebase already does:

1. **Comment the *why*, especially the negative.** The most valuable comments here record why a field is *not* asserted — `port_constants.py:29`, `lags.py:29-32`, `vlans.py:41-45`. Never delete these to "reduce comments".
2. **Record provenance for every vendor-code mapping.** Any `VERIFIED_*` entry states where the code was confirmed (`port_constants.py:24-31`).
3. **A comment that explains what a variable *is* means the variable is misnamed.** Rename instead — see `physical_ports.py:161` and `lags.py:191` (both say "`key` is asset_interface_id"), fixed by N-01.
4. **Public functions need a docstring; private helpers need one when the contract is not obvious from the name.** `D103`/`D102` are currently suppressed (`pyproject.toml:126-127`) but only 3 functions actually violate them — close the gap and re-enable.
5. **Docstring first line: imperative mood, one line, then a blank line.** Two `D401` and three `D205` violations exist (`backend.py:171`, `vlans.py:12`, `identity.py:180`, `devices.py:188`, `ips.py:153`).

### 3.7 Test naming

| Kind | Convention | Example |
|---|---|---|
| Test function | `test_<unit>_<behaviour>` | `test_run_sets_device_primary_ip_from_configstate_interface_cidr` |
| Fixture | noun, no `_` prefix | `assets_spec`, `stub_sdk` |
| Private helper | `_<verb>_<noun>`, adjective **before** the noun | `_mock_empty_fabric_tables` |
| Payload constant | `UPPER_SNAKE_CASE` naming the shape | `SWITCH_ASSET`, `PORT_CONFIG`, `VLAN_PROPERTIES` |

Fix the two adjective-last outliers per N-08.

---

## 4. Suggested sequencing

| Order | Findings | Effort | Rationale |
|---|---|---|---|
| 1 | **N-04**, **N-09**, **N-12**, **N-14** | Small | Mechanical, no behaviour change, immediately unblocks re-enabling `FBT*` |
| 2 | **N-01**, **N-07** | Medium | Pure renames; highest readability return in the package |
| 3 | **N-02** + glossary (§3.4) | Medium | Do with N-01 — same files, same review |
| 4 | **N-03**, **N-06** | Medium | Annotations and `NamedTuple`s; both make N-05 safer |
| 5 | **N-05**, **N-15**, **N-11** | Larger | Structural; each needs its test module re-run to prove behaviour-neutrality |
| 6 | **N-08**, **N-10**, **N-13** | Small | Consistency polish; can ride along with any of the above |

Publish §3 as `docs/naming-conventions.md` first — it is useful before any code changes and gives the renames in steps 2–3 a citable rationale.

---

## 5. Unable to verify

| Claim | Why | What would prove it |
|---|---|---|
| Whether `_quote_values` (`__main__.py:56`) was intended to produce all-string JSON output for a downstream consumer | The function is never called (see architecture audit F-01); its intended output format has no consumer in-repo | The original dry-run consumer, or a fixture/golden file showing the expected shape |
| Whether `function` values are always upper-case from the Assets API | Every comparison defensively calls `.upper()` (`identity.py:42,47,57,70,115`), implying uncertainty; the fixture `conftest.py:19` uses title-case `"Fabric Engine"` while `PLATFORM_BY_FUNCTION` keys (`identity.py:15-20`) are upper-case | The Assets API OpenAPI enum for `Device.function`, or a recorded production response |
| Whether the `_mock_*` helper split reflects an intentional distinction I am reading as inconsistency | Both spellings mock empty tables; no docstrings on `backend_helpers.py:57,63` | A comment or commit message explaining the split — `git log -p tests/backend_helpers.py` |
| Whether `2` at `virtual_chassis.py:22` could ever be a value other than 2 (e.g. clusters with >2 members) | `InferredCluster` exposes exactly `device_one_id` / `device_two_id` (`tables.py:46-50`), so 2 is structural — but whether Platform ONE models larger stacks elsewhere is unknown | The ConfigState `InferredCluster` schema, or a stack of >2 units in a production dry run |
