"""Physical port, VLAN, PoE, duplex, and interface-IP transform tests."""

from __future__ import annotations

from orb_extreme_platformone import transform
from orb_extreme_platformone.backend import INTERFACE_ID_TABLES, PORT_TABLES
from tests.conftest import PORT_CONFIG, PORT_STATE, VLAN_PROPERTIES, cf
from tests.transform_helpers import _tables


def test_port_entity_table_keys_match_backend_extracts():
    """Transform port keys must stay aligned with backend PORT_TABLES + INTERFACE_ID_TABLES."""
    assert frozenset(PORT_TABLES) | frozenset(INTERFACE_ID_TABLES) == transform.PORT_ENTITY_TABLE_KEYS


def test_ports_to_entities_warns_on_duplicate_first_row_join(stub_sdk, caplog):
    dup = {**PORT_CONFIG, "enabled": False}
    entities = transform.ports_to_entities(
        _tables(port_configs=[PORT_CONFIG, dup], vlan_properties=[]),
        device="sw-idf1",
    )

    assert entities[0]._kw["interface"]._kw["enabled"] is True
    assert "Multiple port_configs rows share join key" in caplog.text


def test_ports_to_entities_maps_config_state_and_vlans_onto_one_interface(stub_sdk):
    entities = transform.ports_to_entities(_tables(), device="sw-idf1")

    assert len(entities) == 1
    port = entities[0]._kw["interface"]._kw
    assert port["device"]._kw["name"] == "sw-idf1"
    assert port["name"] == "1/1"
    assert port["enabled"] is True
    assert port["mark_connected"] is True
    assert port["speed"] == 1_000_000
    assert port["duplex"] == "full"
    assert port["type"] == "1000base-t"
    assert port["description"] == "uplink to core"
    assert port["primary_mac_address"] == "AA:BB:CC:DD:EE:01"
    assert port["untagged_vlan"]._kw == {"vid": 10, "name": "10"}
    assert [v._kw for v in port["tagged_vlans"]] == [
        {"vid": 20, "name": "20"},
        {"vid": 30, "name": "30"},
    ]
    assert port["mode"] == "tagged"
    assert cf(port["custom_fields"]["platformone_interface_id"]._kw) == "if-uuid-1"


def test_ports_to_entities_config_only_port_still_syncs_admin_state(stub_sdk):
    """No port-state: admin state syncs; type omitted so degrade cannot invent other."""
    entities = transform.ports_to_entities(_tables(port_states=[], vlan_properties=[]), device="sw-idf1")

    port = entities[0]._kw["interface"]._kw
    assert port["enabled"] is True
    assert "mark_connected" not in port
    assert "speed" not in port
    assert "type" not in port


def test_ports_to_entities_state_only_port_still_syncs_link_state(stub_sdk):
    """No port-config row: link state still maps; enabled defaults admin-up.

    Diode maps an omitted bool to false, so leaving enabled unset would invent
    admin-down (same reason LAG parents always assert an explicit bool).
    """
    down = {**PORT_STATE, "oper_state": 2}
    entities = transform.ports_to_entities(
        _tables(port_configs=[], port_states=[down], vlan_properties=[]), device="sw-idf1"
    )

    port = entities[0]._kw["interface"]._kw
    assert port["mark_connected"] is False
    assert port["enabled"] is True


def test_ports_to_entities_admin_down_and_link_down_are_independent(stub_sdk):
    config = {**PORT_CONFIG, "enabled": False}
    state = {**PORT_STATE, "oper_state": 2}
    entities = transform.ports_to_entities(
        _tables(port_configs=[config], port_states=[state], vlan_properties=[]), device="sw-idf1"
    )

    port = entities[0]._kw["interface"]._kw
    assert port["enabled"] is False
    assert port["mark_connected"] is False


def test_ports_to_entities_unverified_enum_codes_default_type_other(stub_sdk):
    """ConfigState's integer enums have no published value table; codes not
    verified against a real device must not map to speed/duplex, but NetBox
    requires Interface.type so unknown ports fall back to ``other``."""
    state = {**PORT_STATE, "oper_speed": 7, "oper_duplex": 9, "connector_type": 3}
    entities = transform.ports_to_entities(_tables(port_states=[state], vlan_properties=[]), device="sw-idf1")

    port = entities[0]._kw["interface"]._kw
    assert "speed" not in port
    assert "duplex" not in port
    assert port["type"] == "other"


