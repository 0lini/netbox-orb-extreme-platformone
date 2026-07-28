"""Device, scope, and primary-IP transform tests."""

from __future__ import annotations

import pytest

from orb_extreme_platformone import transform
from tests.conftest import SWITCH_ASSET, cf
from tests.transform_helpers import _record, _tables


def test_devices_to_entities_builds_site_location_chain_and_device(stub_sdk) -> None:
    location = {
        "site_name": "HQ",
        "building_name": "B1",
        "floor_name": "F2",
        "site_latitude": 48.137,
        "site_longitude": 11.575,
    }
    entities = transform.devices_to_entities([_record(location=location)])

    site, building, floor, device = (e._kw for e in entities)
    assert site["site"]._kw == {"name": "HQ", "latitude": 48.137, "longitude": 11.575}
    assert building["location"]._kw["name"] == "B1"
    assert building["location"]._kw["parent"] is None
    assert floor["location"]._kw["name"] == "F2"
    assert floor["location"]._kw["parent"] is building["location"]
    assert device["device"]._kw["location"] is floor["location"]


def test_devices_to_entities_maps_the_assets_fields(stub_sdk) -> None:
    entities = transform.devices_to_entities([_record()])

    device = entities[-1]._kw["device"]._kw
    assert device["name"] == "sw-idf1"
    assert device["serial"] == "SN42"
    assert device["status"] == "active"
    assert device["site"]._kw == {"name": "Assets-Site"}
    assert device["device_type"]._kw["model"] == "5320-48P-8XE-FabricEngine"
    assert device["device_type"]._kw["manufacturer"] == "Extreme Networks"
    assert device["platform"]._kw["name"] == "Fabric Engine 9.2.1.0"
    assert device["platform"]._kw["manufacturer"] == "Extreme Networks"
    # Assets reports a bare host address; do not invent /32.
    assert "primary_ip4" not in device
    assert "primary_ip6" not in device
    assert device["role"]._kw == {"name": "Switch", "slug": "switch"}
    assert cf(device["custom_fields"]["platformone_device_id"]._kw) == "42"
    # The ConfigState UUID stays an internal join key; it is not synced.
    assert "platformone_configstate_device_id" not in device["custom_fields"]
    assert device["tags"] == ["extreme-networks", "platform-one", "discovered"]


def test_devices_to_entities_omits_name_when_hostname_missing(stub_sdk) -> None:
    asset = {**SWITCH_ASSET, "host_name": None}
    entities = transform.devices_to_entities([_record(asset=asset)])

    device = entities[-1]._kw["device"]._kw
    assert "name" not in device
    assert device["serial"] == "SN42"


def test_devices_to_entities_skips_bare_primary_ip6(stub_sdk) -> None:
    asset = {**SWITCH_ASSET, "ip_address": "2001:db8::1"}
    entities = transform.devices_to_entities([_record(asset=asset)])

    device = entities[-1]._kw["device"]._kw
    assert "primary_ip6" not in device
    assert "primary_ip4" not in device


def test_devices_to_entities_ignores_assets_ip_even_with_prefix(stub_sdk) -> None:
    """Assets OpenAPI documents a bare host; never assert Assets ip_address
    as Device primary_ip* even if a caller somehow supplies a CIDR string.
    """
    asset = {**SWITCH_ASSET, "ip_address": "10.0.0.2/24"}
    entities = transform.devices_to_entities([_record(asset=asset)])

    device = entities[-1]._kw["device"]._kw
    assert "primary_ip4" not in device
    assert "primary_ip6" not in device


def test_primary_ip_device_entities_sets_configstate_primary_ips(stub_sdk) -> None:
    entities = transform.primary_ip_device_entities(
        [_record()],
        primary_ips_by_cs_id={"cs-uuid-42": {"primary_ip4": "10.0.0.2/24"}},
    )

    assert len(entities) == 1
    device = entities[0]._kw["device"]._kw
    assert device["primary_ip4"] == "10.0.0.2/24"
    assert device["name"] == "sw-idf1"
    assert "serial" not in device


def test_primary_ips_from_tables_prefers_is_primary() -> None:
    tables = _tables(
        interface_ips=[
            {
                "asset_interface_id": "if-uuid-1",
                "address": "10.0.0.2",
                "mask_length": 24,
                "is_primary": True,
            },
            {
                "asset_interface_id": "if-other",
                "address": "10.0.0.99",
                "mask_length": 24,
                "is_primary": False,
            },
        ],
    )
    assert transform.primary_ips_from_tables(tables) == {"primary_ip4": "10.0.0.2/24"}


def test_primary_ips_from_tables_falls_back_to_management_port() -> None:
    tables = _tables(
        port_capabilities=[
            {"asset_device_id": "cs-uuid-42", "port_name": "1/1", "management_port": True},
        ],
        interface_ips=[
            {
                "asset_interface_id": "if-uuid-1",
                "address": "10.0.0.2",
                "mask_length": 24,
                "is_primary": False,
            },
            {
                "asset_interface_id": "if-other",
                "address": "10.0.0.99",
                "mask_length": 24,
            },
        ],
    )
    assert transform.primary_ips_from_tables(tables) == {"primary_ip4": "10.0.0.2/24"}


