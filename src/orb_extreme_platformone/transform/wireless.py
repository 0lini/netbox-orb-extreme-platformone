"""AP radio and WirelessLAN mapping."""

from __future__ import annotations

from collections import defaultdict

from netboxlabs.diode.sdk.ingester import Entity, Interface, WirelessLAN

from orb_extreme_platformone.extract.tables import WIRELESS_TABLES

from .common import _coerce_int, _device_ref, _interface_identity_kwargs, _normalized_mac
from .port_join import _by_key, _first_row
from .wireless_auth import _ensure_wlan, _wlan_kwargs
from .wireless_rf import (
    _channel_frequency_mhz,
    _channel_width_mhz,
    _is_wireless_interface_type,
    _radio_type,
)

# Keys `radios_to_entities` reads — derived from the extract catalog.
WIRELESS_ENTITY_TABLE_KEYS = frozenset(WIRELESS_TABLES)


def _split_if_names(value) -> list[str]:
    """Normalize AssetSsid*.if_names (OpenAPI string) into interface names.

    Accepts a single name, a comma-separated string, or a list. No speculative
    JSON / alternate-separator parsing.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if "," in text:
        return [part.strip() for part in text.split(",") if part.strip()]
    return [text]


def _wireless_radio_key(row: dict) -> str | None:
    """Join key: required ``asset_interface_id`` on wireless-interface rows."""
    interface_id = str(row.get("asset_interface_id") or "").strip()
    return interface_id or None


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
    device,
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
    name_to_key: dict[str, str],
    ssids_by_radio: dict[tuple[str, str], list[str]],
) -> None:
    for if_name in _split_if_names(if_names):
        radio_key = name_to_key.get(if_name)
        if radio_key and ssid not in ssids_by_radio[(device_id, radio_key)]:
            ssids_by_radio[(device_id, radio_key)].append(ssid)


def radios_to_entities(
    tables_by_device: dict[str, dict[str, list[dict]]],
    *,
    device_names: dict[str, str],
    device_meta: dict[str, dict] | None = None,
) -> list[Entity]:
    """Map ConfigState wireless + SSID tables to Interface and WirelessLAN entities.

    `tables_by_device` maps ConfigState AssetDevice UUID -> wireless table
    buckets (`wireless_interfaces`, `wireless_states`, `ssid_configs`,
    `ssid_states`). `device_names` maps the same UUID to the NetBox device
    name already used for Device entities. Optional `device_meta` supplies
    per-device ``site_name`` / ``function`` / ``product_type`` so nested
    Interface ``device`` refs pass Diode generate-diff (same as switch ports).

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
    device_meta = device_meta or {}

    for device_id, tables in tables_by_device.items():
        if device_id not in device_names:
            continue
        configs = tables.get("wireless_interfaces") or []
        states = tables.get("wireless_states") or []
        ssid_configs = tables.get("ssid_configs") or []
        ssid_states = tables.get("ssid_states") or []

        radios: dict[str, dict] = {}
        configs_by_key = _by_key(configs)
        for key in configs_by_key:
            radios.setdefault(key, {"config": {}, "states": []})["config"] = _first_row(
                configs_by_key,
                key,
                table="wireless_interfaces",
            )
        for row in states:
            key = _wireless_radio_key(row)
            if not key:
                continue
            radios.setdefault(key, {"config": {}, "states": []})["states"].append(row)

        name_to_key: dict[str, str] = {}
        for key, radio in radios.items():
            config = radio["config"]
            state = _primary_wireless_state(radio["states"])
            name = str(config.get("name") or state.get("name") or "").strip()
            if not name:
                continue
            name_to_key[name] = key
            radio_rows[(device_id, key)] = {
                "device": device_names[device_id],
                "name": name,
                "config": config,
                "states": radio["states"],
            }
            for state_row in radio["states"]:
                ssid = str(state_row.get("ssid_name") or "").strip()
                if ssid and ssid not in ssids_by_radio[(device_id, key)]:
                    ssids_by_radio[(device_id, key)].append(ssid)
                    _ensure_wlan(wlans, ssid)

        encryption_by_ssid = {
            _ssid_name(row): row.get("encryption") for row in ssid_states if _ssid_name(row)
        }
        for row in ssid_configs:
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
                name_to_key=name_to_key,
                ssids_by_radio=ssids_by_radio,
            )
        for row in ssid_states:
            ssid = _ssid_name(row)
            if not ssid:
                continue
            _ensure_wlan(wlans, ssid, encryption=row.get("encryption"))
            _link_ssid_radios(
                device_id=device_id,
                ssid=ssid,
                if_names=row.get("if_names"),
                name_to_key=name_to_key,
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
        key=lambda item: (item[1]["device"], item[1]["name"]),
    ):
        state = _primary_wireless_state(radio["states"])
        meta = device_meta.get(device_id) or {}
        device_ref = _device_ref(
            name=radio["device"],
            site_name=meta.get("site_name"),
            function=meta.get("function"),
            product_type=meta.get("product_type"),
        )
        entities.append(
            Entity(
                interface=Interface(
                    **_radio_interface_kwargs(
                        device=device_ref,
                        name=radio["name"],
                        config=radio["config"],
                        state=state,
                        ssids=ssids_by_radio.get((device_id, key), []),
                    ),
                ),
            ),
        )
    return entities
