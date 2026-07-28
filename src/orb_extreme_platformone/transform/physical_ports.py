"""Physical switch port entity builders."""

from __future__ import annotations

from netboxlabs.diode.sdk.ingester import Device, Entity, Interface

from .common import _coerce_int, _interface_identity_kwargs, _normalized_mac
from .ips import _ip_entities_for_interface
from .port_constants import (
    _TYPE_BY_SPEED_AND_CONNECTOR,
    LAG_INTERFACE_TYPE,
    OPER_STATE_UP,
    VERIFIED_CONFIG_DUPLEX,
    VERIFIED_OPER_DUPLEX,
    VERIFIED_OPER_SPEED_KBPS,
    VERIFIED_POE_CLASSIFICATION,
)
from .port_join import _first_row, _optional_first_row
from .vlans import _vlan_fields, _vlan_records_for


def _poe_mode(state: dict) -> str | None:
    """NetBox poe_mode=pse when AssetPoePowerPortsState.supported is true.

    ``AssetPoePowerPortsConfig.enable`` is not used: admin enable alone does
    not mean the port is a PSE.
    """
    if state.get("supported") is True:
        return "pse"
    return None


def _poe_type(config: dict) -> str | None:
    """Map AssetPoePowerPortsConfig.classification to Diode poe_type."""
    code = _coerce_int(config.get("classification"))
    if code is None:
        return None
    return VERIFIED_POE_CLASSIFICATION.get(code)


def _duplex(state: dict, config: dict) -> str | None:
    """Prefer oper_duplex; fall back to config duplex only when oper is unset.

    Config fallback applies when ``oper_duplex`` is missing or UNSET (0).
    NONE (3), AUTO (4), and unknown oper codes assert nothing — they must not
    inherit configured half/full/auto.
    """
    oper = _coerce_int(state.get("oper_duplex"))
    if oper is not None and oper in VERIFIED_OPER_DUPLEX:
        return VERIFIED_OPER_DUPLEX[oper]
    if oper not in (None, 0):
        return None
    cfg = _coerce_int(config.get("duplex"))
    if cfg is not None and cfg in VERIFIED_CONFIG_DUPLEX:
        return VERIFIED_CONFIG_DUPLEX[cfg]
    return None


def _iface_base_kwargs(
    *,
    device: Device,
    name: str,
    interface_id: str | None,
    config: dict,
    poe_state: dict | None = None,
    poe_config: dict | None = None,
) -> dict:
    """Shared identity / admin / PoE fields for physical ports and LAG parents."""
    kwargs = _interface_identity_kwargs(
        device=device,
        name=name,
        interface_id=interface_id,
        enabled=config.get("enabled"),
    )
    poe = _poe_mode(poe_state or {})
    if poe is not None:
        kwargs["poe_mode"] = poe
    poe_type = _poe_type(poe_config or {})
    if poe_type is not None:
        kwargs["poe_type"] = poe_type
    return kwargs


def _port_kwargs(
    *,
    device: Device,
    name: str,
    interface_id: str | None,
    config: dict,
    state: dict,
    vlan_records: list[dict],
    capability: dict | None = None,
    poe_state: dict | None = None,
    poe_config: dict | None = None,
) -> dict:
    kwargs = _iface_base_kwargs(
        device=device,
        name=name,
        interface_id=interface_id,
        config=config,
        poe_state=poe_state,
        poe_config=poe_config,
    )

    # Link state maps to mark_connected, never to `enabled` -- admin state is
    # asserted separately above. Coerce ints so string JSON codes still map.
    oper_state = _coerce_int(state.get("oper_state"))
    if oper_state is not None:
        kwargs["mark_connected"] = oper_state == OPER_STATE_UP

    oper_speed = _coerce_int(state.get("oper_speed"))
    connector_type = _coerce_int(state.get("connector_type"))
    speed = VERIFIED_OPER_SPEED_KBPS.get(oper_speed) if oper_speed is not None else None
    if speed is not None:
        kwargs["speed"] = speed
    duplex = _duplex(state, config)
    if duplex is not None:
        kwargs["duplex"] = duplex
    # Assert type only when speed/connector map to a verified NetBox type.
    # Unknown codes omit type — do not invent ``other``.
    mapped_type = _TYPE_BY_SPEED_AND_CONNECTOR.get((oper_speed, connector_type))
    if mapped_type is not None:
        kwargs["type"] = mapped_type

    if config.get("description"):
        kwargs["description"] = config["description"]
    mac = _normalized_mac(state.get("mac_address"))
    if mac:
        kwargs["primary_mac_address"] = mac

    if capability is not None and isinstance(capability.get("management_port"), bool):
        kwargs["mgmt_only"] = capability["management_port"]

    kwargs.update(_vlan_fields(vlan_records))
    return kwargs


def _physical_port_entities(
    *,
    device: Device,
    configs: dict[str, list[dict]],
    states: dict[str, list[dict]],
    vlans: dict[str, list[dict]],
    capabilities: dict[tuple[str, str], dict],
    poe_states: dict[str, list[dict]],
    poe_configs: dict[str, list[dict]],
    interface_ips: dict[str, list[dict]],
    lag_names: set[str],
    lag_interface_ids: set[str],
    membership: dict[str, str],
) -> tuple[list[Entity], dict[str, str]]:
    """Emit physical (non-LAG) port interfaces joined on asset_interface_id."""
    entities: list[Entity] = []
    emitted_keys: dict[str, str] = {}

    for key in sorted(set(configs) | set(states)):
        config = _first_row(configs, key, table="port_configs")
        state = _first_row(states, key, table="port_states")
        name = str(config.get("name") or state.get("name") or "")
        if not name:
            continue
        # `key` is asset_interface_id (required on port config/state).
        if key in lag_interface_ids:
            continue
        if name in lag_names:
            continue
        port_device_id = str(config.get("asset_device_id") or state.get("asset_device_id") or "")
        kwargs = _port_kwargs(
            device=device,
            name=name,
            interface_id=key,
            config=config,
            state=state,
            vlan_records=_vlan_records_for(vlans, interface_id=key),
            capability=capabilities.get((port_device_id, name)),
            poe_state=_optional_first_row(poe_states, key, table="poe_states"),
            poe_config=_optional_first_row(poe_configs, key, table="poe_configs"),
        )
        lag_parent = membership.get(name)
        if lag_parent:
            kwargs["lag"] = Interface(device=device, name=lag_parent, type=LAG_INTERFACE_TYPE)
        entities.append(Entity(interface=Interface(**kwargs)))
        emitted_keys[key] = name
        entities.extend(
            _ip_entities_for_interface(
                device=device,
                interface_name=name,
                rows=interface_ips.get(key, []),
                interface_type=kwargs.get("type"),
            ),
        )

    return entities, emitted_keys