def test_primary_ips_from_tables_matches_assets_host_when_needed() -> None:
    tables = _tables(
        port_capabilities=[],
        interface_ips=[
            {
                "asset_interface_id": "if-uuid-1",
                "address": "10.0.0.2",
                "mask_length": 24,
            },
        ],
    )
    assert transform.primary_ips_from_tables(tables, asset_ip="10.0.0.2") == {"primary_ip4": "10.0.0.2/24"}


def test_primary_ips_from_tables_normalizes_assets_ipv6_host() -> None:
    """Assets may report expanded IPv6 while ConfigState uses compressed form."""
    tables = _tables(
        port_capabilities=[],
        interface_ips=[
            {
                "asset_interface_id": "if-uuid-1",
                "address": "2001:db8::2",
                "mask_length": 64,
            },
        ],
    )
    assert transform.primary_ips_from_tables(tables, asset_ip="2001:0db8:0000:0000:0000:0000:0000:0002") == {
        "primary_ip6": "2001:db8::2/64",
    }


def test_primary_ips_from_tables_skips_bare_addresses_without_mask() -> None:
    tables = _tables(
        interface_ips=[
            {"asset_interface_id": "if-uuid-1", "address": "10.0.0.2", "is_primary": True},
        ],
    )
    assert transform.primary_ips_from_tables(tables) == {}


def test_devices_to_entities_ignores_configstate_model_and_firmware(stub_sdk) -> None:
    """Device type / OS version come from Assets only — no CS model/firmware fill-in."""
    asset = {**SWITCH_ASSET, "product_type": None, "os_version": None}
    cs = {"id": "cs-uuid-42", "model_name": "FabricEngine_5520_24T", "firmware_version": "8.10.1.0"}
    entities = transform.devices_to_entities([_record(asset=asset, cs_device=cs)])

    device = entities[-1]._kw["device"]._kw
    assert "device_type" not in device
    # OS family alone may still form a platform; CS firmware must not appear.
    assert device["platform"]._kw["name"] == "Fabric Engine"


def test_devices_to_entities_non_switch_function_platform_is_version_only(stub_sdk) -> None:
    asset = {**SWITCH_ASSET, "function": "AP"}
    entities = transform.devices_to_entities([_record(asset=asset)])

    assert entities[-1]._kw["device"]._kw["platform"]._kw["name"] == "9.2.1.0"


def test_devices_to_entities_without_function_or_version_asserts_no_platform(stub_sdk) -> None:
    asset = {**SWITCH_ASSET, "function": None, "os_version": None}
    entities = transform.devices_to_entities([_record(asset=asset)])

    assert "platform" not in entities[-1]._kw["device"]._kw


@pytest.mark.parametrize("function", [None, "Unknown", "   "])
def test_devices_to_entities_omits_role_for_empty_or_unknown_function(stub_sdk, function) -> None:
    asset = {**SWITCH_ASSET, "function": function}
    entities = transform.devices_to_entities([_record(asset=asset)])

    assert "role" not in entities[-1]._kw["device"]._kw


def test_devices_to_entities_disconnected_device_is_offline(stub_sdk) -> None:
    asset = {**SWITCH_ASSET, "is_connected": False}
    entities = transform.devices_to_entities([_record(asset=asset)])

    assert entities[-1]._kw["device"]._kw["status"] == "offline"


def test_devices_to_entities_omits_status_when_is_connected_unknown(stub_sdk) -> None:
    asset = {**SWITCH_ASSET}
    del asset["is_connected"]
    entities = transform.devices_to_entities([_record(asset=asset)])

    assert "status" not in entities[-1]._kw["device"]._kw


def test_devices_to_entities_without_any_site_skips_the_device(stub_sdk, caplog) -> None:
    """Platform ONE assigns every device a site itself, so a device without
    one is unexpected: it is skipped instead of getting an invented site.
    """
    asset = {"device_id": 7, "host_name": "sw-lost", "is_connected": True}
    entities = transform.devices_to_entities([_record(asset=asset)])

    assert entities == []
    assert "sw-lost" in caplog.text


def test_scope_devices_filters_on_the_resolved_site() -> None:
    in_scope = _record(location={"site_name": "HQ"})
    out_of_scope = _record(location={"site_name": "Branch"})

    scoped = transform.scope_devices([in_scope, out_of_scope], site_scope={"HQ"})

    assert scoped == [in_scope]


def test_scope_devices_matches_site_names_case_insensitively() -> None:
    in_scope = _record(location={"site_name": "HQ"})
    assert transform.scope_devices([in_scope], site_scope={"hq"}) == [in_scope]


def test_scope_devices_without_a_scope_returns_everything() -> None:
    records = [_record(), _record(location={"site_name": "HQ"})]
    assert transform.scope_devices(records, site_scope=None) == records