def test_ports_to_entities_fiber_gig_port_maps_to_sfp_type(stub_sdk):
    state = {**PORT_STATE, "connector_type": 2}
    entities = transform.ports_to_entities(_tables(port_states=[state], vlan_properties=[]), device="sw-idf1")

    assert entities[0]._kw["interface"]._kw["type"] == "1000base-x-sfp"


def test_ports_to_entities_accepts_string_oper_enum_codes(stub_sdk):
    """JSON string codes must still map speed/type/link (same as mask_length)."""
    state = {
        **PORT_STATE,
        "oper_state": "1",
        "oper_speed": "4",
        "connector_type": "1",
        "oper_duplex": "2",
    }
    port = (
        transform.ports_to_entities(_tables(port_states=[state], vlan_properties=[]), device="sw-idf1")[0]
        ._kw["interface"]
        ._kw
    )
    assert port["mark_connected"] is True
    assert port["speed"] == 1_000_000
    assert port["type"] == "1000base-t"
    assert port["duplex"] == "full"


def test_ports_to_entities_maps_mgmt_only_from_capabilities(stub_sdk):
    caps = [
        {
            "asset_device_id": "cs-uuid-42",
            "port_name": "1/1",
            "management_port": True,
        }
    ]
    entities = transform.ports_to_entities(
        _tables(vlan_properties=[], port_capabilities=caps), device="sw-idf1"
    )

    assert entities[0]._kw["interface"]._kw["mgmt_only"] is True


def test_ports_to_entities_capabilities_scoped_per_device(stub_sdk, caplog):
    """Same port_name on two devices must not share a capability row.

    A mixed capabilities list (as if backend bucketing were skipped) still
    joins each port to its own asset_device_id; the other device's
    management_port must not leak across.
    """
    caps = [
        {"asset_device_id": "cs-uuid-OTHER", "port_name": "1/1", "management_port": True},
        {"asset_device_id": "cs-uuid-42", "port_name": "1/1", "management_port": False},
    ]
    entities = transform.ports_to_entities(
        _tables(vlan_properties=[], port_capabilities=caps), device="sw-idf1"
    )

    assert entities[0]._kw["interface"]._kw["mgmt_only"] is False
    assert "Multiple port_capabilities rows share port_name" not in caplog.text


def test_ports_to_entities_warns_on_per_device_capability_duplicates(stub_sdk, caplog):
    caps = [
        {"asset_device_id": "cs-uuid-42", "port_name": "1/1", "management_port": True},
        {"asset_device_id": "cs-uuid-42", "port_name": "1/1", "management_port": False},
    ]
    entities = transform.ports_to_entities(
        _tables(vlan_properties=[], port_capabilities=caps), device="sw-idf1"
    )

    assert entities[0]._kw["interface"]._kw["mgmt_only"] is True
    assert "Multiple port_capabilities rows share port_name '1/1' on device 'cs-uuid-42'" in caplog.text


def test_ports_to_entities_maps_poe_mode_pse_when_supported(stub_sdk):
    poe_state = {
        "device_id": "cs-uuid-42",
        "asset_interface_id": "if-uuid-1",
        "interface_name": "1/1",
        "supported": True,
    }
    poe_config = {
        "device_id": "cs-uuid-42",
        "asset_interface_id": "if-uuid-1",
        "interface_name": "1/1",
        "enable": False,
        "classification": 1,
    }
    entities = transform.ports_to_entities(
        _tables(vlan_properties=[], poe_states=[poe_state], poe_configs=[poe_config]),
        device="sw-idf1",
    )

    port = entities[0]._kw["interface"]._kw
    assert port["poe_mode"] == "pse"
    assert port["poe_type"] == "type1-ieee802.3af"


def test_ports_to_entities_omits_poe_when_not_supported(stub_sdk):
    poe_state = {
        "device_id": "cs-uuid-42",
        "asset_interface_id": "if-uuid-1",
        "supported": False,
    }
    entities = transform.ports_to_entities(
        _tables(vlan_properties=[], poe_states=[poe_state]),
        device="sw-idf1",
    )

    assert "poe_mode" not in entities[0]._kw["interface"]._kw


