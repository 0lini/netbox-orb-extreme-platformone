"""Wireless radio and WLAN transform tests."""

from __future__ import annotations

import pytest

from orb_extreme_platformone import transform
from orb_extreme_platformone.backend import WIRELESS_TABLES
from tests.conftest import cf
from tests.transform_helpers import _wireless_tables


def test_wireless_entity_table_keys_match_backend_extracts() -> None:
    assert frozenset(WIRELESS_TABLES) == transform.WIRELESS_ENTITY_TABLE_KEYS


def test_radios_to_entities_maps_native_rf_fields_and_wlans(stub_sdk) -> None:
    tables = _wireless_tables(
        state={
            "band": "5GHz",
            "channel": 36,
            "channel_width": 80,
            "bssid": "aa:bb:cc:dd:ee:01",
            "power": 18,
            "radio_mode": "_11ax_5g",
            "ssid_name": "Corp",
        },
        ssid_configs=[
            {"asset_device_id": "cs-ap-1", "name": "Corp", "enabled": True, "if_names": "wifi0"},
            {"asset_device_id": "cs-ap-1", "name": "Guest", "enabled": False, "if_names": "wifi0"},
        ],
        ssid_states=[
            {"asset_device_id": "cs-ap-1", "name": "Corp", "encryption": "PSK", "if_names": "wifi0"},
            {"asset_device_id": "cs-ap-1", "name": "Guest", "encryption": "OPEN", "if_names": "wifi0"},
        ],
    )

    entities = transform.radios_to_entities(tables, device_names={"cs-ap-1": "ap-lobby"})

    wlans = {
        e._kw["wireless_lan"]._kw["ssid"]: e._kw["wireless_lan"]._kw
        for e in entities
        if "wireless_lan" in e._kw
    }
    radios = [e._kw["interface"]._kw for e in entities if "interface" in e._kw]
    assert set(wlans) == {"Corp", "Guest"}
    assert wlans["Corp"]["status"] == "active"
    assert wlans["Corp"]["auth_type"] == "wpa-personal"
    assert wlans["Guest"]["status"] == "disabled"
    assert wlans["Guest"]["auth_type"] == "open"
    assert len(radios) == 1
    radio = radios[0]
    assert radio["device"]._kw["name"] == "ap-lobby"
    assert radio["name"] == "wifi0"
    assert radio["type"] == "ieee802.11ax"
    assert radio["rf_role"] == "ap"
    assert radio["enabled"] is True
    assert radio["tx_power"] == 18
    assert radio["primary_mac_address"] == "AA:BB:CC:DD:EE:01"
    assert radio["rf_channel_frequency"] == 5180.0
    assert radio["rf_channel_width"] == 80.0
    assert radio["wireless_lans"] == ["Corp", "Guest"]
    assert cf(radio["custom_fields"]["platformone_interface_id"]._kw) == "radio-uuid-1"


def test_radios_to_entities_leaves_unverified_rf_codes_unset(stub_sdk) -> None:
    tables = _wireless_tables(
        interface_id="radio-uuid-2",
        name="wifi1",
        state={
            "band": "mystery",
            "channel": 1,
            "channel_width": 7,
            "radio_mode": "_11be_6g",
            "power": 10,
        },
    )

    radio = transform.radios_to_entities(tables, device_names={"cs-ap-1": "ap-lobby"})[0]._kw["interface"]._kw
    # NetBox 4.6 accepts ieee802.11be; RF fields require a wireless type.
    assert radio["type"] == "ieee802.11be"
    assert radio["rf_role"] == "ap"
    assert radio["tx_power"] == 10
    # mystery band / non-standard width still leave channel fields unset.
    assert "rf_channel_frequency" not in radio
    assert "rf_channel_width" not in radio


def test_radios_to_entities_omits_type_when_wireless_state_missing(stub_sdk) -> None:
    """Config-only radios omit type (and RF/WLAN links) so state degrade is safe."""
    tables = _wireless_tables(
        interface_id="radio-1",
        ssid_configs=[
            {"asset_device_id": "cs-ap-1", "name": "Corp", "enabled": True, "if_names": "wifi0"},
        ],
        ssid_states=[
            {"asset_device_id": "cs-ap-1", "name": "Corp", "encryption": "PSK", "if_names": "wifi0"},
        ],
    )
    entities = transform.radios_to_entities(tables, device_names={"cs-ap-1": "ap-lobby"})
    radio = next(e._kw["interface"]._kw for e in entities if "interface" in e._kw)
    assert "type" not in radio
    assert "rf_role" not in radio
    assert "tx_power" not in radio
    assert "wireless_lans" not in radio
    assert radio["device"]._kw["name"] == "ap-lobby"


