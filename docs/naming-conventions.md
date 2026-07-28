# Naming conventions

House style for `netbox-orb-extreme-platformone`. This documents the
conventions the codebase already follows rather than imposing new ones — the
function-name patterns in §2 are its strongest naming asset, and new code
should extend them rather than invent alternatives.

Casing, builtin shadowing and import order are enforced by `ruff`
(`select = ["ALL"]`), so this file covers what a linter cannot check: which
*word* to pick.

---

## 1. Casing and visibility

| Kind | Convention | Example |
|---|---|---|
| Module | `snake_case`; singular for a concept, plural for a collection | `identity.py`, `catalog.py`, `physical_ports.py` |
| Class | `PascalCase` noun phrase | `PlatformOneClient`, `PlatformOneTransport`, `PlatformOneApiError` |
| Function / method | `snake_case` | `correlated_records`, `ports_to_entities` |
| Variable / parameter | `snake_case` | `cs_device_id`, `failed_tables` |
| Module constant | `UPPER_SNAKE_CASE` | `PORT_TABLES`, `VERIFIED_OPER_DUPLEX` |
| Module-private | leading `_` | `_ERROR_BODY_LIMIT`, `_group_by_interface_id` |
| Type alias / NamedTuple | `PascalCase` | `TableCatalog`, `LagResult`, `RetrieveResult` |

---

## 2. Function name patterns

| Pattern | Contract | Examples |
|---|---|---|
| `<x>_to_entities` | Maps domain rows → `list[Entity]` | `devices_to_entities`, `ports_to_entities`, `radios_to_entities` |
| `_<x>_entities` | Private entity builder returning `list[Entity]` | `_lag_entities`, `_physical_port_entities`, `_orphan_ip_entities` |
| `_<x>_kwargs` | Builds a `dict` of Diode constructor kwargs — never emits | `_port_kwargs`, `_lag_kwargs`, `_site_kwargs`, `_wlan_kwargs` |
| `extract_<x>` | Performs I/O against Platform ONE, returns raw rows | `extract_port_tables`, `extract_inferred_clusters` |
| `_group_by_<y>` | Returns a `dict` grouping rows by `<y>` | `_group_by_interface_id` |
| `<x>_by_<y>` | Returns a `dict` keyed by `<y>` | `_capabilities_by_port`, `_interface_names_by_id` |
| `is_<x>` / `_is_<x>` | Returns `bool`, no side effects | `is_switch`, `is_ap`, `_is_extreme_reserved_vlan` |
| `_coerce_<type>` | Parse-or-`None`; never raises, never invents | `_coerce_int`, `_coerce_bool` |
| `<x>_for(...)` | Derives one `<x>`, `None` when unknown | `role_for`, `platform_for`, `_status_for` |
| `require_<x>` | Validates and returns, or raises | `require_https_url` |
| `ensure_<x>` | Idempotent side effect, returns `None` | `ensure_schema`, `_ensure_wlan` |
| `_first_<x>` | First match or empty sentinel | `_first_row`, `_first_non_empty_field` |

**A grouping name must say which key.** `_by_key` does not; `_group_by_interface_id`
does. If a helper needs a docstring to explain what its key is, the name is wrong.

---

## 3. Constant patterns

| Prefix | Meaning | Examples |
|---|---|---|
| `CF_<NAME>` | NetBox custom-field name (defined in `schema.py`) | `CF_DEVICE_ID`, `CF_ISIS_AREA` |
| `VERIFIED_<X>` | Vendor code → NetBox value map, **only** for codes confirmed against real hardware | `VERIFIED_OPER_DUPLEX`, `VERIFIED_POE_CLASSIFICATION` |
| `<X>_TABLES` | ConfigState catalog: `transform_key → (retrieve-table, filter_field)` | `PORT_TABLES`, `WIRELESS_TABLES` |
| `DEFAULT_<X>` | Fallback when config omits a value | `DEFAULT_BASE_URL`, `DEFAULT_TIMEOUT_SECONDS` |
| `RETRY_<X>` | Retry policy parameter | `RETRY_TOTAL`, `RETRY_BACKOFF_FACTOR` |
| `_<X>_LIMIT` / `_<X>_SIZE` | Pagination or truncation bound | `_ERROR_BODY_LIMIT`, `CONFIGSTATE_PAGE_SIZE` |

The `VERIFIED_` prefix encodes the project's core discipline — never assert an
unverified mapping — directly in the identifier. Use it for any new vendor-code
table, and comment where the codes were confirmed.

---

## 4. Domain glossary

**Read this before naming anything that holds an identifier.** The two upstream
APIs each have a device id, they are different types, and the obvious names
point the wrong way.