def test_ports_to_entities_ignores_poe_config_enable_without_supported(stub_sdk):
    """PoE admin enable alone does not imply pse — only state.supported does."""
    poe_state = {
        "device_id": "cs-uuid-42",
        "asset_interface_id": "if-uuid-1",
        "supported": False,
    }
    poe_config = {"asset_interface_id": "if-uuid-1", "enable": True, "classification": 3}
    entities = transform.ports_to_entities(
        _tables(vlan_properties=[], poe_states=[poe_state], poe_configs=[poe_config]),
        device="sw-idf1",
    )

    port = entities[0]._kw["interface"]._kw
    assert "poe_mode" not in port
    assert port["poe_type"] == "type2-ieee802.3at"


def test_ports_to_entities_maps_poe_classification_bt_and_omits_unmapped(stub_sdk):
    """IEEE BT maps to Diode poe_type; AF_HIGH / PRE_* have no Diode value."""
    poe_state = {
        "device_id": "cs-uuid-42",
        "asset_interface_id": "if-uuid-1",
        "supported": True,
    }
    for classification, expected in (
        (4, "type3-ieee802.3bt"),
        (5, "type4-ieee802.3bt"),
        (2, None),
        (6, None),
        (7, None),
        (0, None),
    ):
        poe_config = {"asset_interface_id": "if-uuid-1", "classification": classification}
        port = (
            transform.ports_to_entities(
                _tables(vlan_properties=[], poe_states=[poe_state], poe_configs=[poe_config]),
                device="sw-idf1",
            )[0]
            ._kw["interface"]
            ._kw
        )
        if expected is None:
            assert "poe_type" not in port
        else:
            assert port["poe_type"] == expected


def test_ports_to_entities_maps_oper_duplex_half(stub_sdk):
    state = {**PORT_STATE, "oper_duplex": 1}
    port = (
        transform.ports_to_entities(_tables(port_states=[state], vlan_properties=[]), device="sw-idf1")[0]
        ._kw["interface"]
        ._kw
    )
    assert port["duplex"] == "half"


def test_ports_to_entities_falls_back_to_config_duplex_auto(stub_sdk):
    """When oper_duplex is unset, config duplex (incl. AUTO) is used."""
    state = {**PORT_STATE, "oper_duplex": 0}
    config = {**PORT_CONFIG, "duplex": 4}
    port = (
        transform.ports_to_entities(
            _tables(port_configs=[config], port_states=[state], vlan_properties=[]),
            device="sw-idf1",
        )[0]
        ._kw["interface"]
        ._kw
    )
    assert port["duplex"] == "auto"


def test_ports_to_entities_does_not_fallback_for_non_unset_oper_duplex(stub_sdk):
    """NONE / unknown oper_duplex must not inherit configured duplex."""
    config = {**PORT_CONFIG, "duplex": 1}
    for oper_duplex in (3, 9):
        state = {**PORT_STATE, "oper_duplex": oper_duplex}
        port = (
            transform.ports_to_entities(
                _tables(port_configs=[config], port_states=[state], vlan_properties=[]),
                device="sw-idf1",
            )[0]
            ._kw["interface"]
            ._kw
        )
        assert "duplex" not in port


def test_ports_to_entities_prefers_oper_duplex_over_config(stub_sdk):
    state = {**PORT_STATE, "oper_duplex": 2}
    config = {**PORT_CONFIG, "duplex": 1}
    port = (
        transform.ports_to_entities(
            _tables(port_configs=[config], port_states=[state], vlan_properties=[]),
            device="sw-idf1",
        )[0]
        ._kw["interface"]
        ._kw
    )
    assert port["duplex"] == "full"


def test_ports_to_entities_does_not_use_native_vlan_without_vlan_properties(stub_sdk):
    """VLANs come only from vlan-properties — no AssetPortConfig.native_vlan invent."""
    config = {**PORT_CONFIG, "native_vlan": 99, "port_mode": True}
    entities = transform.ports_to_entities(
        _tables(port_configs=[config], vlan_properties=[]), device="sw-idf1"
    )

    port = entities[0]._kw["interface"]._kw
    assert "untagged_vlan" not in port
    assert "mode" not in port


