"""Switch port, LAG, VLAN, PoE, and interface-IP mapping."""

from __future__ import annotations

from typing import TYPE_CHECKING

from orb_extreme_platformone.identity import SLASH_PORT_FUNCTIONS

from .common import _device_ref
from .ips import _interface_names_by_id, _orphan_ip_entities, primary_ips_from_tables
from .lags import _lag_entities
from .physical_ports import _physical_port_entities
from .port_constants import PORT_ENTITY_TABLE_KEYS
from .port_join import _by_key, _capabilities_by_port, _native_port_name_tables

if TYPE_CHECKING:
    from netboxlabs.diode.sdk.ingester import Entity

__all__ = [
    "PORT_ENTITY_TABLE_KEYS",
    "ports_to_entities",
    "primary_ips_from_tables",
]


def ports_to_entities(
    tables: dict[str, list[dict]],
    *,
    device: str,
    function: str | None = None,
    site_name: str | None = None,
    product_type: str | None = None,
) -> list[Entity]:
    """Map one switch's ConfigState port + LAG + VLAN tables to Diode entities.

    `tables` holds the device's "port_configs", "port_states",
    "vlan_properties", "lag_configs", "lag_states", optional
    "port_capabilities", "poe_states", "poe_configs", and "interface_ips"
    rows. Physical ports are the union of config+state rows joined on
    asset_interface_id. LAG interfaces come from lag
    config/state (type `lag`); member ports get Diode `Interface.lag`
    pointing at the parent LAG (membership from lag-config, falling back to
    lag-state `member_ports` when config omits them; members without a port
    row are not stubbed). Interface IP rows become Diode
    IPAddress entities assigned to the matching interface. VLAN membership
    refs use `vid` plus `name=str(vid)` (NetBox requires a name;
    switch-local names are not site-scoped). Physical ports without a
    verified connector map omit `type` (do not invent `other`); SVI stubs use
    `virtual`.

    Nested Interface ``device`` refs include site/role/device_type when
    known — Diode rejects name-only Device stubs during generate-diff.

    `function` (the Assets OS family) rewrites ConfigState's slot:port
    notation to the OS-native form (1:52 -> 1/52 on Fabric Engine / VOSS)
    before any joining, so every emitted name and cross-reference agrees.
    """
    if function and function.upper() in SLASH_PORT_FUNCTIONS:
        tables = _native_port_name_tables(tables, function)
    device_ref = _device_ref(
        name=device,
        site_name=site_name,
        function=function,
        product_type=product_type,
    )
    configs = _by_key(tables.get("port_configs") or [])
    states = _by_key(tables.get("port_states") or [])
    vlan_rows = tables.get("vlan_properties") or []
    vlans = _by_key(vlan_rows)
    capabilities = _capabilities_by_port(tables.get("port_capabilities") or [])
    poe_states = _by_key(tables.get("poe_states") or [])
    poe_configs = _by_key(tables.get("poe_configs") or [])
    interface_ips = _by_key(tables.get("interface_ips") or [])
    lag_configs = tables.get("lag_configs") or []
    lag_states = tables.get("lag_states") or []

    lag_entities, lag_names, lag_interface_ids, membership, emitted_keys = _lag_entities(
        device=device_ref,
        lag_configs=lag_configs,
        lag_states=lag_states,
        vlans=vlans,
        poe_states=poe_states,
        poe_configs=poe_configs,
        interface_ips=interface_ips,
        port_configs=configs,
        port_states=states,
    )
    entities = list(lag_entities)

    port_entities, port_keys = _physical_port_entities(
        device=device_ref,
        configs=configs,
        states=states,
        vlans=vlans,
        capabilities=capabilities,
        poe_states=poe_states,
        poe_configs=poe_configs,
        interface_ips=interface_ips,
        lag_names=lag_names,
        lag_interface_ids=lag_interface_ids,
        membership=membership,
    )
    entities.extend(port_entities)
    emitted_keys.update(port_keys)

    entities.extend(
        _orphan_ip_entities(
            device=device_ref,
            interface_ips=interface_ips,
            emitted_keys=emitted_keys,
            interface_names=_interface_names_by_id(tables),
        ),
    )
    return entities
