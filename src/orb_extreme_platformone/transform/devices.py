"""Device, site, and location mapping."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from netboxlabs.diode.sdk.ingester import (
    Device,
    Entity,
    Location,
    Platform,
    Site,
    VirtualChassis,
)

from orb_extreme_platformone.identity import DeviceRecord, expand_location_paths, platform_name

if TYPE_CHECKING:
    from collections.abc import Iterator

from .common import (
    CF_DEVICE_ID,
    MANUFACTURER,
    PROVENANCE_TAGS,
    _cf_text,
    _device_identity_fields,
    _virtual_chassis_kwargs,
    logger,
)


def _status_for(asset: dict) -> str | None:
    """Map Assets `is_connected` to Device status when known.

    ``true`` → ``active``, ``false`` → ``offline``. Missing/unknown values
    assert nothing — do not invent ``active``.
    """
    connected = asset.get("is_connected")
    if connected is True:
        return "active"
    if connected is False:
        return "offline"
    return None


def _coord(value) -> float | None:
    """Return a finite float coordinate, or None when unset/invalid."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _site_kwargs(site_name: str, coords: tuple[float | None, float | None] | None) -> dict:
    kwargs: dict = {"name": site_name}
    if coords:
        lat, lon = coords
        if lat is not None and -90.0 <= lat <= 90.0:
            kwargs["latitude"] = lat
        if lon is not None and -180.0 <= lon <= 180.0:
            kwargs["longitude"] = lon
    return kwargs


def _device_kwargs(
    record: DeviceRecord,
    *,
    location: Location | None,
    vc_membership: dict | None = None,
    fabric_fields: dict | None = None,
) -> dict:
    asset = record.asset
    custom_fields: dict = {}
    if asset.get("device_id") is not None:
        custom_fields[CF_DEVICE_ID] = _cf_text(str(asset["device_id"]))
    if fabric_fields:
        custom_fields.update(fabric_fields)
    kwargs = {
        "serial": asset.get("serial_number") or None,
        "custom_fields": custom_fields,
        "tags": PROVENANCE_TAGS,
        **_device_identity_fields(record),
    }
    if record.name is not None:
        kwargs["name"] = record.name
    status = _status_for(asset)
    if status is not None:
        kwargs["status"] = status
    if location is not None:
        kwargs["location"] = location

    # Assets product_type / os_version only — no ConfigState model_name /
    # firmware_version fallback.
    platform = platform_name(record.function, asset.get("os_version"))
    if platform:
        kwargs["platform"] = Platform(name=platform, manufacturer=MANUFACTURER)
    if vc_membership:
        kwargs["virtual_chassis"] = VirtualChassis(
            **_virtual_chassis_kwargs(vc_membership["name"], vc_membership.get("cluster_id")),
        )
        kwargs["vc_position"] = vc_membership["position"]
    return kwargs


def _iter_scoped_devices(
    records: list[DeviceRecord],
    *,
    site_scope: set[str] | None,
) -> Iterator[tuple[DeviceRecord, str]]:
    """Yield ``(record, site_name)`` for records that pass scope.

    The site is yielded alongside because it is non-optional here and optional
    on the record: Platform ONE assigns every device a site itself, so a record
    without one is unexpected and skipped rather than given an invented
    default. Site matching is case-insensitive (policy ``hq`` matches ``HQ``).
    """
    folded_scope = {site.casefold() for site in site_scope} if site_scope else None
    for record in records:
        site_name = record.site_name
        if site_name is None:
            logger.warning("Skipping device %s: Platform ONE reports no site for it", record.label)
            continue
        if folded_scope and site_name.casefold() not in folded_scope:
            continue
        yield record, site_name


def scope_devices(records: list[DeviceRecord], *, site_scope: set[str] | None) -> list[DeviceRecord]:
    """Return the device records whose resolved site is in site_scope (all, if no scope).

    Ownership: the backend scopes once up front (port fan-out must match the
    device list). Pass the result to `devices_to_entities` with
    `site_scope=None` so mapping does not re-filter by site. Direct callers
    that have not scoped yet may pass `site_scope` into `devices_to_entities`
    instead.
    """
    return [record for record, _ in _iter_scoped_devices(records, site_scope=site_scope)]


def _merge_site_coords(
    site_coords: dict[str, tuple[float | None, float | None]],
    site_name: str,
    location: dict | None,
) -> None:
    """Keep the first non-null lat/lon seen per site name."""
    if not location:
        return
    lat = _coord(location.get("site_latitude"))
    lon = _coord(location.get("site_longitude"))
    if lat is None and lon is None:
        return
    existing = site_coords.get(site_name)
    if existing is None:
        site_coords[site_name] = (lat, lon)
        return
    prev_lat, prev_lon = existing
    site_coords[site_name] = (
        prev_lat if prev_lat is not None else lat,
        prev_lon if prev_lon is not None else lon,
    )