def test_radios_to_entities_omits_type_when_state_lacks_radio_mode(stub_sdk) -> None:
    """A state row without radio_mode omits type; RF/WLAN stay gated off."""
    tables = _wireless_tables(
        interface_id="radio-1",
        state={"band": "5GHz", "channel": 36},
        ssid_configs=[
            {"asset_device_id": "cs-ap-1", "name": "Corp", "enabled": True, "if_names": "wifi0"},
        ],
        ssid_states=[
            {"asset_device_id": "cs-ap-1", "name": "Corp", "encryption": "PSK", "if_names": "wifi0"},
        ],
    )
    radio = next(
        e._kw["interface"]._kw
        for e in transform.radios_to_entities(tables, device_names={"cs-ap-1": "ap-lobby"})
        if "interface" in e._kw
    )
    assert "type" not in radio
    assert "rf_role" not in radio
    assert "wireless_lans" not in radio


def test_radios_to_entities_warns_on_duplicate_wireless_config(stub_sdk, caplog) -> None:
    """Duplicate wireless_interfaces rows share join key; first-row wins like ports."""
    base = _wireless_tables(
        interface_id="radio-1",
        state={"radio_mode": "_11ax_5g", "power": 12},
    )
    first, second = (
        {
            "asset_device_id": "cs-ap-1",
            "asset_interface_id": "radio-1",
            "name": "wifi0",
            "enabled": True,
        },
        {
            "asset_device_id": "cs-ap-1",
            "asset_interface_id": "radio-1",
            "name": "wifi0",
            "enabled": False,
        },
    )
    base["cs-ap-1"]["wireless_interfaces"] = [first, second]

    entities = transform.radios_to_entities(base, device_names={"cs-ap-1": "ap-lobby"})

    radio = next(e._kw["interface"]._kw for e in entities if "interface" in e._kw)
    assert radio["enabled"] is True
    assert "Multiple wireless_interfaces rows share join key" in caplog.text


def test_radios_to_entities_uses_first_nonempty_state_for_rf(stub_sdk) -> None:
    """Empty leading state rows must not hide name/RF fields from a later state row."""
    tables = {
        "cs-ap-1": {
            "wireless_interfaces": [
                {
                    "asset_device_id": "cs-ap-1",
                    "asset_interface_id": "radio-1",
                    "enabled": True,
                },
            ],
            "wireless_states": [
                {},
                {
                    "asset_device_id": "cs-ap-1",
                    "asset_interface_id": "radio-1",
                    "name": "wifi0",
                    "radio_mode": "_11ax_5g",
                    "power": 18,
                    "ssid_name": "Corp",
                },
            ],
            "ssid_configs": [],
            "ssid_states": [],
        },
    }
    radio = next(
        e._kw["interface"]._kw
        for e in transform.radios_to_entities(tables, device_names={"cs-ap-1": "ap-lobby"})
        if "interface" in e._kw
    )
    assert radio["name"] == "wifi0"
    assert radio["type"] == "ieee802.11ax"
    assert radio["tx_power"] == 18
    assert radio["wireless_lans"] == ["Corp"]


def test_radios_to_entities_enriches_nested_device_ref(stub_sdk) -> None:
    tables = _wireless_tables(interface_id="radio-1", state={"radio_mode": "_11ax_5g"})
    radio = (
        transform.radios_to_entities(
            tables,
            device_names={"cs-ap-1": "ap-lobby"},
            device_meta={
                "cs-ap-1": {
                    "site_name": "HQ",
                    "function": "AP",
                    "product_type": "AP5050U",
                },
            },
        )[0]
        ._kw["interface"]
        ._kw
    )
    device = radio["device"]._kw
    assert device["name"] == "ap-lobby"
    assert device["site"]._kw["name"] == "HQ"
    assert device["role"]._kw["name"] == "Wireless AP"
    assert device["device_type"]._kw["model"] == "AP5050U"


def test_radios_to_entities_skips_devices_missing_from_device_names(stub_sdk) -> None:
    tables = _wireless_tables(interface_id="r1", enabled=None)
    assert transform.radios_to_entities(tables, device_names={}) == []


@pytest.mark.parametrize(
    ("band", "channel", "radio_mode", "expected_mhz"),
    [
        ("BAND_5_GHZ", 36, "_11ax_5g", 5180.0),
        ("BAND_2_4_GHZ", 1, "_11ax_2g", 2412.0),
    ],
)
def test_radios_to_entities_accepts_band_enum_style_labels(
    stub_sdk, band, channel, radio_mode, expected_mhz,
) -> None:
    tables = _wireless_tables(
        state={"band": band, "channel": channel, "channel_width": 20, "radio_mode": radio_mode},
    )
    radio = transform.radios_to_entities(tables, device_names={"cs-ap-1": "ap-lobby"})[0]._kw["interface"]._kw
    assert radio["rf_channel_frequency"] == expected_mhz


