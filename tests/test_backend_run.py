"""Backend orchestration, run, degradation, and scope tests."""

from __future__ import annotations

import json

import responses
from worker.models import Config, Policy

from orb_extreme_platformone.backend import Backend
from tests.backend_helpers import (
    _mock_assets,
    _mock_configstate,
    _mock_empty_clusters,
    _mock_empty_fabric_tables,
    _mock_empty_interface_id_tables,
    _mock_empty_port_and_lag_tables,
    _mock_empty_port_tables,
    _policy,
)
from tests.conftest import CS_SWITCH, SWITCH_ASSET


@responses.activate
def test_run_produces_site_location_device_and_interface_entities() -> None:
    _mock_assets([SWITCH_ASSET])
    _mock_configstate("asset-device", "AssetDevice", [CS_SWITCH])
    _mock_configstate(
        "asset-location",
        "AssetLocation",
        [
            {
                "asset_device_id": "cs-uuid-42",
                "site_name": "HQ",
                "building_name": "B1",
                "floor_name": "F2",
            },
        ],
    )
    _mock_configstate(
        "asset-port-config",
        "AssetPortConfig",
        [
            {
                "asset_device_id": "cs-uuid-42",
                "asset_interface_id": "if-1",
                "name": "1/1",
                "enabled": True,
                "description": "uplink",
            },
        ],
    )
    _mock_configstate(
        "asset-port-state",
        "AssetPortState",
        [
            {
                "asset_device_id": "cs-uuid-42",
                "asset_interface_id": "if-1",
                "name": "1/1",
                "oper_state": 1,
                "oper_speed": 4,
                "oper_duplex": 2,
                "connector_type": 1,
            },
        ],
    )
    _mock_configstate(
        "asset-interface-vlan-properties",
        "AssetInterfaceVlanProperties",
        [
            {
                "device_id": "cs-uuid-42",
                "asset_interface_id": "if-1",
                "interface_name": "1/1",
                "port_vlan": 10,
                "vlans": [{"vlan_number": 10}, {"vlan_number": 20}],
            },
        ],
    )
    _mock_configstate("asset-lag-config", "AssetLagConfig", [])
    _mock_configstate("asset-lag-state", "AssetLagState", [])
    _mock_configstate("asset-port-capabilities", "AssetPortCapabilities", [])
    _mock_configstate("asset-poe-power-ports-state", "AssetPoePowerPortsState", [])
    _mock_empty_interface_id_tables()
    _mock_empty_fabric_tables()
    _mock_empty_clusters()

    entities = list(Backend().run("platformone_worker", _policy()))

    assert len(entities) == 5
    assert entities[0].site.name == "HQ"
    assert entities[1].location.name == "B1"
    assert entities[2].location.name == "F2"
    assert entities[2].location.parent.name == "B1"
    assert entities[3].device.name == "sw-idf1"
    assert entities[3].device.site.name == "HQ"
    assert entities[3].device.location.name == "F2"
    assert entities[3].device.role.name == "Switch"
    assert "platformone_configstate_device_id" not in entities[3].device.custom_fields
    interface = entities[4].interface
    assert interface.name == "1/1"
    assert interface.device.name == "sw-idf1"
    assert interface.enabled is True
    assert interface.mark_connected is True
    assert interface.speed == 1_000_000
    assert interface.type == "1000base-t"
    assert interface.untagged_vlan.vid == 10
    assert interface.untagged_vlan.name == "10"
    assert [v.vid for v in interface.tagged_vlans] == [20]
    assert interface.tagged_vlans[0].name == "20"
    assert interface.mode == "tagged"


