"""Virtual chassis transform tests."""

from __future__ import annotations

from orb_extreme_platformone import transform
from tests.conftest import cf
from tests.transform_helpers import _record

SWITCH_ASSET_PEER = {
    "device_id": 43,
    "host_name": "sw-idf2",
    "serial_number": "SN43",
    "mac_address": "aabbccddee00",
    "product_type": "FabricEngine_5320_48P_8XE",
    "function": "Fabric Engine",
    "os_version": "9.2.1.0",
    "is_connected": True,
    "ip_address": "10.0.0.3",
    "site_name": "Assets-Site",
}

INFERRED_CLUSTER = {
    "id": "cluster-uuid-1",
    "device_one_id": "cs-uuid-42",
    "device_two_id": "cs-uuid-43",
    "device_one_peer_name": "peer-b",
    "device_two_peer_name": "peer-a",
    "type": 1,
}


def test_virtual_chassis_to_entities_maps_inferred_cluster(stub_sdk):
    records_by_cs_id = {
        "cs-uuid-42": _record(),
        "cs-uuid-43": _record(asset=SWITCH_ASSET_PEER, cs_device_id="cs-uuid-43"),
    }

    entities, memberships = transform.virtual_chassis_to_entities(
        [INFERRED_CLUSTER],
        records_by_cs_id=records_by_cs_id,
    )

    assert len(entities) == 1
    vc = entities[0]._kw["virtual_chassis"]._kw
    # Peer names are sorted for a stable name when primary/backup flips.
    assert vc["name"] == "peer-a / peer-b"
    master = vc["master"]._kw
    assert master["name"] == "sw-idf1"
    assert master["site"]._kw["name"] == "Assets-Site"
    assert master["role"]._kw["name"] == "Switch"
    assert master["device_type"]._kw["model"] == "5320-48P-8XE-FabricEngine"
    assert "description" not in vc
    assert vc["tags"] == ["extreme-networks", "platform-one", "discovered"]
    assert cf(vc["custom_fields"]["platformone_cluster_id"]._kw) == "cluster-uuid-1"
    assert "domain" not in vc
    assert "comments" not in vc
    assert memberships == {
        "cs-uuid-42": {"name": "peer-a / peer-b", "position": 1, "cluster_id": "cluster-uuid-1"},
        "cs-uuid-43": {"name": "peer-a / peer-b", "position": 2, "cluster_id": "cluster-uuid-1"},
    }


def test_virtual_chassis_to_entities_skips_partial_clusters(stub_sdk):
    """Both members must be in scope; a half-known pair is skipped."""
    entities, memberships = transform.virtual_chassis_to_entities(
        [INFERRED_CLUSTER],
        records_by_cs_id={"cs-uuid-42": _record()},
    )

    assert entities == []
    assert memberships == {}


def test_virtual_chassis_falls_back_to_device_names_without_peer_names(stub_sdk):
    cluster = {
        "id": "cluster-uuid-2",
        "device_one_id": "cs-uuid-42",
        "device_two_id": "cs-uuid-43",
    }
    records_by_cs_id = {
        "cs-uuid-42": _record(),
        "cs-uuid-43": _record(asset=SWITCH_ASSET_PEER, cs_device_id="cs-uuid-43"),
    }

    entities, memberships = transform.virtual_chassis_to_entities(
        [cluster],
        records_by_cs_id=records_by_cs_id,
    )

    assert entities[0]._kw["virtual_chassis"]._kw["name"] == "sw-idf1 / sw-idf2"
    assert memberships["cs-uuid-42"]["name"] == "sw-idf1 / sw-idf2"


def test_virtual_chassis_ignores_identical_placeholder_peer_names(stub_sdk):
    """Fabric often reports peer_name 'Default' on both members -- that must not
    become the NetBox VirtualChassis name for every cluster."""
    cluster = {
        **INFERRED_CLUSTER,
        "device_one_peer_name": "Default",
        "device_two_peer_name": "Default",
    }
    records_by_cs_id = {
        "cs-uuid-42": _record(),
        "cs-uuid-43": _record(asset=SWITCH_ASSET_PEER, cs_device_id="cs-uuid-43"),
    }

    entities, _ = transform.virtual_chassis_to_entities([cluster], records_by_cs_id=records_by_cs_id)

    assert entities[0]._kw["virtual_chassis"]._kw["name"] == "sw-idf1 / sw-idf2"


