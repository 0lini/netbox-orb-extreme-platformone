"""AP radio and WirelessLAN mapping."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from netboxlabs.diode.sdk.ingester import Device, Entity, Interface, WirelessLAN

from orb_extreme_platformone.catalog import WIRELESS_TABLES

from .common import _coerce_int, _device_ref, _interface_identity_kwargs, _normalized_mac
from .port_join import _first_row, _group_by_interface_id
from .wireless_auth import _ensure_wlan, _wlan_kwargs
from .wireless_rf import (
    _channel_frequency_mhz,
    _channel_width_mhz,
    _is_wireless_interface_type,
    _radio_type,
)

# Keys `radios_to_entities` reads — derived from the extract catalog.
WIRELESS_ENTITY_TABLE_KEYS = frozenset(WIRELESS_TABLES)


if TYPE_CHECKING:
    from orb_extreme_platformone.identity import DeviceRecord


def _split_if_names(value: str | None) -> list[str]:
    """Split ``AssetSsid*.if_names`` into interface names.

    The spec declares it a single ``string``, so one radio and several arrive
    in the same field with a comma between them. No list form to handle, and
    no speculative JSON / alternate-separator parsing.
    """
    return [name.strip() for name in str(value or "").split(",") if name.strip()]


def _ssid_name(row: dict) -> str:
    return str(row.get("name") or "").strip()


def _primary_wireless_state(states: list[dict]) -> dict:
    """First non-empty state row for radio identity / RF fields.

    Multiple state rows per ``asset_interface_id`` are valid (SSID names), but
    radio_mode / BSSID / power / channel live on a single primary state view.
    """
    return next((row for row in states if row), {})


def _radio_interface_kwargs(
    *,
    device: Device,
    name: str,
    config: dict,
    state: dict,
    ssids: list[str],
) -> dict:
    interface_id = str(config.get("asset_interface_id") or state.get("asset_interface_id") or "")
    kwargs = _interface_identity_kwargs(
        device=device,
        name=name,
        interface_id=interface_id or None,
        enabled=config.get("enabled"),
    )
    # radio_mode exists only on AssetWirelessInterfaceState, not config.
    # Assert type only for a known ieee802.11* mode. Missing/unknown mode
    # omits type — do not invent ``other`` (and RF/WLAN links stay gated off).
    radio_type = _radio_type(state.get("radio_mode"))
    if radio_type is not None:
        kwargs["type"] = radio_type
    wireless = _is_wireless_interface_type(kwargs.get("type"))
    if wireless:
        kwargs["rf_role"] = "ap"
        tx_power = _coerce_int(state.get("power"))
        if tx_power is not None:
            kwargs["tx_power"] = tx_power
        frequency = _channel_frequency_mhz(state.get("band"), state.get("channel"))
        if frequency is not None:
            kwargs["rf_channel_frequency"] = frequency
        width = _channel_width_mhz(state.get("channel_width"))
        if width is not None:
            kwargs["rf_channel_width"] = width
    mac = _normalized_mac(state.get("bssid"))
    if mac:
        kwargs["primary_mac_address"] = mac
    # wireless_lans is only legal on ieee802.11* types (same constraint as rf_role).
    if ssids and wireless:
        kwargs["wireless_lans"] = ssids
    return kwargs


def _link_ssid_radios(
    *,
    device_id: str,
    ssid: str,
    if_names,
    name_to_interface_id: dict[str, str],
    ssids_by_radio: dict[tuple[str, str], list[str]],
) -> None:
    """Attach an SSID to each radio its `if_names` list names."""
    for if_name in _split_if_names(if_names):
        interface_id = name_to_interface_id.get(if_name)
        if interface_id and ssid not in ssids_by_radio[(device_id, interface_id)]:
            ssids_by_radio[(device_id, interface_id)].append(ssid)


def radios_to_entities(
    tables_by_device: dict[str, dict[str, list[dict]]],
    *,
    records: dict[str, DeviceRecord],
) -> list[Entity]:
    """Map ConfigState wireless + SSID tables to Interface and WirelessLAN entities.

    `tables_by_device` maps ConfigState AssetDevice UUID -> wireless table
    buckets (`wireless_interfaces`, `wireless_states`, `ssid_configs`,
    `ssid_states`). `records` maps the same UUID to the device record, which
    supplies both the NetBox name and the site/role/device_type a nested
    Interface ``device`` ref needs to pass Diode generate-diff. Devices absent
    from `records` are skipped.

    Each radio becomes an Interface with native RF fields (`rf_role`,
    `tx_power`, `rf_channel_frequency`, `rf_channel_width`, `type`,
    `primary_mac_address`, `wireless_lans`). Each distinct SSID becomes a
    WirelessLAN (`ssid`, `status`, `auth_type`, `auth_cipher`).
    WLANs are not site-scoped: the same SSID can broadcast from APs in many
    sites. SSIDs link to radios via `AssetSsid*.if_names` and any
    `ssid_name` on wireless interface state rows.
    """
    wlans: dict[str, dict] = {}
    ssids_by_radio: dict[tuple[str, str], list[str]] = defaultdict(list)
    radio_rows: dict[tuple[str, str], dict] = {}

    for device_id, tables in tables_by_device.items():
        record = records.get(device_id)
        if record is None:
            continue
        configs = _group_by_interface_id(tables.get("wireless_interfaces") or [])
        states = _group_by_interface_id(tables.get("wireless_states") or [])

        name_to_interface_id: dict[str, str] = {}
        for interface_id in sorted(set(configs) | set(states)):
            config = _first_row(configs, interface_id, table="wireless_interfaces")
            state_rows = states.get(interface_id, [])
            name = str(config.get("name") or _primary_wireless_state(state_rows).get("name") or "").strip()
            if not name:
                continue
            name_to_interface_id[name] = interface_id
            radio_rows[(device_id, interface_id)] = {
                "record": record,
                "name": name,
                "config": config,
                "states": state_rows,
            }
            # A radio's own state rows name the SSIDs it is currently serving.
            for state_row in state_rows:
                ssid = str(state_row.get("ssid_name") or "").strip()
                if ssid and ssid not in ssids_by_radio[(device_id, interface_id)]:
                    ssids_by_radio[(device_id, interface_id)].append(ssid)
                    _ensure_wlan(wlans, ssid)

        # `enabled` only exists on ssid-config rows and `encryption` only on
        # ssid-state rows, so one pass over both covers each without either
        # overwriting the other (_ensure_wlan ORs enabled, keeps first cipher).
        ssid_configs = tables.get("ssid_configs") or []
        ssid_states = tables.get("ssid_states") or []
        encryption_by_ssid = {
            _ssid_name(row): row.get("encryption") for row in ssid_states if _ssid_name(row)
        }
        for row in (*ssid_configs, *ssid_states):
            ssid = _ssid_name(row)
            if not ssid:
                continue
            _ensure_wlan(
                wlans,
                ssid,
                enabled=row.get("enabled"),
                encryption=encryption_by_ssid.get(ssid),
            )
            _link_ssid_radios(
                device_id=device_id,
                ssid=ssid,
                if_names=row.get("if_names"),
                name_to_interface_id=name_to_interface_id,
                ssids_by_radio=ssids_by_radio,
            )

    entities = [
        Entity(
            wireless_lan=WirelessLAN(
                **_wlan_kwargs(ssid, enabled=meta.get("enabled"), encryption=meta.get("encryption")),
            ),
        )
        for ssid, meta in sorted(wlans.items())
    ]
    for (device_id, key), radio in sorted(
        radio_rows.items(),
        key=lambda item: (item[1]["record"].name or "", item[1]["name"]),
    ):
        state = _primary_wireless_state(radio["states"])
        entities.append(
            Entity(
                interface=Interface(
                    **_radio_interface_kwargs(
                        device=_device_ref(radio["record"]),
                        name=radio["name"],
                        config=radio["config"],
                        state=state,
                        ssids=ssids_by_radio.get((device_id, key), []),
                    ),
                ),
            ),
        )
    return entities