@responses.activate
def test_run_attaches_isis_and_spbm_device_custom_fields() -> None:
    _mock_assets([SWITCH_ASSET])
    _mock_configstate("asset-device", "AssetDevice", [CS_SWITCH])
    _mock_configstate(
        "asset-location",
        "AssetLocation",
        [{"asset_device_id": "cs-uuid-42", "site_name": "HQ"}],
    )
    _mock_empty_port_and_lag_tables(include_fabric=False)
    _mock_configstate(
        "asset-isis-global-config",
        "AssetIsisGlobalConfig",
        [
            {
                "asset_device_id": "cs-uuid-42",
                "manual_area_address": "00.0001.0000.00",
                "sys_id": "0010.0a0b.0c0d.00",
            },
        ],
    )
    _mock_configstate("asset-isis-global-state", "AssetIsisGlobalState", [])
    _mock_configstate(
        "asset-spbm-instance",
        "AssetSpbmInstance",
        [{"asset_device_id": "cs-uuid-42", "node_nick_name": "0.aa.bb", "instance_id": 1}],
    )
    _mock_empty_clusters()

    entities = list(Backend().run("platformone_worker", _policy()))

    device = next(e.device for e in entities if e.HasField("device"))
    assert device.custom_fields["platformone_isis_area"].text == "00.0001.0000.00"
    assert device.custom_fields["platformone_isis_system_id"].text == "0010.0a0b.0c0d.00"
    assert device.custom_fields["platformone_spbm_nickname"].text == "0.aa.bb"
    assert any("/retrieve-asset-isis-global-config" in c.request.url for c in responses.calls)
    assert any("/retrieve-asset-spbm-instance" in c.request.url for c in responses.calls)


@responses.activate
def test_run_sets_device_primary_ip_from_configstate_interface_cidr() -> None:
    """Bare Assets management IP must not become /32; use ConfigState mask."""
    _mock_assets([SWITCH_ASSET])
    _mock_configstate("asset-device", "AssetDevice", [CS_SWITCH])
    _mock_configstate("asset-location", "AssetLocation", [])
    _mock_configstate(
        "asset-port-config",
        "AssetPortConfig",
        [
            {
                "asset_device_id": "cs-uuid-42",
                "asset_interface_id": "if-1",
                "name": "1/1",
                "enabled": True,
            },
        ],
    )
    _mock_configstate("asset-port-state", "AssetPortState", [])
    _mock_configstate("asset-interface-vlan-properties", "AssetInterfaceVlanProperties", [])
    _mock_configstate("asset-lag-config", "AssetLagConfig", [])
    _mock_configstate("asset-lag-state", "AssetLagState", [])
    _mock_configstate("asset-port-capabilities", "AssetPortCapabilities", [])
    _mock_configstate("asset-poe-power-ports-state", "AssetPoePowerPortsState", [])
    _mock_configstate(
        "asset-interface-ip-address",
        "AssetInterfaceIpAddress",
        [
            {
                "asset_interface_id": "if-1",
                "address": "10.0.0.2",
                "mask_length": 24,
                "is_primary": True,
            },
        ],
    )
    _mock_empty_fabric_tables()
    _mock_empty_clusters()

    entities = list(Backend().run("platformone_worker", _policy()))

    devices = [e.device for e in entities if e.HasField("device")]
    # Main Device (no primary_ip) then follow-up Device that asserts primary_ip4
    # after Interface IPAddress entities — avoids Diode apply dropping serial/CFs.
    assert len(devices) == 2
    assert not devices[0].HasField("primary_ip4")
    assert devices[0].serial == "SN42"
    assert devices[1].primary_ip4.address == "10.0.0.2/24"

    # Position is load-bearing, not incidental: NetBox rejects primary_ip* for an
    # IP that is not yet assigned to the device, and drops the sibling fields
    # (serial, custom fields) with it. Assert the IPAddress lands first.
    kinds = [entity.WhichOneof("entity") for entity in entities]
    device_positions = [index for index, kind in enumerate(kinds) if kind == "device"]
    assert kinds.index("ip_address") < device_positions[-1], (
        "Device(primary_ip*) must be emitted after the IPAddress entity it references"
    )


