"""Interface IP helpers for switch port transforms."""

from __future__ import annotations

import ipaddress

from netboxlabs.diode.sdk.ingester import Entity, Interface, IPAddress

from .common import PROVENANCE_TAGS, _explicit_cidr, _interface_identity_kwargs
from .port_constants import VIRTUAL_INTERFACE_TYPE


def _mgmt_interface_ids(tables: dict[str, list[dict]]) -> set[str]:
    """Interface UUIDs flagged management_port via port capabilities + port rows."""
    mgmt_ports = {
        (str(cap.get("asset_device_id") or ""), str(cap.get("port_name") or ""))
        for cap in tables.get("port_capabilities") or []
        if cap.get("management_port") is True and cap.get("port_name")
    }
    if not mgmt_ports:
        return set()
    ids: set[str] = set()
    for row in (*(tables.get("port_configs") or []), *(tables.get("port_states") or [])):
        key = (str(row.get("asset_device_id") or ""), str(row.get("name") or ""))
        interface_id = str(row.get("asset_interface_id") or "")
        if interface_id and key in mgmt_ports:
            ids.add(interface_id)
    return ids


def _pick_primary_cidr(candidates: list[tuple[int, str]]) -> dict[str, str]:
    """Keep the first CIDR per address family from ranked candidates."""
    result: dict[str, str] = {}
    for version, cidr in candidates:
        key = "primary_ip4" if version == 4 else "primary_ip6"
        result.setdefault(key, cidr)
    return result


_CidrRow = tuple[dict, str, ipaddress.IPv4Interface | ipaddress.IPv6Interface]


def _ranked_ip_matches(rows: list[_CidrRow], predicate) -> list[tuple[int, str]]:
    return [(iface.version, cidr) for row, cidr, iface in rows if predicate(row, iface)]


def primary_ips_from_tables(
    tables: dict[str, list[dict]],
    *,
    asset_ip: str | None = None,
) -> dict[str, str]:
    """Derive Device primary_ip4/primary_ip6 from ConfigState interface IPs.

    Prefers rows with ``is_primary`` True, then IPs on ``management_port``
    interfaces, then an interface IP whose host matches Assets ``ip_address``.
    Every candidate must have a real prefix (``mask_length`` / CIDR); bare
    hosts are never padded with /32 or /128.
    """
    rows_with_cidr: list[tuple[dict, str, ipaddress.IPv4Interface | ipaddress.IPv6Interface]] = []
    for row in tables.get("interface_ips") or []:
        # Require an explicit prefix from ConfigState (mask_length or inline /n);
        # never accept ip_interface's implicit /32 or /128 on a bare host.
        cidr = _interface_ip_cidr(row)
        if not cidr:
            continue
        try:
            iface = ipaddress.ip_interface(cidr)
        except ValueError:
            continue
        rows_with_cidr.append((row, cidr, iface))

    if not rows_with_cidr:
        return {}

    ranked = _ranked_ip_matches(rows_with_cidr, lambda row, _iface: row.get("is_primary") is True)
    if ranked:
        return _pick_primary_cidr(ranked)

    mgmt_ids = _mgmt_interface_ids(tables)
    if mgmt_ids:
        ranked = _ranked_ip_matches(
            rows_with_cidr,
            lambda row, _iface: str(row.get("asset_interface_id") or "") in mgmt_ids,
        )
        if ranked:
            return _pick_primary_cidr(ranked)

    asset_host = (asset_ip or "").strip()
    if asset_host and "/" in asset_host:
        asset_host = asset_host.split("/", 1)[0]
    if asset_host:
        try:
            asset_address = ipaddress.ip_address(asset_host)
        except ValueError:
            asset_address = None
        if asset_address is not None:
            ranked = _ranked_ip_matches(rows_with_cidr, lambda _row, iface: iface.ip == asset_address)
            if ranked:
                return _pick_primary_cidr(ranked)

    return {}


def _interface_ip_cidr(row: dict) -> str | None:
    """Build address/prefix for AssetInterfaceIpAddress → Diode IPAddress.

    `address` is a bare address and `mask_length` its prefix length. Without
    an explicit prefix (inline ``/n`` or usable ``mask_length``), return
    None — never invent /32 or /128.
    """
    return _explicit_cidr(row.get("address"), row.get("mask_length"))


def _ip_entities_for_interface(
    *,
    device: object,
    interface_name: str,
    rows: list[dict],
    interface_type: str | None = None,
) -> list[Entity]:
    entities: list[Entity] = []
    seen: set[str] = set()
    for row in rows:
        cidr = _interface_ip_cidr(row)
        if not cidr or cidr in seen:
            continue
        seen.add(cidr)
        # Mirror the parent Interface: omit type when unknown so an IP upsert
        # cannot re-assert ``other`` over a good prior NetBox value.
        iface_kwargs: dict = {"device": device, "name": interface_name}
        if interface_type is not None:
            iface_kwargs["type"] = interface_type
        entities.append(
            Entity(
                ip_address=IPAddress(
                    address=cidr,
                    status="active",
                    assigned_object_interface=Interface(**iface_kwargs),
                    tags=PROVENANCE_TAGS,
                ),
            ),
        )
    return entities


def _orphan_ip_entities(
    *,
    device: object,
    interface_ips: dict[str, list[dict]],
    emitted_keys: dict[str, str],
    interface_names: dict[str, str],
) -> list[Entity]:
    """IPs on interfaces that got no Interface entity above (e.g. VLAN/SVI
    interfaces, which appear in vlan_properties but not the port tables).

    Emits a minimal Interface first so the IPAddress has a real assigned
    object, then the IP entities. ``type=virtual`` for these non-port rows
    (SVIs); NetBox requires a non-blank type.

    Interface names come from already-fetched port/LAG/VLAN rows keyed by
    ``asset_interface_id``. ``AssetInterfaceIpAddress`` has no interface_name
    field in OpenAPI — do not invent one from the IP row.
    """
    entities: list[Entity] = []
    emitted_names: set[str] = set()
    for key, rows in sorted(interface_ips.items()):
        if key in emitted_keys:
            continue
        name = interface_names.get(key)
        if not name:
            continue
        if name not in emitted_names:
            entities.append(
                Entity(
                    interface=Interface(
                        **{
                            **_interface_identity_kwargs(
                                device=device,
                                name=name,
                                interface_id=key or None,
                            ),
                            "type": VIRTUAL_INTERFACE_TYPE,
                        },
                    ),
                ),
            )
            emitted_names.add(name)
        entities.extend(
            _ip_entities_for_interface(
                device=device,
                interface_name=name,
                rows=rows,
                interface_type=VIRTUAL_INTERFACE_TYPE,
            ),
        )
    return entities


def _interface_names_by_id(tables: dict[str, list[dict]]) -> dict[str, str]:
    """Map asset_interface_id → interface name from port/LAG/VLAN rows.

    Prefer port/LAG ``name``, then vlan-properties ``interface_name``. First
    non-empty name wins so later tables do not rename an already-known id.
    """
    names: dict[str, str] = {}
    for key in ("port_configs", "port_states", "lag_configs", "lag_states"):
        for row in tables.get(key) or []:
            interface_id = str(row.get("asset_interface_id") or "")
            name = str(row.get("name") or "").strip()
            if interface_id and name:
                names.setdefault(interface_id, name)
    for row in tables.get("vlan_properties") or []:
        interface_id = str(row.get("asset_interface_id") or "")
        name = str(row.get("interface_name") or "").strip()
        if interface_id and name:
            names.setdefault(interface_id, name)
    return names
