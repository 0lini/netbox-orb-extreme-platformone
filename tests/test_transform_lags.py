"""LAG-focused ports_to_entities transform tests."""

from __future__ import annotations

from orb_extreme_platformone import transform
from tests.conftest import PORT_CONFIG, cf
from tests.transform_helpers import _tables

LAG_CONFIG = {
    "id": "lag-cfg-1",
    "asset_device_id": "cs-uuid-42",
    "asset_interface_id": "lag-if-1",
    "lag_number": "1",
    "name": "lag1",
    "enabled": True,
    "member_ports": [
        {"asset_lag_config_id": "lag-cfg-1", "interface_name": "1/1"},
        {"asset_lag_config_id": "lag-cfg-1", "interface_name": "1/2"},
    ],
}

LAG_STATE = {
    "id": "lag-st-1",
    "asset_device_id": "cs-uuid-42",
    "asset_interface_id": "lag-if-1",
    "lag_number": "1",
    "name": "lag1",
}


def test_ports_to_entities_maps_lag_parent_and_member_refs(stub_sdk):
    port2 = {**PORT_CONFIG, "asset_interface_id": "if-uuid-2", "name": "1/2"}
    entities = transform.ports_to_entities(
        _tables(
            port_configs=[PORT_CONFIG, port2],
            port_states=[],
            vlan_properties=[],
            lag_configs=[LAG_CONFIG],
            lag_states=[LAG_STATE],
        ),
        device="sw-idf1",
    )

    ports = {e._kw["interface"]._kw["name"]: e._kw["interface"]._kw for e in entities}
    assert [e._kw["interface"]._kw["name"] for e in entities][0] == "lag1"
    assert ports["lag1"]["type"] == "lag"
    assert ports["lag1"]["enabled"] is True
    assert cf(ports["lag1"]["custom_fields"]["platformone_interface_id"]._kw) == "lag-if-1"
    assert ports["1/1"]["lag"]._kw["name"] == "lag1"
    assert ports["1/1"]["lag"]._kw["device"]._kw["name"] == "sw-idf1"
    assert ports["1/2"]["lag"]._kw["name"] == "lag1"
    assert ports["1/2"]["lag"]._kw["device"]._kw["name"] == "sw-idf1"


def test_ports_to_entities_skips_lag_without_name(stub_sdk):
    """Switches auto-generate LAG names; do not invent lag-{n} from lag_number."""
    lag = {**LAG_CONFIG, "name": None, "member_ports": []}
    entities = transform.ports_to_entities(
        _tables(port_configs=[], port_states=[], vlan_properties=[], lag_configs=[lag], lag_states=[]),
        device="sw-idf1",
    )

    assert entities == []


def test_ports_to_entities_uses_state_lag_name_for_config_members(stub_sdk):
    """Membership comes from lag-config; name may live only on paired lag-state."""
    lag_config = {**LAG_CONFIG, "name": None}
    lag_state = {
        "asset_device_id": "cs-uuid-42",
        "asset_interface_id": "lag-if-1",
        "name": "state-lag",
        "member_ports": [],
    }
    port2 = {**PORT_CONFIG, "asset_interface_id": "if-uuid-2", "name": "1/2"}
    entities = transform.ports_to_entities(
        _tables(
            port_configs=[PORT_CONFIG, port2],
            port_states=[],
            vlan_properties=[],
            lag_configs=[lag_config],
            lag_states=[lag_state],
        ),
        device="sw-idf1",
    )

    ports = {e._kw["interface"]._kw["name"]: e._kw["interface"]._kw for e in entities}
    assert ports["state-lag"]["type"] == "lag"
    assert ports["1/1"]["lag"]._kw["name"] == "state-lag"
    assert ports["1/2"]["lag"]._kw["name"] == "state-lag"


def test_ports_to_entities_uses_state_member_ports_when_config_omits_them(stub_sdk):
    """When lag-config has no member_ports, fall back to lag-state members."""
    lag_config = {**LAG_CONFIG, "member_ports": []}
    lag_state = {
        "asset_device_id": "cs-uuid-42",
        "asset_interface_id": "lag-if-1",
        "name": "lag1",
        "member_ports": [
            {"interface_name": "1/1"},
            {"interface_name": "1/2"},
        ],
    }
    port2 = {**PORT_CONFIG, "asset_interface_id": "if-uuid-2", "name": "1/2"}
    entities = transform.ports_to_entities(
        _tables(
            port_configs=[PORT_CONFIG, port2],
            port_states=[],
            vlan_properties=[],
            lag_configs=[lag_config],
            lag_states=[lag_state],
        ),
        device="sw-idf1",
    )
    ports = {e._kw["interface"]._kw["name"]: e._kw["interface"]._kw for e in entities}
    assert ports["1/1"]["lag"]._kw["name"] == "lag1"
    assert ports["1/2"]["lag"]._kw["name"] == "lag1"