@responses.activate
def test_run_batches_every_switch_into_one_call_per_port_table() -> None:
    switch2 = {**SWITCH_ASSET, "device_id": 43, "host_name": "sw-idf2", "serial_number": "SN43"}
    cs2 = {"id": "cs-uuid-43", "serial_number": "SN43"}
    _mock_assets([SWITCH_ASSET, switch2])
    _mock_configstate("asset-device", "AssetDevice", [CS_SWITCH, cs2])
    _mock_configstate("asset-location", "AssetLocation", [])
    _mock_empty_port_tables()

    list(Backend().run("platformone_worker", _policy()))

    device_calls = [c for c in responses.calls if "/retrieve-asset-device" in c.request.url]
    assert len(device_calls) == 1
    assert json.loads(device_calls[0].request.body) == {"serial_number": ["SN42", "SN43"]}
    port_calls = [c for c in responses.calls if "/retrieve-asset-port-config" in c.request.url]
    assert len(port_calls) == 1
    assert json.loads(port_calls[0].request.body) == {"asset_device_id": ["cs-uuid-42", "cs-uuid-43"]}
    vlan_calls = [c for c in responses.calls if "/retrieve-asset-interface-vlan-properties" in c.request.url]
    assert json.loads(vlan_calls[0].request.body) == {"device_id": ["cs-uuid-42", "cs-uuid-43"]}
    assert not [c for c in responses.calls if "/retrieve-asset-vlan-config" in c.request.url]
    inferred_calls = [c for c in responses.calls if "/retrieve-inferred-device" in c.request.url]
    assert len(inferred_calls) == 1
    assert json.loads(inferred_calls[0].request.body) == {"asset_device_id": ["cs-uuid-42", "cs-uuid-43"]}
    # No InferredDevice rows -> cluster retrieve is skipped (AssetDevice UUIDs
    # are the wrong ID space for device_one_id / device_two_id).
    assert not [c for c in responses.calls if "/retrieve-inferred-cluster" in c.request.url]


@responses.activate
def test_run_serial_less_configstate_record_stays_uncorrelated() -> None:
    """Serial number is the primary key between the two APIs -- there is
    deliberately no MAC/IP fallback. A ConfigState record without one never
    correlates; the device still syncs Assets-only.
    """
    cs = {"id": "cs-uuid-42", "base_mac_address": "AA:BB:CC:DD:EE:FF"}
    _mock_assets([SWITCH_ASSET])
    _mock_configstate("asset-device", "AssetDevice", [cs])
    _mock_configstate("asset-location", "AssetLocation", [])

    entities = list(Backend().run("platformone_worker", _policy()))

    assert [e.device.name for e in entities if e.HasField("device")] == ["sw-idf1"]
    assert not [c for c in responses.calls if "/retrieve-asset-port-config" in c.request.url]


@responses.activate
def test_run_out_of_scope_devices_get_no_port_calls_and_no_entities() -> None:
    """Scope regression: an out-of-scope switch must not leak back in as
    Interface entities via the port fan-out (Diode would re-create its
    Device through implicit reference handling).
    """
    branch_switch = {**SWITCH_ASSET, "device_id": 43, "host_name": "sw-branch", "serial_number": "SN43"}
    cs2 = {"id": "cs-uuid-43", "serial_number": "SN43"}
    _mock_assets([SWITCH_ASSET, branch_switch])
    _mock_configstate("asset-device", "AssetDevice", [CS_SWITCH, cs2])
    _mock_configstate(
        "asset-location",
        "AssetLocation",
        [
            {"asset_device_id": "cs-uuid-42", "site_name": "HQ"},
            {"asset_device_id": "cs-uuid-43", "site_name": "Branch"},
        ],
    )
    _mock_empty_port_tables()

    policy = Policy(
        config=Config(package="orb_extreme_platformone", PLATFORMONE_API_TOKEN="tok"),
        scope={"sites": ["HQ"]},
    )
    entities = list(Backend().run("platformone_worker", policy))

    device_names = [e.device.name for e in entities if e.HasField("device")]
    assert device_names == ["sw-idf1"]
    port_calls = [c for c in responses.calls if "/retrieve-asset-port-config" in c.request.url]
    assert json.loads(port_calls[0].request.body) == {"asset_device_id": ["cs-uuid-42"]}
    inferred_calls = [c for c in responses.calls if "/retrieve-inferred-device" in c.request.url]
    assert json.loads(inferred_calls[0].request.body) == {"asset_device_id": ["cs-uuid-42"]}
    assert not [c for c in responses.calls if "/retrieve-inferred-cluster" in c.request.url]