def test_ports_to_entities_vlan_properties_still_apply_with_native_vlan_present(stub_sdk):
    config = {**PORT_CONFIG, "native_vlan": 99, "port_mode": True}
    entities = transform.ports_to_entities(_tables(port_configs=[config]), device="sw-idf1")

    port = entities[0]._kw["interface"]._kw
    assert port["untagged_vlan"]._kw == {"vid": 10, "name": "10"}
    assert port["mode"] == "tagged"


def test_ports_to_entities_rewrites_colon_ports_to_native_notation(stub_sdk):
    """ConfigState reports slot:port for every OS; on Fabric Engine the ports
    must come out slash-native with capability and LAG-member joins intact."""
    config = {
        "asset_device_id": "cs-uuid-42",
        "asset_interface_id": "if-uuid-52",
        "name": "1:52",
        "enabled": True,
    }
    caps = [{"asset_device_id": "cs-uuid-42", "port_name": "1:52", "management_port": True}]
    lag_config = {
        "asset_device_id": "cs-uuid-42",
        "asset_interface_id": "if-uuid-lag",
        "name": "lag 1",
        "member_ports": [{"interface_name": "1:52"}],
    }
    entities = transform.ports_to_entities(
        _tables(
            port_configs=[config],
            port_states=[],
            vlan_properties=[],
            port_capabilities=caps,
            lag_configs=[lag_config],
        ),
        device="sw-idf1",
        function="Fabric Engine",
    )

    by_name = {e._kw["interface"]._kw["name"]: e._kw["interface"]._kw for e in entities}
    assert set(by_name) == {"lag 1", "1/52"}
    assert by_name["1/52"]["mgmt_only"] is True
    assert by_name["1/52"]["lag"]._kw["name"] == "lag 1"
    # Caller rows stay untouched (tables are copied, not mutated).
    assert config["name"] == "1:52"


def test_ports_to_entities_keeps_colon_ports_for_switch_engine(stub_sdk):
    config = {"asset_device_id": "cs-uuid-42", "asset_interface_id": "if-uuid-52", "name": "1:52"}
    entities = transform.ports_to_entities(
        _tables(port_configs=[config], port_states=[], vlan_properties=[]),
        device="sw-idf1",
        function="Switch Engine",
    )

    assert entities[0]._kw["interface"]._kw["name"] == "1:52"


def test_ports_to_entities_emits_interface_ip_addresses(stub_sdk):
    ips = [
        {
            "asset_interface_id": "if-uuid-1",
            "address": "10.0.0.2",
            "mask_length": 24,
            "ip_version": 4,
            "is_primary": True,
        },
        {
            "asset_interface_id": "if-uuid-1",
            "address": "2001:db8::2",
            "mask_length": 64,
            "ip_version": 6,
            "is_primary": False,
        },
    ]
    entities = transform.ports_to_entities(_tables(vlan_properties=[], interface_ips=ips), device="sw-idf1")

    assert entities[0]._kw["interface"]._kw["name"] == "1/1"
    ip_entities = [e._kw["ip_address"]._kw for e in entities if "ip_address" in e._kw]
    addresses = {ip["address"] for ip in ip_entities}
    assert addresses == {"10.0.0.2/24", "2001:db8::2/64"}
    assert all(ip["assigned_object_interface"]._kw["name"] == "1/1" for ip in ip_entities)
    assert all(ip["assigned_object_interface"]._kw["device"]._kw["name"] == "sw-idf1" for ip in ip_entities)
    assert all(ip["assigned_object_interface"]._kw["type"] == "1000base-t" for ip in ip_entities)


def test_ports_to_entities_ip_stub_omits_type_when_port_state_missing(stub_sdk):
    """IP assignment stubs must not re-assert type=other when Interface omits type."""
    ips = [
        {
            "asset_interface_id": "if-uuid-1",
            "address": "10.0.0.2",
            "mask_length": 24,
        }
    ]
    entities = transform.ports_to_entities(
        _tables(port_states=[], vlan_properties=[], interface_ips=ips), device="sw-idf1"
    )

    port = entities[0]._kw["interface"]._kw
    assert "type" not in port
    ip = next(e._kw["ip_address"]._kw for e in entities if "ip_address" in e._kw)
    assert "type" not in ip["assigned_object_interface"]._kw


