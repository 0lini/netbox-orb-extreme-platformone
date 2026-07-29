# Domain glossary

Naming and layout follow ordinary Python conventions, enforced by `ruff`
(`select = ["ALL"]`). What a linter cannot catch is this: **the two upstream
APIs each have a device identifier, they are different things, and the obvious
names point the wrong way.**

| Term | Means | Type |
|---|---|---|
| **asset** | An Assets-API `Device` row (`DeviceRecord.asset`) | `dict` |
| **`device_id`** (Assets row field) | Assets-API device id → `CF_DEVICE_ID` | `int` \| `str` |
| **`cs_device_id`** | ConfigState `AssetDevice.id` UUID | `str` |
| **`asset_device_id`** | The **same** ConfigState UUID, spelled as ConfigState's *filter field name* — despite the "asset" prefix | `str` |
| **`asset_interface_id`** | ConfigState interface UUID; the join key across all port tables | `str` |
| **inferred device** | ConfigState `InferredDevice.id` — a *third* id space, remapped to `cs_device_id` in `extract/clusters.py` | `str` |
| **`function`** | Assets `Device.function` — the **OS family** string (`"FABRIC ENGINE"`, `"AP"`), not a Python callable | `str` |
| **`classification`** | Assets device-class filter (`ALL`, `SWITCH`, `WIRELESS`) | `str` |

Two rules follow:

1. A variable holding a ConfigState UUID is `cs_device_id` / `cs_device_ids`,
   never bare `device_id` / `device_ids`.
2. Keep the API's spelling in the lookup and the glossary's in the variable:
   `cs_device_id = row.get("asset_device_id")`.

`VERIFIED_*` constants (`transform/port_constants.py`) map vendor integer codes
to NetBox values, and hold **only** codes confirmed against real hardware. The
prefix is the project's core discipline — never assert an unverified mapping —
so keep it for any new code table and note where the codes were confirmed.
