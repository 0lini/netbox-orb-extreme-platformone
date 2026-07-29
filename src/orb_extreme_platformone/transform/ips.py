"""Interface IP helpers for switch port transforms."""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

from netboxlabs.diode.sdk.ingester import Entity, Interface, IPAddress

from .common import PROVENANCE_TAGS, _explicit_cidr, _interface_identity_kwargs

if TYPE_CHECKING:
    from collections.abc import Callable
from .port_constants import VIRTUAL_INTERFACE_TYPE

_IPV4_VERSION = 4


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
        port_key = (str(row.get("asset_device_id") or ""), str(row.get("name") or ""))
        interface_id = str(row.get("asset_interface_id") or "")
        if interface_id and port_key in mgmt_ports:
            ids.add(interface_id)
    return ids


_IpInterface = ipaddress.IPv4Interface | ipaddress.IPv6Interface
_CidrRow = tuple[dict, str, _IpInterface]


def _pick_primary_cidr(rows: list[_CidrRow], matches: Callable[[dict, _IpInterface], bool]) -> dict[str, str]:
    """First matching CIDR per address family, in input order."""
    picked: dict[str, str] = {}
    for row, cidr, iface in rows:
        if matches(row, iface):
            field = "primary_ip4" if iface.version == _IPV4_VERSION else "primary_ip6"
            picked.setdefault(field, cidr)
    return picked


def _parsed_address(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse a bare host address, or None when absent or malformed.

    Assets declares ``Device.ip_address`` as ``format: ipv4`` dotted decimal, so
    a prefixed value is off-spec and simply fails to match — better than
    guessing at a host half.
    """
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def primary_ips_from_tables(
    tables: dict[str, list[dict]],
    *,
    asset_ip: str | None = None,
) -> dict[str, str]:
    """Derive Device primary_ip4/primary_ip6 from ConfigState interface IPs.

    Prefers rows with ``is_primary`` True, then IPs on ``management_port``
    interfaces, then an interface IP whose host matches Assets ``ip_address``.
    The first tier that matches anything wins outright; families are not mixed
    across tiers. Every candidate must have a real prefix (``mask_length`` /
    CIDR); bare hosts are never padded with /32 or /128.
    """
    rows_with_cidr: list[_CidrRow] = []
    for row in tables.get("interface_ips") or []:
        # Require an explicit prefix from ConfigState (mask_length or inline /n);
        # never accept ip_interface's implicit /32 or /128 on a bare host.
        cidr = _interface_ip_cidr(row)
        if not cidr:
            continue
        # `cidr` is already str(ip_interface(...)) from _explicit_cidr, so this
        # parse cannot fail.
        rows_with_cidr.append((row, cidr, ipaddress.ip_interface(cidr)))

    if not rows_with_cidr:
        return {}

    mgmt_ids = _mgmt_interface_ids(tables)
    asset_address = _parsed_address(asset_ip)
    tiers: tuple[Callable[[dict, _IpInterface], bool], ...] = (
        lambda row, _iface: row.get("is_primary") is True,
        lambda row, _iface: str(row.get("asset_interface_id") or "") in mgmt_ids,
        lambda _row, iface: asset_address is not None and iface.ip == asset_address,
    )
    for matches in tiers:
        picked = _pick_primary_cidr(rows_with_cidr, matches)
        if picked:
            return picked
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
    """Emit IPs for interfaces that got no Interface entity above.

    Covers VLAN/SVI interfaces, which appear in vlan_properties but not in the
    port tables.

    Emits a minimal Interface first so the IPAddress has a real assigned
    object, then the IP entities. ``type=virtual`` for these non-port rows
    (SVIs); NetBox requires a non-blank type.

    Interface names come from already-fetched port/LAG/VLAN rows keyed by
    ``asset_interface_id``. ``AssetInterfaceIpAddress`` has no interface_name
    field in OpenAPI — do not invent one from the IP row.
    """
    entities: list[Entity] = []
    emitted_names: set[str] = set()
    for interface_id, rows in sorted(interface_ips.items()):
        if interface_id in emitted_keys:
            continue
        name = interface_names.get(interface_id)
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
                                interface_id=interface_id or None,
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


# Tables that carry an interface name, and the field they spell it with, in
# preference order: port/LAG `name` first, then vlan-properties
# `interface_name`. First non-empty name wins per id.
_NAME_FIELD_BY_TABLE = {
    "port_configs": "name",
    "port_states": "name",
    "lag_configs": "name",
    "lag_states": "name",
    "vlan_properties": "interface_name",
}


def _interface_names_by_id(tables: dict[str, list[dict]]) -> dict[str, str]:
    """Map asset_interface_id → interface name from port/LAG/VLAN rows."""
    names: dict[str, str] = {}
    for table_key, name_field in _NAME_FIELD_BY_TABLE.items():
        for row in tables.get(table_key) or []:
            interface_id = str(row.get("asset_interface_id") or "")
            name = str(row.get(name_field) or "").strip()
            if interface_id and name:
                names.setdefault(interface_id, name)
    return names