def test_ports_to_entities_emits_svi_ips_via_vlan_interface_name(stub_sdk):
    """An IP on an interface with no port/LAG row (e.g. a VLAN/SVI interface)
    emits a minimal Interface, then the IPAddress assigned to it.

    Name comes from vlan-properties (or port/LAG rows), not from the IP row —
    AssetInterfaceIpAddress has no interface_name in OpenAPI.
    """
    ips = [
        {
            "asset_interface_id": "if-svi",
            "address": "10.0.10.1",
            "mask_length": 24,
        }
    ]
    svi_vlan = {
        "device_id": "cs-uuid-42",
        "asset_interface_id": "if-svi",
        "interface_name": "vlan10",
        "port_vlan": 10,
        "vlans": [{"vlan_number": 10}],
    }
    entities = transform.ports_to_entities(
        _tables(vlan_properties=[svi_vlan], interface_ips=ips), device="sw-idf1"
    )

    # Physical port 1/1 from default fixtures, then the SVI interface + its IP.
    iface_entities = [e._kw["interface"]._kw for e in entities if "interface" in e._kw]
    svi = next(i for i in iface_entities if i["name"] == "vlan10")
    assert svi["device"]._kw["name"] == "sw-idf1"
    assert svi["type"] == "virtual"
    assert svi["enabled"] is True
    assert cf(svi["custom_fields"]["platformone_interface_id"]._kw) == "if-svi"

    ip_entities = [e._kw["ip_address"]._kw for e in entities if "ip_address" in e._kw]
    assert [ip["address"] for ip in ip_entities] == ["10.0.10.1/24"]
    assert ip_entities[0]["assigned_object_interface"]._kw["name"] == "vlan10"


def test_ports_to_entities_skips_orphan_ips_without_known_interface_name(stub_sdk):
    """IP rows whose asset_interface_id is not in port/LAG/VLAN tables are skipped."""
    ips = [
        {
            "asset_interface_id": "if-unknown",
            "address": "10.0.10.1",
            "mask_length": 24,
            # Non-schema field must not be used as a name source.
            "interface_name": "vlan10",
        }
    ]
    entities = transform.ports_to_entities(_tables(vlan_properties=[], interface_ips=ips), device="sw-idf1")

    assert not [e for e in entities if "ip_address" in e._kw]
    assert not [e for e in entities if "interface" in e._kw and e._kw["interface"]._kw["name"] == "vlan10"]


def test_ports_to_entities_skips_interface_ips_without_mask_length(stub_sdk):
    ips = [
        {
            "asset_interface_id": "if-uuid-1",
            "address": "10.0.0.2",
            "is_primary": True,
        }
    ]
    entities = transform.ports_to_entities(_tables(vlan_properties=[], interface_ips=ips), device="sw-idf1")

    assert not [e for e in entities if "ip_address" in e._kw]


def test_ports_to_entities_untagged_only_is_access_mode(stub_sdk):
    vlan = {**VLAN_PROPERTIES, "vlans": [{"vlan_number": 10}]}
    entities = transform.ports_to_entities(_tables(vlan_properties=[vlan]), device="sw-idf1")

    port = entities[0]._kw["interface"]._kw
    assert port["mode"] == "access"
    assert port["untagged_vlan"]._kw == {"vid": 10, "name": "10"}
    assert "tagged_vlans" not in port


def test_ports_to_entities_omits_mode_when_no_vlan_rows(stub_sdk):
    """FLEX-UNI/Fabric-Attach ports can be mapped to an I-SID instead of a
    VLAN -- inventing an access mode would misrepresent them."""
    port = transform.ports_to_entities(_tables(vlan_properties=[]), device="sw-idf1")[0]._kw["interface"]._kw
    assert "mode" not in port
    assert "untagged_vlan" not in port
    assert "tagged_vlans" not in port


def test_ports_to_entities_omits_reserved_untagged_vlan(stub_sdk):
    """Untagged VID 4094 (Extreme reserved) is omitted from the interface."""
    vlan = {
        **VLAN_PROPERTIES,
        "port_vlan": 4094,
        "vlans": [{"vlan_number": 10}, {"vlan_number": 4094}],
    }
    entities = transform.ports_to_entities(_tables(vlan_properties=[vlan]), device="sw-idf1")
    port = entities[0]._kw["interface"]._kw
    assert "untagged_vlan" not in port
    assert [v._kw["vid"] for v in port["tagged_vlans"]] == [10]
    assert port["mode"] == "tagged"