@responses.activate
def test_run_survives_a_failed_port_table_and_keeps_the_rest(caplog) -> None:
    """One failing ConfigState table (here port-state) degrades that table's
    fields for the tick; ports still map from port-config.

    Tables are fetched concurrently; a single failure must not abort siblings.
    """
    _mock_assets([SWITCH_ASSET])
    _mock_configstate("asset-device", "AssetDevice", [CS_SWITCH])
    _mock_configstate("asset-location", "AssetLocation", [])
    _mock_configstate(
        "asset-port-config",
        "AssetPortConfig",
        [{"asset_device_id": "cs-uuid-42", "asset_interface_id": "if-1", "name": "1/1", "enabled": True}],
    )
    _mock_configstate("asset-port-state", "AssetPortState", [], status=500)
    _mock_configstate("asset-interface-vlan-properties", "AssetInterfaceVlanProperties", [])
    _mock_configstate("asset-lag-config", "AssetLagConfig", [])
    _mock_configstate("asset-lag-state", "AssetLagState", [])
    _mock_configstate("asset-port-capabilities", "AssetPortCapabilities", [])
    _mock_configstate("asset-poe-power-ports-state", "AssetPoePowerPortsState", [])
    _mock_empty_interface_id_tables()
    _mock_empty_fabric_tables()
    _mock_empty_clusters()

    entities = list(Backend().run("platformone_worker", _policy()))

    interfaces = [e.interface for e in entities if e.HasField("interface")]
    assert [i.name for i in interfaces] == ["1/1"]
    assert interfaces[0].enabled is True
    assert "ConfigState degradation this tick; failed tables: asset-port-state" in caplog.text
    # Sibling tables still ran (concurrent degrade-on-failure).
    assert any("/retrieve-asset-port-config" in c.request.url for c in responses.calls)
    assert any("/retrieve-asset-lag-config" in c.request.url for c in responses.calls)
    assert not any("/retrieve-asset-vlan-config" in c.request.url for c in responses.calls)


def test_correlate_warns_on_duplicate_serial(caplog) -> None:
    from orb_extreme_platformone.extract import correlate

    assets = [{"device_id": 1, "serial_number": "SN1"}]
    cs_devices = [
        {"id": "a", "serial_number": "SN1"},
        {"id": "b", "serial_number": "sn1"},
    ]
    matched = correlate(assets, cs_devices)

    assert matched[1]["id"] == "a"
    assert "Duplicate ConfigState AssetDevice serial_number" in caplog.text


def test_correlate_preserves_string_device_ids() -> None:
    """Assets device_id may arrive as a JSON string; lookup must still work."""
    from orb_extreme_platformone.extract.correlate import correlate

    assets = [{"device_id": "42", "serial_number": "SN1"}]
    cs_devices = [{"id": "cs-uuid-1", "serial_number": "SN1"}]

    matched = correlate(assets, cs_devices)

    assert matched["42"]["id"] == "cs-uuid-1"