def test_virtual_chassis_warns_on_duplicate_computed_names(stub_sdk, caplog):
    """Colliding human names are emitted as-is (no invented suffix).

    NetBox does not unique VirtualChassis.name; identity is the unique
    platformone_cluster_id custom field. The worker warns so upstream
    hostname collisions stay visible in the logs.
    """
    twin = {
        "device_id": 44,
        "host_name": "sw-idf1",
        "serial_number": "SN44",
        "mac_address": "aabbccddee01",
        "product_type": "FabricEngine_5320_48P_8XE",
        "function": "Fabric Engine",
        "is_connected": True,
        "site_name": "Assets-Site",
    }
    twin_peer = {**SWITCH_ASSET_PEER, "device_id": 45, "host_name": "sw-idf2", "serial_number": "SN45"}
    clusters = [
        INFERRED_CLUSTER,
        {
            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "device_one_id": "cs-uuid-44",
            "device_two_id": "cs-uuid-45",
            "device_one_peer_name": "peer-b",
            "device_two_peer_name": "peer-a",
        },
    ]
    records_by_cs_id = {
        "cs-uuid-42": _record(),
        "cs-uuid-43": _record(asset=SWITCH_ASSET_PEER, cs_device_id="cs-uuid-43"),
        "cs-uuid-44": _record(asset=twin, cs_device_id="cs-uuid-44"),
        "cs-uuid-45": _record(asset=twin_peer, cs_device_id="cs-uuid-45"),
    }

    entities, memberships = transform.virtual_chassis_to_entities(clusters, records_by_cs_id=records_by_cs_id)

    names = [e._kw["virtual_chassis"]._kw["name"] for e in entities]
    assert names == ["peer-a / peer-b", "peer-a / peer-b"]
    assert memberships["cs-uuid-44"]["name"] == "peer-a / peer-b"
    assert memberships["cs-uuid-44"]["cluster_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert "Duplicate VirtualChassis name" in caplog.text
    assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in caplog.text
    assert "platformone_cluster_id" in caplog.text


def test_devices_to_entities_attaches_virtual_chassis_membership(stub_sdk):
    """Membership Devices before VirtualChassis.master — NetBox rejects a
    master that is not yet assigned to the chassis on fresh create."""
    peer = _record(asset=SWITCH_ASSET_PEER, cs_device_id="cs-uuid-43")
    vc_entities, memberships = transform.virtual_chassis_to_entities(
        [INFERRED_CLUSTER],
        records_by_cs_id={"cs-uuid-42": _record(), "cs-uuid-43": peer},
    )

    entities = transform.devices_to_entities(
        [_record(), peer],
        virtual_chassis_entities=vc_entities,
        vc_memberships=memberships,
    )

    kinds = [next(iter(e._kw)) for e in entities]
    assert kinds == ["site", "device", "device", "virtual_chassis"]
    devices = {e._kw["device"]._kw["name"]: e._kw["device"]._kw for e in entities if "device" in e._kw}
    vc_ref = devices["sw-idf1"]["virtual_chassis"]._kw
    assert vc_ref["name"] == "peer-a / peer-b"
    assert cf(vc_ref["custom_fields"]["platformone_cluster_id"]._kw) == "cluster-uuid-1"
    assert devices["sw-idf1"]["vc_position"] == 1
    assert devices["sw-idf2"]["vc_position"] == 2
    master = entities[-1]._kw["virtual_chassis"]._kw["master"]._kw
    assert master["name"] == "sw-idf1"
    assert master["site"]._kw["name"] == "Assets-Site"
    assert master["role"]._kw["name"] == "Switch"
    assert master["device_type"]._kw["model"] == "5320-48P-8XE-FabricEngine"