@pytest.mark.parametrize(
    ("encryption", "auth_type", "auth_cipher"),
    [
        ("OPEN", "open", "auto"),
        ("PSK", "wpa-personal", "auto"),
        ("WPA-PSK", "wpa-personal", "auto"),
        ("WPA", "wpa-personal", "tkip"),
        ("TYPE_WPA", "wpa-personal", "tkip"),
        ("WPA2-PSK", "wpa-personal", "aes"),
        ("TYPE_802DOT1X", "wpa-enterprise", "auto"),
        ("WEP", "wep", "wep"),
    ],
)
def test_radios_to_entities_maps_ssid_encryption_to_auth(
    stub_sdk, encryption, auth_type, auth_cipher,
) -> None:
    ssid_state = {"asset_device_id": "cs-ap-1", "name": "Corp", "if_names": "wifi0", "encryption": encryption}
    tables = {
        "cs-ap-1": {
            "wireless_interfaces": [],
            "wireless_states": [],
            "ssid_configs": [{"asset_device_id": "cs-ap-1", "name": "Corp", "enabled": True}],
            "ssid_states": [ssid_state],
        },
    }
    wlan = (
        transform.radios_to_entities(tables, device_names={"cs-ap-1": "ap-lobby"})[0]._kw["wireless_lan"]._kw
    )
    assert wlan["auth_type"] == auth_type
    assert wlan["auth_cipher"] == auth_cipher


def test_radios_to_entities_omits_auth_and_status_when_unknown(stub_sdk) -> None:
    """Missing enabled/encryption must not invent active/open/auto."""
    tables = {
        "cs-ap-1": {
            "wireless_interfaces": [],
            "wireless_states": [],
            "ssid_configs": [{"asset_device_id": "cs-ap-1", "name": "Corp", "if_names": "wifi0"}],
            "ssid_states": [{"asset_device_id": "cs-ap-1", "name": "Corp"}],
        },
    }

    wlan = (
        transform.radios_to_entities(tables, device_names={"cs-ap-1": "ap-lobby"})[0]._kw["wireless_lan"]._kw
    )
    assert wlan["ssid"] == "Corp"
    assert "status" not in wlan
    assert "auth_type" not in wlan
    assert "auth_cipher" not in wlan


def test_radios_to_entities_omits_auth_for_unrecognized_encryption(stub_sdk) -> None:
    tables = {
        "cs-ap-1": {
            "wireless_interfaces": [],
            "wireless_states": [],
            "ssid_configs": [{"asset_device_id": "cs-ap-1", "name": "Corp", "enabled": True}],
            "ssid_states": [{"asset_device_id": "cs-ap-1", "name": "Corp", "encryption": "MYSTERY_SUITE"}],
        },
    }
    wlan = (
        transform.radios_to_entities(tables, device_names={"cs-ap-1": "ap-lobby"})[0]._kw["wireless_lan"]._kw
    )
    assert wlan["status"] == "active"
    assert "auth_type" not in wlan
    assert "auth_cipher" not in wlan


def test_radios_to_entities_merges_ssid_enabled_across_aps(stub_sdk, caplog) -> None:
    """Same SSID on two APs: enabled is OR'd; conflicting encryption keeps first."""
    tables = {
        "cs-ap-1": {
            "wireless_interfaces": [],
            "wireless_states": [],
            "ssid_configs": [
                {"asset_device_id": "cs-ap-1", "name": "Guest", "enabled": False, "if_names": "wifi0"},
            ],
            "ssid_states": [
                {"asset_device_id": "cs-ap-1", "name": "Guest", "encryption": "OPEN", "if_names": "wifi0"},
            ],
        },
        "cs-ap-2": {
            "wireless_interfaces": [],
            "wireless_states": [],
            "ssid_configs": [
                {"asset_device_id": "cs-ap-2", "name": "Guest", "enabled": True, "if_names": "wifi0"},
            ],
            "ssid_states": [
                {"asset_device_id": "cs-ap-2", "name": "Guest", "encryption": "PSK", "if_names": "wifi0"},
            ],
        },
    }
    wlan = (
        transform.radios_to_entities(tables, device_names={"cs-ap-1": "ap-1", "cs-ap-2": "ap-2"})[0]
        ._kw["wireless_lan"]
        ._kw
    )
    assert wlan["ssid"] == "Guest"
    assert wlan["status"] == "active"
    assert wlan["auth_type"] == "open"
    assert "Conflicting encryption for SSID" in caplog.text