def devices_to_entities(
    records: list[DeviceRecord],
    *,
    site_scope: set[str] | None = None,
    virtual_chassis_entities: list[Entity] | None = None,
    vc_memberships: dict[str, dict] | None = None,
    fabric_by_cs_id: dict[str, dict] | None = None,
) -> list[Entity]:
    """Map device records to Diode Site, Location, Device and VirtualChassis entities.

    One Site per distinct site, one nested Location per Building/Floor level in
    use, then Devices, then VirtualChassis (if any).

    When the caller has already run `scope_devices` (backend tick path), pass
    `site_scope=None` so this does not re-filter by site. When calling
    directly with an unscoped list, pass `site_scope` here instead.

    `vc_memberships` is keyed by ConfigState device UUID (`cs_device_id`).
    `fabric_by_cs_id` carries optional ISIS/SPBM text custom-field values for
    the same key. Primary IPs are applied separately via
    `primary_ip_device_entities` after Interface/IPAddress entities (see
    backend tick ordering). Assets `ip_address` is bare and is never asserted
    as a primary IP.

    Devices with ``virtual_chassis`` / ``vc_position`` are emitted before the
    first-class VirtualChassis entities that set ``master``. NetBox rejects
    assigning a master that is not yet a chassis member; on a fresh VC create
    Diode applies entities in iterable order within a batch, so membership
    must land before master.
    """
    entities: list[Entity] = []
    site_names: set[str] = set()
    location_paths: set[tuple[str, tuple[str, ...]]] = set()
    site_coords: dict[str, tuple[float | None, float | None]] = {}

    scoped = list(_iter_scoped_devices(records, site_scope=site_scope))
    for record, site_name in scoped:
        site_names.add(site_name)
        _merge_site_coords(site_coords, site_name, record.location)
        if record.location_path:
            location_paths.add((site_name, tuple(record.location_path)))

    for site_name in sorted(site_names):
        entities.append(Entity(site=Site(**_site_kwargs(site_name, site_coords.get(site_name)))))

    # expand_location_paths orders ancestors before descendants, so one pass
    # can thread `parent` through the cache.
    location_cache: dict[tuple[str, tuple[str, ...]], Location] = {}
    for site_name, path in expand_location_paths(location_paths):
        parent = location_cache.get((site_name, path[:-1])) if len(path) > 1 else None
        location = Location(name=path[-1], site=site_name, parent=parent)
        location_cache[(site_name, path)] = location
        entities.append(Entity(location=location))

    for record, site_name in scoped:
        path = tuple(record.location_path)
        cs_device_id = record.cs_device_id
        entities.append(
            Entity(
                device=Device(
                    **_device_kwargs(
                        record,
                        location=location_cache.get((site_name, path)) if path else None,
                        vc_membership=(vc_memberships or {}).get(cs_device_id) if cs_device_id else None,
                        fabric_fields=(fabric_by_cs_id or {}).get(cs_device_id) if cs_device_id else None,
                    ),
                ),
            ),
        )

    # After member Devices so NetBox accepts VirtualChassis.master on create.
    if virtual_chassis_entities:
        entities.extend(virtual_chassis_entities)

    return entities


def primary_ip_device_entities(
    records: list[DeviceRecord],
    *,
    primary_ips_by_cs_id: dict[str, dict[str, str]],
) -> list[Entity]:
    """Emit follow-up Device entities that only assert primary_ip4/primary_ip6.

    Diode may split a Device create into a minimal create + an update. When
    ``primary_ip*`` is on that update before the matching Interface IPAddress
    exists, NetBox rejects the whole update (``IP address is not assigned to
    this device``) and drops sibling fields such as ``serial`` and custom
    fields. Call this *after* port/IP entities so the IP already exists.
    """
    if not primary_ips_by_cs_id:
        return []
    entities: list[Entity] = []
    for record, _site_name in _iter_scoped_devices(records, site_scope=None):
        primary_ips = primary_ips_by_cs_id.get(record.cs_device_id or "")
        if not primary_ips or record.name is None:
            continue
        # Enough identity for Diode generate-diff to match the existing Device;
        # avoid re-asserting serial/tags/CFs here so a primary-IP failure cannot
        # wipe them again.
        entities.append(
            Entity(
                device=Device(name=record.name, **_device_identity_fields(record), **primary_ips),
            ),
        )
    return entities