def test_ports_to_entities_warns_on_dual_lag_membership(stub_sdk, caplog):
    lag_a = {**LAG_CONFIG, "name": "lag-a", "member_ports": [{"interface_name": "1/1"}]}
    lag_b = {
        **LAG_CONFIG,
        "asset_interface_id": "lag-if-2",
        "name": "lag-b",
        "member_ports": [{"interface_name": "1/1"}],
    }
    entities = transform.ports_to_entities(
        _tables(
            port_states=[],
            vlan_properties=[],
            lag_configs=[lag_a, lag_b],
            lag_states=[],
        ),
        device="sw-idf1",
    )
    ports = {e._kw["interface"]._kw["name"]: e._kw["interface"]._kw for e in entities}
    assert ports["1/1"]["lag"]._kw["name"] == "lag-a"
    assert "listed as member of both" in caplog.text


def test_ports_to_entities_emits_duplicate_port_when_lag_is_unnamed(stub_sdk):
    """Unnamed LAG must not suppress a port-table row that shares its interface id."""
    lag = {**LAG_CONFIG, "name": None, "member_ports": []}
    lag_as_port = {
        "asset_device_id": "cs-uuid-42",
        "asset_interface_id": "lag-if-1",
        "name": "port-table-lag",
        "enabled": True,
    }
    entities = transform.ports_to_entities(
        _tables(
            port_configs=[lag_as_port],
            port_states=[],
            vlan_properties=[],
            lag_configs=[lag],
            lag_states=[],
        ),
        device="sw-idf1",
    )

    ports = [e._kw["interface"]._kw for e in entities]
    assert len(ports) == 1
    assert ports[0]["name"] == "port-table-lag"
    # Unnamed LAG is skipped; duplicate port-config row syncs without state type.
    assert "type" not in ports[0]


def test_ports_to_entities_skips_lag_members_without_port_rows(stub_sdk):
    """LAG membership alone does not invent stub member Interfaces."""
    entities = transform.ports_to_entities(
        _tables(
            port_configs=[],
            port_states=[],
            vlan_properties=[],
            lag_configs=[LAG_CONFIG],
            lag_states=[],
        ),
        device="sw-idf1",
    )

    ports = {e._kw["interface"]._kw["name"]: e._kw["interface"]._kw for e in entities}
    assert set(ports) == {"lag1"}


def test_ports_to_entities_skips_lag_row_duplicated_in_port_tables(stub_sdk):
    """If AssetPortConfig also returns the LAG's asset_interface_id, emit type=lag once."""
    lag_as_port = {
        "asset_device_id": "cs-uuid-42",
        "asset_interface_id": "lag-if-1",
        "name": "lag1",
        "enabled": True,
        "description": "core lag",
    }
    lag_as_state = {
        "asset_device_id": "cs-uuid-42",
        "asset_interface_id": "lag-if-1",
        "name": "lag1",
        "oper_state": 1,
        "mac_address": "aa:bb:cc:dd:ee:99",
        "oper_speed": 4,
        "oper_duplex": 1,
        "connector_type": 1,
    }
    entities = transform.ports_to_entities(
        _tables(
            port_configs=[lag_as_port],
            port_states=[lag_as_state],
            vlan_properties=[],
            lag_configs=[{**LAG_CONFIG, "member_ports": []}],
            lag_states=[],
        ),
        device="sw-idf1",
    )

    ports = [e._kw["interface"]._kw for e in entities]
    assert len(ports) == 1
    assert ports[0]["name"] == "lag1"
    assert ports[0]["type"] == "lag"
    assert ports[0]["description"] == "core lag"
    # NetBox rejects mark_connected on type=lag; omit it so the LAG applies.
    assert "mark_connected" not in ports[0]
    assert ports[0]["primary_mac_address"] == "AA:BB:CC:DD:EE:99"
    assert "untagged_vlan" not in ports[0]
    assert "mode" not in ports[0]
    assert "speed" not in ports[0]
    assert "duplex" not in ports[0]


def test_ports_to_entities_lag_applies_vlan_trunk_from_vlan_properties(stub_sdk):
    """Trunk VLANs on the LAG parent come from vlan-properties on its interface id."""
    vlan_on_lag = {
        "asset_interface_id": "lag-if-1",
        "port_vlan": 10,
        "vlans": [{"vlan_number": 10}, {"vlan_number": 20}, {"vlan_number": 30}],
    }
    entities = transform.ports_to_entities(
        _tables(
            port_configs=[],
            port_states=[],
            vlan_properties=[vlan_on_lag],
            lag_configs=[{**LAG_CONFIG, "member_ports": []}],
            lag_states=[],
        ),
        device="sw-idf1",
    )

    lag = entities[0]._kw["interface"]._kw
    assert lag["type"] == "lag"
    assert lag["mode"] == "tagged"
    assert lag["untagged_vlan"]._kw["vid"] == 10
    assert [v._kw["vid"] for v in lag["tagged_vlans"]] == [20, 30]