def test_ports_to_entities_strips_reserved_tagged_vids(stub_sdk):
    """Tagged list drops Extreme reserved VIDs; user VIDs and mode remain."""
    vlan = {
        **VLAN_PROPERTIES,
        "port_vlan": 10,
        "vlans": [
            {"vlan_number": 10},
            {"vlan_number": 20},
            {"vlan_number": 4060},
            {"vlan_number": 4094},
        ],
    }
    entities = transform.ports_to_entities(_tables(vlan_properties=[vlan]), device="sw-idf1")
    port = entities[0]._kw["interface"]._kw
    assert port["untagged_vlan"]._kw == {"vid": 10, "name": "10"}
    assert [v._kw["vid"] for v in port["tagged_vlans"]] == [20]
    assert port["mode"] == "tagged"


def test_ports_to_entities_omits_vlan_and_mode_when_only_reserved(stub_sdk):
    """A port whose only membership is reserved VID 4094 gets no VLAN/mode."""
    vlan = {
        **VLAN_PROPERTIES,
        "port_vlan": 4094,
        "vlans": [{"vlan_number": 4094}],
    }
    port = (
        transform.ports_to_entities(_tables(vlan_properties=[vlan]), device="sw-idf1")[0]._kw["interface"]._kw
    )
    assert "mode" not in port
    assert "untagged_vlan" not in port
    assert "tagged_vlans" not in port


def test_ports_to_entities_ports_join_on_interface_id_not_row_order(stub_sdk):
    config2 = {**PORT_CONFIG, "asset_interface_id": "if-uuid-2", "name": "1/2", "enabled": False}
    state2 = {**PORT_STATE, "asset_interface_id": "if-uuid-2", "name": "1/2", "oper_state": 2}
    entities = transform.ports_to_entities(
        _tables(
            port_configs=[PORT_CONFIG, config2],
            port_states=[state2, PORT_STATE],  # deliberately reversed order
            vlan_properties=[],
        ),
        device="sw-idf1",
    )

    ports = {e._kw["interface"]._kw["name"]: e._kw["interface"]._kw for e in entities}
    assert ports["1/1"]["enabled"] is True
    assert ports["1/1"]["mark_connected"] is True
    assert ports["1/2"]["enabled"] is False
    assert ports["1/2"]["mark_connected"] is False


def test_ports_to_entities_nests_device_site_role_and_type(stub_sdk):
    """Diode generate-diff requires nested Device site/role/device_type."""
    entities = transform.ports_to_entities(
        _tables(vlan_properties=[]),
        device="sw-idf1",
        function="Fabric Engine",
        site_name="Campus",
        product_type="FabricEngine_5320_48P_8XE",
    )
    device = entities[0]._kw["interface"]._kw["device"]._kw
    assert device["name"] == "sw-idf1"
    assert device["site"]._kw["name"] == "Campus"
    assert device["role"]._kw["name"] == "Switch"
    assert device["device_type"]._kw["model"] == "5320-48P-8XE-FabricEngine"


def test_ports_to_entities_warns_on_conflicting_port_vlan(stub_sdk, caplog):
    vlan_a = {**VLAN_PROPERTIES, "port_vlan": 10, "vlans": [{"vlan_number": 10}]}
    vlan_b = {**VLAN_PROPERTIES, "port_vlan": 20, "vlans": [{"vlan_number": 20}]}
    port = (
        transform.ports_to_entities(_tables(vlan_properties=[vlan_a, vlan_b]), device="sw-idf1")[0]
        ._kw["interface"]
        ._kw
    )
    assert port["untagged_vlan"]._kw["vid"] == 10
    assert "Conflicting port_vlan" in caplog.text


def test_ports_to_entities_accepts_string_mask_length(stub_sdk):
    ips = [
        {
            "asset_interface_id": "if-uuid-1",
            "address": "10.0.0.2",
            "mask_length": "24",
            "is_primary": True,
        }
    ]
    entities = transform.ports_to_entities(_tables(vlan_properties=[], interface_ips=ips), device="sw-idf1")
    ip_entities = [e._kw["ip_address"]._kw for e in entities if "ip_address" in e._kw]
    assert ip_entities[0]["address"] == "10.0.0.2/24"
