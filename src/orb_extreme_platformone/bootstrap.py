"""One-time idempotent NetBox schema setup: custom fields + provenance tags.

Uses the NetBox REST API directly (not Diode) because field definitions are
schema, not data. Skips gracefully if no NetBox credentials are configured.

Failures surface as ``NetBoxApiError`` rather than a raw ``requests``
exception, so the package presents one error taxonomy: this mirrors
``PlatformOneApiError`` on the Platform ONE side.
"""

from __future__ import annotations

import requests

from .http import ApiError, raise_for_response
from .schema import (
    CF_CLUSTER_ID,
    CF_DEVICE_ID,
    CF_INTERFACE_ID,
    CF_ISIS_AREA,
    CF_ISIS_SYSTEM_ID,
    CF_SPBM_NICKNAME,
    TAG_NAMES,
)
from .urls import require_https_url

# Correlation-key and tag names live in `schema` so the pure transform layer
# can stamp them onto entities without importing this HTTP module. The
# definitions below (types, descriptions, `unique` enforcement) stay here:
# they are NetBox schema, and only bootstrap writes them.
#
# `unique` is enforced (NetBox >= 3.7) on the per-object-type correlation
# keys: two NetBox objects of the same type claiming the same Platform ONE id
# is always a sync defect worth failing loudly on. The ConfigState AssetDevice
# UUID stays an internal join key (re-correlated by serial every tick) and is
# not stored on Device.


def _custom_field(
    name: str,
    label: str,
    object_type: str,
    description: str,
    *,
    unique: bool,
) -> dict:
    """One NetBox custom-field definition; every field this worker needs is text."""
    return {
        "name": name,
        "label": label,
        "type": "text",
        "object_types": [object_type],
        "description": description,
        "filter_logic": "exact",
        "unique": unique,
    }


CUSTOM_FIELDS = [
    _custom_field(
        CF_DEVICE_ID,
        "Platform ONE Device ID",
        "dcim.device",
        "Immutable Extreme Platform ONE device id (Assets API device_id); "
        "stable correlation key even if the device is renamed.",
        unique=True,
    ),
    _custom_field(
        CF_INTERFACE_ID,
        "Platform ONE Interface ID",
        "dcim.interface",
        "Immutable Extreme Platform ONE interface UUID (ConfigState "
        "asset_interface_id); stable correlation key even if the port is renamed.",
        unique=True,
    ),
    _custom_field(
        CF_CLUSTER_ID,
        "Platform ONE Cluster ID",
        "dcim.virtualchassis",
        "Immutable Extreme Platform ONE InferredCluster UUID (ConfigState "
        "retrieve-inferred-cluster id); stable correlation key even if peer "
        "names change.",
        unique=True,
    ),
    _custom_field(
        CF_ISIS_AREA,
        "Platform ONE ISIS Area",
        "dcim.device",
        "ISIS area address from ConfigState "
        "(manual_area_address, else area_name, else learned/default area).",
        unique=False,
    ),
    _custom_field(
        CF_ISIS_SYSTEM_ID,
        "Platform ONE ISIS System ID",
        "dcim.device",
        "ISIS system id (sys_id) from ConfigState retrieve-asset-isis-global-config.",
        unique=False,
    ),
    _custom_field(
        CF_SPBM_NICKNAME,
        "Platform ONE SPBM Nickname",
        "dcim.device",
        "SPBM node nickname from ConfigState retrieve-asset-spbm-instance "
        "(node_nick_name), falling back to ISIS area_vnode_nickname.",
        unique=False,
    ),
]

# Extreme Networks brand primary purple; `discovered` is neutral grey because
# it marks provenance rather than a vendor.
_EXTREME_PURPLE = "440099"

TAGS = [
    {
        "name": TAG_NAMES[0],
        "slug": TAG_NAMES[0],
        "color": _EXTREME_PURPLE,
        "description": "Objects synced from Extreme Networks via netbox-orb-extreme-platformone.",
    },
    {
        "name": TAG_NAMES[1],
        "slug": TAG_NAMES[1],
        "color": _EXTREME_PURPLE,
        "description": "Objects synced from Extreme Platform ONE via netbox-orb-extreme-platformone.",
    },
    {
        "name": TAG_NAMES[2],
        "slug": TAG_NAMES[2],
        "color": "9e9e9e",
        "description": "Objects created by automated discovery rather than manually.",
    },
]


_REQUEST_TIMEOUT_SECONDS = 30


class NetBoxApiError(ApiError):
    """Raised on a failed NetBox REST call during schema bootstrap."""

    upstream = "NetBox"


def _headers(token: str) -> dict:
    return {"Authorization": f"Token {token}", "Content-Type": "application/json"}


def _request(method: str, url: str, token: str, **kwargs) -> requests.Response:
    """NetBox REST call that never follows redirects (token must not leave origin)."""
    kwargs.setdefault("timeout", _REQUEST_TIMEOUT_SECONDS)
    kwargs.setdefault("allow_redirects", False)
    try:
        resp = requests.request(method, url, headers=_headers(token), **kwargs)
    except requests.RequestException as exc:
        msg = f"NetBox request failed for {url}: {exc}"
        raise NetBoxApiError(msg, path=url) from exc
    raise_for_response(resp, path=url, error=NetBoxApiError)
    return resp


def _lookup(url: str, token: str, name: str) -> dict | None:
    resp = _request("GET", url, token, params={"name": name})
    results = resp.json().get("results") or []
    return results[0] if results else None


def _ensure_all(url: str, token: str, definitions: list[dict]) -> None:
    """Create missing definitions; align `unique` on existing ones.

    Only `unique` is reconciled on existing records: it is the one flag with
    enforcement semantics, and pre-uniqueness bootstraps must pick it up.
    Everything else (label, description, ...) is left to manual edits.
    """
    for definition in definitions:
        existing = _lookup(url, token, definition["name"])
        if existing is None:
            _request("POST", url, token, json=definition)
            continue
        desired_unique = definition.get("unique")
        if desired_unique is not None and existing.get("unique") != desired_unique:
            _request(
                "PATCH",
                f"{url}{existing['id']}/",
                token,
                json={"unique": desired_unique},
            )


def ensure_schema(netbox_url: str | None, netbox_token: str | None) -> None:
    """Idempotently create the custom-field definitions and provenance tags.

    When either URL or token is missing the call is a no-op so scheduled
    runs without bootstrap credentials stay quiet. Callers that set
    ``BOOTSTRAP: true`` should fail closed before invoking this (see
    ``backend.Backend.run``).
    """
    if not netbox_url or not netbox_token:
        return
    base = require_https_url(netbox_url, what="NETBOX_API_URL")
    _ensure_all(f"{base}/api/extras/custom-fields/", netbox_token, CUSTOM_FIELDS)
    _ensure_all(f"{base}/api/extras/tags/", netbox_token, TAGS)
