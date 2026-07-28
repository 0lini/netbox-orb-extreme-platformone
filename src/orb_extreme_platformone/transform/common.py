"""Shared transform constants and small helpers."""

from __future__ import annotations

import ipaddress
import logging

from netboxlabs.diode.sdk.ingester import (
    CustomFieldValue,
    Device,
    DeviceRole,
    DeviceType,
    Site,
)

from orb_extreme_platformone.identity import device_type_model_for, role_for
from orb_extreme_platformone.schema import (
    CF_CLUSTER_ID,
    CF_DEVICE_ID,
    CF_INTERFACE_ID,
    CF_ISIS_AREA,
    CF_ISIS_SYSTEM_ID,
    CF_SPBM_NICKNAME,
    MANUFACTURER,
    TAG_NAMES,
)

logger = logging.getLogger("orb_extreme_platformone.transform")

PROVENANCE_TAGS = list(TAG_NAMES)

__all__ = [
    "CF_CLUSTER_ID",
    "CF_DEVICE_ID",
    "CF_INTERFACE_ID",
    "CF_ISIS_AREA",
    "CF_ISIS_SYSTEM_ID",
    "CF_SPBM_NICKNAME",
    "MANUFACTURER",
    "PROVENANCE_TAGS",
    "logger",
]


def _cf_text(value: str) -> CustomFieldValue:
    return CustomFieldValue(text=value)


def _device_ref(
    *,
    name: str,
    site_name: str | None = None,
    function: str | None = None,
    product_type: str | None = None,
) -> Device:
    """Nested Device stub for Interface / IPAddress / VirtualChassis.master refs.

    Diode's generate-diff validates nested ``dcim.device`` against NetBox
    required fields (site, role, device_type) even when the device already
    exists. Name-only stubs therefore fail reconciliation (and for
    VirtualChassis, drop the whole chassis entity including its unique
    ``platformone_cluster_id``). Mirror enough identity from the parent
    Assets row to pass that check; top-level Device entities remain the
    source of truth.
    """
    return Device(
        name=name,
        **_device_identity_fields(
            function=function,
            product_type=product_type,
            site_name=site_name,
        ),
    )


def _device_identity_fields(
    *,
    function: str | None = None,
    product_type: str | None = None,
    site_name: str | None = None,
) -> dict:
    """Device identity fields needed by Diode nested refs and light updates."""
    kwargs: dict = {}
    if site_name:
        kwargs["site"] = Site(name=site_name)
    role = role_for(function)
    if role:
        role_name, role_slug = role
        kwargs["role"] = DeviceRole(name=role_name, slug=role_slug)
    model = device_type_model_for(product_type)
    if model:
        kwargs["device_type"] = DeviceType(model=model, manufacturer=MANUFACTURER)
        kwargs["manufacturer"] = MANUFACTURER
    return kwargs


def _interface_custom_fields(*, interface_id: str | None = None) -> dict:
    """Build interface custom fields (ConfigState asset_interface_id)."""
    if not interface_id:
        return {}
    return {CF_INTERFACE_ID: _cf_text(str(interface_id))}


def _coerce_bool(value) -> bool | None:
    """Accept JSON bools, 0/1, or common true/false strings; else None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in {"true", "1", "yes"}:
            return True
        if text in {"false", "0", "no"}:
            return False
    return None


def _interface_identity_kwargs(
    *,
    device: str | Device,
    name: str,
    interface_id: str | None = None,
    enabled=None,
) -> dict:
    """Shared device/name/tags/custom_fields/enabled base for Interface entities.

    Always asserts ``enabled``: Diode/protobuf maps an omitted bool to false, so
    leaving it unset would invent admin-down. Unknown/missing → admin-up
    (same posture as LAG parents when Platform ONE omits a usable signal).
    """
    kwargs: dict = {
        "device": device,
        "name": name,
        "tags": PROVENANCE_TAGS,
    }
    custom_fields = _interface_custom_fields(interface_id=interface_id)
    if custom_fields:
        kwargs["custom_fields"] = custom_fields
    coerced = _coerce_bool(enabled)
    kwargs["enabled"] = True if coerced is None else coerced
    return kwargs


def _normalized_mac(value) -> str | None:
    if not value:
        return None
    return str(value).upper()


def _compact_token(value: str, drop: str = " _-") -> str:
    """Casefold and strip separator characters for fuzzy token matching."""
    text = str(value).casefold()
    for ch in drop:
        text = text.replace(ch, "")
    return text


def _coerce_int(value) -> int | None:
    """Accept JSON ints or digit-only strings; reject floats/bools/garbage."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _explicit_cidr(raw, mask_length=None) -> str | None:
    """Parse only explicitly-prefixed addresses; never invent /32 or /128.

    Accepts an inline ``/n`` in ``raw``, or a bare host plus usable
    ``mask_length``. Invalid values return None.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if "/" not in text:
        mask = _coerce_int(mask_length)
        if mask is None or not 0 <= mask <= 128:
            return None
        text = f"{text}/{mask}"
    try:
        return str(ipaddress.ip_interface(text))
    except ValueError:
        return None