@responses.activate
def test_run_maps_inferred_cluster_to_virtual_chassis() -> None:
    switch2 = {**SWITCH_ASSET, "device_id": 43, "host_name": "sw-idf2", "serial_number": "SN43"}
    cs2 = {"id": "cs-uuid-43", "serial_number": "SN43"}
    _mock_assets([SWITCH_ASSET, switch2])
    _mock_configstate("asset-device", "AssetDevice", [CS_SWITCH, cs2])
    _mock_configstate("asset-location", "AssetLocation", [])
    _mock_empty_port_and_lag_tables()
    # device_one_id / device_two_id are InferredDevice UUIDs, not AssetDevice.
    _mock_configstate(
        "inferred-device",
        "InferredDevice",
        [
            {"id": "inf-uuid-42", "asset_device_id": "cs-uuid-42"},
            {"id": "inf-uuid-43", "asset_device_id": "cs-uuid-43"},
        ],
    )
    cluster = {
        "id": "cluster-uuid-1",
        "device_one_id": "inf-uuid-42",
        "device_two_id": "inf-uuid-43",
        "device_one_peer_name": "peer-b",
        "device_two_peer_name": "peer-a",
    }
    # Both member-filter calls return the same cluster; backend dedupes by id.
    _mock_configstate("inferred-cluster", "InferredCluster", [cluster])
    _mock_configstate("inferred-cluster", "InferredCluster", [cluster])

    entities = list(Backend().run("platformone_worker", _policy()))

    inferred_calls = [c for c in responses.calls if "/retrieve-inferred-device" in c.request.url]
    assert json.loads(inferred_calls[0].request.body) == {"asset_device_id": ["cs-uuid-42", "cs-uuid-43"]}
    cluster_calls = [c for c in responses.calls if "/retrieve-inferred-cluster" in c.request.url]
    assert len(cluster_calls) == 2
    bodies = [json.loads(c.request.body) for c in cluster_calls]
    assert {"device_one_id": ["inf-uuid-42", "inf-uuid-43"]} in bodies
    assert {"device_two_id": ["inf-uuid-42", "inf-uuid-43"]} in bodies

    chassis = [e.virtual_chassis for e in entities if e.HasField("virtual_chassis")]
    assert len(chassis) == 1
    assert chassis[0].name == "peer-a / peer-b"
    assert chassis[0].master.name == "sw-idf1"
    assert chassis[0].master.site.name == "Assets-Site"
    assert chassis[0].master.role.name == "Switch"
    assert chassis[0].master.device_type.model == "5320-48P-8XE-FabricEngine"
    assert not chassis[0].description
    assert chassis[0].custom_fields["platformone_cluster_id"].text == "cluster-uuid-1"

    devices = {e.device.name: e.device for e in entities if e.HasField("device")}
    assert devices["sw-idf1"].virtual_chassis.name == "peer-a / peer-b"
    assert devices["sw-idf1"].virtual_chassis.custom_fields["platformone_cluster_id"].text == "cluster-uuid-1"
    assert devices["sw-idf1"].vc_position == 1
    assert devices["sw-idf2"].vc_position == 2

    # Membership Devices precede VirtualChassis.master (fresh-create race).
    kinds = []
    for e in entities:
        if e.HasField("device"):
            kinds.append("device")
        elif e.HasField("virtual_chassis"):
            kinds.append("virtual_chassis")
    assert kinds.index("device") < kinds.index("virtual_chassis")


@responses.activate
def test_run_maps_lag_interfaces_and_member_lag_refs() -> None:
    _mock_assets([SWITCH_ASSET])
    _mock_configstate("asset-device", "AssetDevice", [CS_SWITCH])
    _mock_configstate("asset-location", "AssetLocation", [])
    _mock_configstate(
        "asset-port-config",
        "AssetPortConfig",
        [
            {"asset_device_id": "cs-uuid-42", "asset_interface_id": "if-1", "name": "1/1", "enabled": True},
            {"asset_device_id": "cs-uuid-42", "asset_interface_id": "if-2", "name": "1/2", "enabled": True},
        ],
    )
    _mock_configstate("asset-port-state", "AssetPortState", [])
    _mock_configstate("asset-interface-vlan-properties", "AssetInterfaceVlanProperties", [])
    _mock_configstate(
        "asset-lag-config",
        "AssetLagConfig",
        [
            {
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
            },
        ],
    )
    _mock_configstate("asset-lag-state", "AssetLagState", [])
    _mock_configstate("asset-port-capabilities", "AssetPortCapabilities", [])
    _mock_configstate("asset-poe-power-ports-state", "AssetPoePowerPortsState", [])
    _mock_empty_interface_id_tables()
    _mock_empty_fabric_tables()
    _mock_empty_clusters()

    entities = list(Backend().run("platformone_worker", _policy()))

    interfaces = {e.interface.name: e.interface for e in entities if e.HasField("interface")}
    assert set(interfaces) == {"lag1", "1/1", "1/2"}
    assert interfaces["lag1"].type == "lag"
    assert interfaces["lag1"].enabled is True
    assert interfaces["lag1"].custom_fields["platformone_interface_id"].text == "lag-if-1"
    assert interfaces["1/1"].lag.name == "lag1"
    assert interfaces["1/2"].lag.name == "lag1"
    assert not [c for c in responses.calls if "/retrieve-asset-lag-config-member-port" in c.request.url]
    assert not [c for c in responses.calls if "/retrieve-asset-lag-state-member-port" in c.request.url]


