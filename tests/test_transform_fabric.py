"""Fabric (ISIS / SPBM) custom-field transform tests."""

from __future__ import annotations

from orb_extreme_platformone import bootstrap, transform
from orb_extreme_platformone.transform.fabric import device_fabric_custom_fields
from tests.conftest import cf


def test_device_fabric_custom_fields_maps_area_sys_id_and_nickname(stub_sdk):
    fields = device_fabric_custom_fields(
        {
            "isis_global_configs": [
                {
                    "manual_area_address": "00.0001.0000.00",
                    "area_name": "home",
                    "sys_id": "0010.0a0b.0c0d.00",
                    "area_vnode_nickname": "0.01.02",
                }
            ],
            "isis_global_states": [],
            "spbm_instances": [{"node_nick_name": "0.aa.bb", "instance_id": 1}],
        }
    )
    assert cf(fields[bootstrap.CF_ISIS_AREA]._kw) == "00.0001.0000.00"
    assert cf(fields[bootstrap.CF_ISIS_SYSTEM_ID]._kw) == "0010.0a0b.0c0d.00"
    assert cf(fields[bootstrap.CF_SPBM_NICKNAME]._kw) == "0.aa.bb"


def test_device_fabric_custom_fields_prefers_manual_area_over_name(stub_sdk):
    fields = device_fabric_custom_fields(
        {
            "isis_global_configs": [{"area_name": "home", "manual_area_address": ""}],
            "isis_global_states": [{"default_area_address": "00.0002.0000.00"}],
            "spbm_instances": [],
        }
    )
    assert cf(fields[bootstrap.CF_ISIS_AREA]._kw) == "home"
    assert bootstrap.CF_ISIS_SYSTEM_ID not in fields
    assert bootstrap.CF_SPBM_NICKNAME not in fields


def test_device_fabric_custom_fields_falls_back_to_state_area_and_vnode_nick(stub_sdk):
    fields = device_fabric_custom_fields(
        {
            "isis_global_configs": [{"area_vnode_nickname": "0.11.22", "sys_id": "aabb.ccdd.eeff.00"}],
            "isis_global_states": [{"dynamically_learned_area": "00.00aa.0000.00"}],
            "spbm_instances": [],
        }
    )
    assert cf(fields[bootstrap.CF_ISIS_AREA]._kw) == "00.00aa.0000.00"
    assert cf(fields[bootstrap.CF_ISIS_SYSTEM_ID]._kw) == "aabb.ccdd.eeff.00"
    assert cf(fields[bootstrap.CF_SPBM_NICKNAME]._kw) == "0.11.22"


def test_device_fabric_custom_fields_empty_when_no_fabric_data(stub_sdk):
    assert device_fabric_custom_fields({}) == {}
    assert device_fabric_custom_fields({"isis_global_configs": [{}], "spbm_instances": []}) == {}


def test_devices_to_entities_attaches_fabric_custom_fields(stub_sdk):
    fabric = device_fabric_custom_fields(
        {
            "isis_global_configs": [
                {"manual_area_address": "00.0001.0000.00", "sys_id": "0010.0a0b.0c0d.00"}
            ],
            "spbm_instances": [{"node_nick_name": "0.aa.bb"}],
        }
    )
    entities = transform.devices_to_entities(
        [
            {
                "asset": {
                    "device_id": 42,
                    "host_name": "sw-idf1",
                    "serial_number": "SN1",
                    "site_name": "HQ",
                    "function": "Fabric Engine",
                    "product_type": "5520-24X",
                    "is_connected": True,
                },
                "cs_device_id": "cs-uuid-42",
                "location": {"site_name": "HQ"},
            }
        ],
        fabric_by_cs_id={"cs-uuid-42": fabric},
    )
    device = next(e._kw["device"]._kw for e in entities if "device" in e._kw)
    cfs = device["custom_fields"]
    assert cf(cfs["platformone_device_id"]._kw) == "42"
    assert cf(cfs["platformone_isis_area"]._kw) == "00.0001.0000.00"
    assert cf(cfs["platformone_isis_system_id"]._kw) == "0010.0a0b.0c0d.00"
    assert cf(cfs["platformone_spbm_nickname"]._kw) == "0.aa.bb"
    assert "text" in cfs["platformone_isis_area"]._kw
