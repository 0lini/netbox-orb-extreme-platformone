"""One-time idempotent NetBox schema setup: custom fields + provenance tags.

Uses the NetBox REST API directly (not Diode) because field definitions are
schema, not data. Skips gracefully if no NetBox credentials are configured.

Failures surface as ``NetBoxApiError`` rather than a raw ``requests``
exception, so the package presents one error taxonomy: this mirrors
``PlatformOneApiError`` on the Platform ONE side.
"""

from __future__ import annotations

import requests

from .http import truncate_error_body
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

CUSTOM_FIELDS = [
    {
        "name": CF_DEVICE_ID,
        "label": "Platform ONE Device ID",
        "type": "text",
        "object_types": ["dcim.device"],
        "description": (
            "Immutable Extreme Platform ONE device id (Assets API device_id); "
            "stable correlation key even if the device is renamed."
        ),
        "filter_logic": "exact",
        "unique": True,
    },
    {
        "name": CF_INTERFACE_ID,
        "label": "Platform ONE Interface ID",
        "type": "text",
        "object_types": ["dcim.interface"],
        "description": (
            "Immutable Extreme Platform ONE interface UUID "
            "(ConfigState asset_interface_id); stable correlation key even if "
            "the port is renamed."
        ),
        "filter_logic": "exact",
        "unique": True,
    },
    {
        "name": CF_CLUSTER_ID,
        "label": "Platform ONE Cluster ID",
        "type": "text",
        "object_types": ["dcim.virtualchassis"],
        "description": (
            "Immutable Extreme Platform ONE InferredCluster UUID "
            "(ConfigState retrieve-inferred-cluster id); stable correlation "
            "key even if peer names change."
        ),
        "filter_logic": "exact",
        "unique": True,
    },
    {
        "name": CF_ISIS_AREA,
        "label": "Platform ONE ISIS Area",
        "type": "text",
        "object_types": ["dcim.device"],
        "description": (
            "ISIS area address from ConfigState "
            "(manual_area_address, else area_name, else learned/default area)."
        ),
        "filter_logic": "exact",
        "unique": False,
    },
    {
        "name": CF_ISIS_SYSTEM_ID,
        "label": "Platform ONE ISIS System ID",
        "type": "text",
        "object_types": ["dcim.device"],
        "description": ("ISIS system id (sys_id) from ConfigState retrieve-asset-isis-global-config."),
        "filter_logic": "exact",
        "unique": False,
    },
    {
        "name": CF_SPBM_NICKNAME,
        "label": "Platform ONE SPBM Nickname",
        "type": "text",
        "object_types": ["dcim.device"],
        "description": (
            "SPBM node nickname from ConfigState retrieve-asset-spbm-instance "
            "(node_nick_name), falling back to ISIS area_vnode_nickname."
        ),
        "filter_logic": "exact",
        "unique": False,
    },
]

TAGS = [
    {
        "name": TAG_NAMES[0],
        "slug": TAG_NAMES[0],
        # Extreme Networks brand primary purple (#440099).
        "color": "440099",
        "description": "Objects synced from Extreme Networks via netbox-orb-extreme-platformone.",
    },
    {
        "name": TAG_NAMES[1],
        "slug": TAG_NAMES[1],
        # Same Extreme brand purple as extreme-networks (#440099).
        "color": "440099",
        "description": "Objects synced from Extreme Platform ONE via netbox-orb-extreme-platformone.",
    },
    {
        "name": TAG_NAMES[2],
        "slug": TAG_NAMES[2],
        # Neutral gray — provenance marker, not brand-colored.
        "color": "9e9e9e",
        "description": "Objects created by automated discovery rather than manually.",
    },
]


_HTTP_REDIRECT_MIN = 300
_HTTP_CLIENT_ERROR_MIN = 400
_REQUEST_TIMEOUT_SECONDS = 30
_AUTH_FAILURE_STATUSES = (401, 403)


class NetBoxApiError(RuntimeError):
    """Raised on a failed NetBox REST call during schema bootstrap.

    Mirrors ``PlatformOneApiError`` so callers handle one error shape across
    both upstreams. ``status_code`` is ``None`` for transport failures.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_auth_failure(self) -> bool:
        """NetBox token is wrong or lacks permission — retrying will not help."""
        return self.status_code in _AUTH_FAILURE_STATUSES


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
        raise NetBoxApiError(msg) from exc
    if _HTTP_REDIRECT_MIN <= resp.status_code < _HTTP_CLIENT_ERROR_MIN:
        msg = f"NetBox unexpected redirect {resp.status_code} for {url}"
        raise NetBoxApiError(msg, status_code=resp.status_code)
    if resp.status_code >= _HTTP_CLIENT_ERROR_MIN:
        detail = truncate_error_body(resp.text)
        msg = f"NetBox API error {resp.status_code} for {url}: {detail}"
        raise NetBoxApiError(msg, status_code=resp.status_code)
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
