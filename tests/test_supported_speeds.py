"""Supported-speeds capability mapping tests."""

from __future__ import annotations

import json

from orb_extreme_platformone import bootstrap, transform
from orb_extreme_platformone.transform.supported_speeds import (
    map_speed_duplex_token,
    supported_speeds_custom_fields,
    supported_speeds_payload,
)
from tests.conftest import PORT_CONFIG, PORT_STATE, cf
from tests.transform_helpers import _tables


def test_map_speed_duplex_token_accepts_common_enum_forms():
    assert map_speed_duplex_token("SPEED_1000_FULL") == "1G-full"
    assert map_speed_duplex_token("speed_100_half") == "100M-half"
    assert map_speed_duplex_token("1000FULL") == "1G-full"
    assert map_speed_duplex_token("1G-full") == "1G-full"
    assert map_speed_duplex_token("10G/FULL") == "10G-full"
    assert map_speed_duplex_token("2.5G_FULL") == "2.5G-full"


def test_map_speed_duplex_token_rejects_unknown_and_empty():
    assert map_speed_duplex_token(None) is None
    assert map_speed_duplex_token("") is None
    assert map_speed_duplex_token("BOGUS") is None
    assert map_speed_duplex_token("999G-full") is None


def test_supported_speeds_payload_splits_forced_and_advertised():
    payload = supported_speeds_payload(
        {
            "auto_neg_off_supported_speed_duplex_list": ["SPEED_100_FULL", "SPEED_1000_FULL", "NOPE"],
            "auto_neg_on_supported_adv_list": ["10M-half", "SPEED_1000_FULL", "SPEED_1000_FULL"],
        }
    )
    assert payload == {
        "forced": ["100M-full", "1G-full"],
        "advertised": ["10M-half", "1G-full"],
    }


def test_supported_speeds_payload_empty_when_unmapped(stub_sdk):
    assert supported_speeds_payload(None) is None
    assert supported_speeds_payload({"management_port": True}) is None
    assert supported_speeds_custom_fields({"auto_neg_off_supported_speed_duplex_list": ["x"]}) == {}


def test_ports_to_entities_attaches_supported_speeds_json_cf(stub_sdk):
    caps = [
        {
            "asset_device_id": "cs-uuid-42",
            "port_name": "1/1",
            "management_port": False,
            "auto_neg_off_supported_speed_duplex_list": ["100FULL", "1000FULL"],
            "auto_neg_on_supported_adv_list": ["10HALF", "100FULL", "1000FULL"],
        }
    ]
    port = (
        transform.ports_to_entities(
            _tables(
                port_configs=[PORT_CONFIG],
                port_states=[PORT_STATE],
                vlan_properties=[],
                port_capabilities=caps,
            ),
            device="sw-idf1",
        )[0]
        ._kw["interface"]
        ._kw
    )
    # Native speed stays operational kbps from oper_speed.
    assert port["speed"] == 1_000_000
    raw = port["custom_fields"][bootstrap.CF_SUPPORTED_SPEEDS]._kw["json"]
    assert json.loads(raw) == {
        "advertised": ["10M-half", "100M-full", "1G-full"],
        "forced": ["100M-full", "1G-full"],
    }
    assert cf(port["custom_fields"]["platformone_interface_id"]._kw) == "if-uuid-1"
