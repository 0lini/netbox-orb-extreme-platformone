"""Shared helpers for transform tests."""

from __future__ import annotations

from tests.conftest import PORT_CONFIG, PORT_STATE, SWITCH_ASSET, VLAN_PROPERTIES


def _record(asset=SWITCH_ASSET, location=None, cs_device_id="cs-uuid-42", cs_device=None):
    return {
        "asset": asset,
        "cs_device_id": cs_device_id,
        "cs_device": cs_device,
        "location": location,
    }


def _tables(**overrides):
    tables = {
        "port_configs": [PORT_CONFIG],
        "port_states": [PORT_STATE],
        "vlan_properties": [VLAN_PROPERTIES],
    }
    tables.update(overrides)
    return tables


def _wireless_tables(
    *,
    device_id: str = "cs-ap-1",
    interface_id: str = "radio-uuid-1",
    name: str = "wifi0",
    enabled=True,
    state: dict | None = None,
    ssid_configs: list | None = None,
    ssid_states: list | None = None,
):
    """Minimal AP wireless table bucket for radios_to_entities tests."""
    config = {
        "asset_device_id": device_id,
        "asset_interface_id": interface_id,
        "name": name,
    }
    if enabled is not None:
        config["enabled"] = enabled
    states = []
    if state is not None:
        states.append(
            {
                "asset_device_id": device_id,
                "asset_interface_id": interface_id,
                "name": name,
                **state,
            },
        )
    return {
        device_id: {
            "wireless_interfaces": [config],
            "wireless_states": states,
            "ssid_configs": ssid_configs or [],
            "ssid_states": ssid_states or [],
        },
    }