def test_ports_to_entities_lag_ignores_false_enabled_from_lag_config(stub_sdk):
    """AssetLagConfig.enabled is false in production for in-service MLTs.

    Diode maps omitted/false onto NetBox disabled; assert admin-up unless a
    duplicate AssetPortConfig row says otherwise.
    """
    lag = {**LAG_CONFIG, "enabled": False, "member_ports": []}
    entities = transform.ports_to_entities(
        _tables(port_configs=[], port_states=[], vlan_properties=[], lag_configs=[lag], lag_states=[]),
        device="sw-idf1",
    )
    assert entities[0]._kw["interface"]._kw["enabled"] is True


def test_ports_to_entities_lag_enabled_follows_duplicate_port_config(stub_sdk):
    """When port tables also list the LAG interface id, prefer that admin state."""
    lag = {**LAG_CONFIG, "enabled": True, "member_ports": []}
    lag_as_port = {
        "asset_device_id": "cs-uuid-42",
        "asset_interface_id": "lag-if-1",
        "name": "lag1",
        "enabled": False,
    }
    entities = transform.ports_to_entities(
        _tables(
            port_configs=[lag_as_port],
            port_states=[],
            vlan_properties=[],
            lag_configs=[lag],
            lag_states=[],
        ),
        device="sw-idf1",
    )
    assert entities[0]._kw["interface"]._kw["enabled"] is False


def test_ports_to_entities_lag_vlan_joins_on_asset_interface_id(stub_sdk):
    """VLAN rows attach to the LAG only via asset_interface_id (always present)."""
    vlan_on_lag = {
        "asset_interface_id": "lag-if-1",
        "interface_name": "lag1",
        "port_vlan": 10,
        "vlans": [{"vlan_number": 10}, {"vlan_number": 20}],
    }
    entities = transform.ports_to_entities(
        _tables(
            port_configs=[],
            port_states=[],
            vlan_properties=[vlan_on_lag],
            lag_configs=[{**LAG_CONFIG, "member_ports": []}],
            lag_states=[],
        ),
        device="sw-idf1",
    )
    lag = entities[0]._kw["interface"]._kw
    assert lag["mode"] == "tagged"
    assert lag["untagged_vlan"]._kw["vid"] == 10
    assert [v._kw["vid"] for v in lag["tagged_vlans"]] == [20]


def test_ports_to_entities_ignores_vlan_rows_without_asset_interface_id(stub_sdk):
    """Name-only vlan-properties rows are not joined (asset_interface_id is required)."""
    vlan_name_only = {
        "interface_name": "lag1",
        "port_vlan": 10,
        "vlans": [{"vlan_number": 10}, {"vlan_number": 20}],
    }
    entities = transform.ports_to_entities(
        _tables(
            port_configs=[],
            port_states=[],
            vlan_properties=[vlan_name_only],
            lag_configs=[{**LAG_CONFIG, "member_ports": []}],
            lag_states=[],
        ),
        device="sw-idf1",
    )
    lag = entities[0]._kw["interface"]._kw
    assert "mode" not in lag
    assert "untagged_vlan" not in lag
    assert "tagged_vlans" not in lag


def test_ports_to_entities_lag_joins_poe_and_ip_like_physical_ports(stub_sdk):
    """PoE + IP joins use the LAG's asset_interface_id the same way as ports."""
    poe_state = {"asset_interface_id": "lag-if-1", "supported": True}
    ips = [
        {
            "asset_interface_id": "lag-if-1",
            "address": "10.0.0.1",
            "mask_length": 24,
        }
    ]
    entities = transform.ports_to_entities(
        _tables(
            port_configs=[],
            port_states=[],
            vlan_properties=[],
            lag_configs=[{**LAG_CONFIG, "member_ports": []}],
            lag_states=[],
            poe_states=[poe_state],
            interface_ips=ips,
        ),
        device="sw-idf1",
    )

    interfaces = [e._kw["interface"]._kw for e in entities if "interface" in e._kw]
    ips_out = [e._kw["ip_address"]._kw for e in entities if "ip_address" in e._kw]
    assert interfaces[0]["type"] == "lag"
    assert interfaces[0]["poe_mode"] == "pse"
    assert cf(interfaces[0]["custom_fields"]["platformone_interface_id"]._kw) == "lag-if-1"
    assert "lag_number" not in interfaces[0].get("custom_fields", {})
    assert ips_out[0]["address"] == "10.0.0.1/24"
    assert ips_out[0]["assigned_object_interface"]._kw["name"] == "lag1"


def test_ports_to_entities_ignores_unmapped_lacp_fields_on_lag(stub_sdk):
    """LACP mode/key/algo have no Diode target; do not invent description or mode."""
    lag = {
        **LAG_CONFIG,
        "member_ports": [],
        "mode": 2,
        "lacp_key": "100",
        "load_balance_algo": 1,
        "dynamic": True,
    }
    entities = transform.ports_to_entities(
        _tables(port_configs=[], port_states=[], vlan_properties=[], lag_configs=[lag], lag_states=[]),
        device="sw-idf1",
    )

    kwargs = entities[0]._kw["interface"]._kw
    assert kwargs["type"] == "lag"
    assert "mode" not in kwargs  # 802.1Q mode only; not LACP mode
    assert "description" not in kwargs
    assert "lacp_key" not in kwargs