@responses.activate
def test_run_survives_a_failed_inferred_cluster_fetch() -> None:
    _mock_assets([SWITCH_ASSET])
    _mock_configstate("asset-device", "AssetDevice", [CS_SWITCH])
    _mock_configstate("asset-location", "AssetLocation", [])
    _mock_empty_port_and_lag_tables()
    _mock_configstate(
        "inferred-device",
        "InferredDevice",
        [{"id": "inf-uuid-42", "asset_device_id": "cs-uuid-42"}],
    )
    # Both member-side filters fail → extract raises; backend degrades VC.
    _mock_configstate("inferred-cluster", "InferredCluster", [], status=500)
    _mock_configstate("inferred-cluster", "InferredCluster", [], status=500)

    entities = list(Backend().run("platformone_worker", _policy()))

    assert [e.device.name for e in entities if e.HasField("device")] == ["sw-idf1"]
    assert not [e for e in entities if e.HasField("virtual_chassis")]


@responses.activate
def test_run_keeps_virtual_chassis_when_one_cluster_filter_fails(caplog) -> None:
    """One member-side inferred-cluster filter can fail without dropping VC."""
    switch2 = {**SWITCH_ASSET, "device_id": 43, "host_name": "sw-idf2", "serial_number": "SN43"}
    cs2 = {"id": "cs-uuid-43", "serial_number": "SN43"}
    _mock_assets([SWITCH_ASSET, switch2])
    _mock_configstate("asset-device", "AssetDevice", [CS_SWITCH, cs2])
    _mock_configstate("asset-location", "AssetLocation", [])
    _mock_empty_port_and_lag_tables()
    _mock_configstate(
        "inferred-device",
        "InferredDevice",
        [
            {"id": "inf-uuid-42", "asset_device_id": "cs-uuid-42"},
            {"id": "inf-uuid-43", "asset_device_id": "cs-uuid-43"},
        ],
    )
    cluster = {
        "id": "cluster-uuid-1",
        "device_one_id": "inf-uuid-42",
        "device_two_id": "inf-uuid-43",
        "device_one_peer_name": "peer-b",
        "device_two_peer_name": "peer-a",
    }
    # First filter (device_one_id) succeeds; second (device_two_id) fails.
    _mock_configstate("inferred-cluster", "InferredCluster", [cluster])
    _mock_configstate("inferred-cluster", "InferredCluster", [], status=500)

    entities = list(Backend().run("platformone_worker", _policy()))

    chassis = [e.virtual_chassis for e in entities if e.HasField("virtual_chassis")]
    assert len(chassis) == 1
    assert chassis[0].name == "peer-a / peer-b"
    assert "continuing with other member side" in caplog.text


@responses.activate
def test_run_survives_a_configstate_outage_with_assets_only_data() -> None:
    """ConfigState down entirely: devices still sync from Assets (flat site,
    no ports), the tick does not fail.
    """
    _mock_assets([SWITCH_ASSET])
    _mock_configstate("asset-device", "AssetDevice", [], status=500)

    entities = list(Backend().run("platformone_worker", _policy()))

    assert [e.site.name for e in entities if e.HasField("site")] == ["Assets-Site"]
    device_names = [e.device.name for e in entities if e.HasField("device")]
    assert device_names == ["sw-idf1"]
    assert not [e for e in entities if e.HasField("interface")]


@responses.activate
def test_run_uncorrelated_device_syncs_without_ports() -> None:
    """A device Assets knows but ConfigState doesn't (not collected yet)
    still becomes a Device entity -- just with no ports or building/floor.
    """
    _mock_assets([SWITCH_ASSET])
    _mock_configstate("asset-device", "AssetDevice", [{"id": "cs-other", "serial_number": "OTHER"}])
    _mock_configstate("asset-location", "AssetLocation", [])

    entities = list(Backend().run("platformone_worker", _policy()))

    device_names = [e.device.name for e in entities if e.HasField("device")]
    assert device_names == ["sw-idf1"]
    assert not [e for e in entities if e.HasField("interface")]
    port_calls = [c for c in responses.calls if "/retrieve-asset-port" in c.request.url]
    assert not port_calls
    assert not [c for c in responses.calls if "/retrieve-inferred-cluster" in c.request.url]