| Term | Means | Type | Never confuse with |
|---|---|---|---|
| **asset** | An Assets-API `Device` row (`record["asset"]`) | `dict` | ConfigState `AssetDevice` |
| **`device_id`** (Assets row field) | Assets-API device id → `CF_DEVICE_ID` | `int` \| `str` | any ConfigState UUID |
| **`cs_device_id`** | ConfigState `AssetDevice.id` UUID | `str` | Assets `device_id` |
| **`asset_device_id`** | The **same** ConfigState UUID, spelled as ConfigState's *filter field name* | `str` | Assets `device_id` — despite the "asset" prefix |
| **`asset_interface_id`** | ConfigState interface UUID; the join key across all port tables | `str` | port `name` |
| **inferred device** | ConfigState `InferredDevice.id` — a *third* id space, remapped to `cs_device_id` in `extract/clusters.py` | `str` | both of the above |
| **`function`** | Assets `Device.function` — the **OS family** string (`"FABRIC ENGINE"`, `"AP"`) | `str` | a Python callable |
| **`classification`** | Assets device-class filter (`ALL`, `SWITCH`, `WIRELESS`) | `str` | `function` |
| **table key** | Catalog key into `PORT_TABLES` etc. (`"port_configs"`) | `str` | `asset_interface_id` |
| **record** | The joined `{asset, cs_device_id, cs_device, location}` dict | `dict` | an Assets row alone |

**Rules:**

1. A local or parameter holding a ConfigState UUID is `cs_device_id` /
   `cs_device_ids` — never bare `device_id` / `device_ids`.
2. Keep the API's spelling in the *lookup* but the glossary's in the *variable*:
   `cs_device_id = row.get("asset_device_id")`.
3. `function` stays as-is. It mirrors the upstream field name, and fidelity beats
   the momentary misreading of `is_switch(function)`.

---

## 5. Signature rules

1. **Max 5 positional-or-keyword parameters.** Beyond that make them
   keyword-only; beyond 7, bundle into a frozen dataclass.
2. **No boolean positional parameters.** Keyword-only, named at the call site.
3. **Annotate every callable parameter** with its full `Callable[[...], ...]`
   signature. Two parameters named `predicate` with different arities is how
   this codebase got confused before.
4. **Return three or more values as a `NamedTuple`.** `LagResult` exists because
   its positions 2/3 are both `set[str]` and 4/5 both `dict[str, str]` —
   transposing either pair type-checks cleanly and corrupts LAG membership.
5. **`None` is the optional sentinel**, never a mutable default. Normalize with
   `or {}` at use.
6. **Annotate concrete SDK types, not `object`.** Import under `TYPE_CHECKING`
   if a runtime import is unwanted.

---

## 6. Comments and docstrings

Current ratio is roughly 0.4 comment+doc lines per code line, which is healthy
for a package mapping a vendor API whose OpenAPI specs sit behind a login wall.

1. **Comment the *why*, especially the negative.** The most valuable comments
   here record why a field is *not* asserted — `port_constants.py`,
   `lags.py:_lag_admin_enabled`, `vlans.py:_vlan_fields`. Do not delete these
   to "reduce comments".
2. **Record provenance for every vendor-code mapping.** State where the code was
   confirmed.
3. **A comment explaining what a variable *is* means the variable is misnamed.**
   Rename it. Two `# `key` is asset_interface_id` comments were deleted exactly
   this way.
4. **First line: imperative mood, one line, then a blank line.** Enforced by
   `D205`/`D401`.
5. Public functions and methods need a docstring (`D102`/`D103` are on).
   `__init__` is exempt — document the contract on the class.

---

## 7. Logging

1. Use `logging_context.get_logger(__name__)`, never `logging.getLogger`. A
   `logging.Filter` on a parent logger is **not** applied to records from its
   children, so each logger needs the policy-context filter itself.
2. Do not thread `policy_name` into pure helpers. The contextvar carries it;
   operators format it with `%(policy)s`.
3. **WARNING** = the tick survives and degraded. **ERROR** = the tick is over.
   Keep that distinction — an alert on `level >= ERROR` depends on it.

---

## 8. Tests

| Kind | Convention | Example |
|---|---|---|
| Test function | `test_<unit>_<behaviour>` | `test_run_sets_device_primary_ip_from_configstate_interface_cidr` |
| Fixture | noun, no `_` prefix | `stub_sdk` |
| Private helper | `_<verb>_<noun>`, adjective **before** the noun | `_mock_empty_fabric_tables` |
| Payload constant | `UPPER_SNAKE_CASE` naming the shape | `SWITCH_ASSET`, `PORT_CONFIG` |

Spell out abbreviations in helper names (`_mock_configstate`, not `_mock_cs`) so
they match the glossary.