@responses.activate
def test_run_maps_ap_radios_and_wlans() -> None:
    ap_asset = {
        "device_id": 99,
        "host_name": "ap-lobby",
        "serial_number": "AP99",
        "mac_address": "aabbccddee99",
        "product_type": "AP5050",
        "function": "AP",
        "os_version": "10.7.0",
        "is_connected": True,
        "ip_address": "10.0.0.99",
        "site_name": "HQ",
    }
    _mock_assets([ap_asset])
    _mock_configstate(
        "asset-device",
        "AssetDevice",
        [{"id": "cs-ap-1", "serial_number": "AP99", "base_mac_address": "AA:BB:CC:DD:EE:99"}],
    )
    _mock_configstate(
        "asset-location",
        "AssetLocation",
        [{"asset_device_id": "cs-ap-1", "site_name": "HQ", "building_name": "B1", "floor_name": "F1"}],
    )
    _mock_configstate(
        "asset-wireless-interface",
        "AssetWirelessInterface",
        [
            {
                "asset_device_id": "cs-ap-1",
                "asset_interface_id": "radio-1",
                "name": "wifi0",
                "enabled": True,
            },
        ],
    )
    _mock_configstate(
        "asset-wireless-interface-state",
        "AssetWirelessInterfaceState",
        [
            {
                "asset_device_id": "cs-ap-1",
                "asset_interface_id": "radio-1",
                "name": "wifi0",
                "band": "5GHz",
                "channel": 36,
                "channel_width": 40,
                "bssid": "aa:bb:cc:dd:ee:01",
                "power": 15,
                "radio_mode": "_11ax_5g",
            },
        ],
    )
    _mock_configstate(
        "asset-ssid-config",
        "AssetSsidConfig",
        [{"asset_device_id": "cs-ap-1", "name": "Corp", "enabled": True, "if_names": "wifi0"}],
    )
    _mock_configstate(
        "asset-ssid-state",
        "AssetSsidState",
        [{"asset_device_id": "cs-ap-1", "name": "Corp", "encryption": "TYPE_802DOT1X", "if_names": "wifi0"}],
    )
    _mock_empty_clusters()

    entities = list(Backend().run("platformone_worker", _policy()))

    wlans = [e.wireless_lan for e in entities if e.HasField("wireless_lan")]
    radios = [e.interface for e in entities if e.HasField("interface")]
    devices = [e.device for e in entities if e.HasField("device")]
    assert [d.name for d in devices] == ["ap-lobby"]
    assert devices[0].role.name == "Wireless AP"
    assert [w.ssid for w in wlans] == ["Corp"]
    assert wlans[0].auth_type == "wpa-enterprise"
    assert wlans[0].status == "active"
    assert len(radios) == 1
    assert radios[0].name == "wifi0"
    assert radios[0].rf_role == "ap"
    assert radios[0].type == "ieee802.11ax"
    assert radios[0].tx_power == 15
    assert radios[0].rf_channel_frequency == 5180.0
    assert radios[0].rf_channel_width == 40.0
    assert [w.ssid for w in radios[0].wireless_lans] == ["Corp"]
    # APs must not trigger switch port retrieves.
    assert not [c for c in responses.calls if "/retrieve-asset-port-config" in c.request.url]


@responses.activate
def test_run_site_scope_matches_case_insensitively() -> None:
    _mock_assets([SWITCH_ASSET])
    _mock_configstate("asset-device", "AssetDevice", [CS_SWITCH])
    _mock_configstate(
        "asset-location",
        "AssetLocation",
        [{"asset_device_id": "cs-uuid-42", "site_name": "HQ"}],
    )
    _mock_empty_port_tables()

    policy = Policy(
        config=Config(package="orb_extreme_platformone", PLATFORMONE_API_TOKEN="tok"),
        scope={"sites": ["hq"]},
    )
    entities = list(Backend().run("platformone_worker", policy))

    assert [e.device.name for e in entities if e.HasField("device")] == ["sw-idf1"]
