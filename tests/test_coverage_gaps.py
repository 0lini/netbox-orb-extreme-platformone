"""Targeted tests for uncovered lines and branches (100% coverage gate)."""

from __future__ import annotations

import importlib
import importlib.metadata
from importlib.metadata import PackageNotFoundError
from typing import NoReturn
from unittest.mock import MagicMock

import pytest
import requests
import responses
from worker.models import Config, Policy

from orb_extreme_platformone import transform
from orb_extreme_platformone.backend import Backend, _device_names, _records_by_cs_id, _scope_sites
from orb_extreme_platformone.client import (
    DEFAULT_BASE_URL,
    PlatformOneApiError,
    PlatformOneClient,
    _chunked,
    truncate_error_body,
)
from orb_extreme_platformone.extract import clusters as clusters_mod
from orb_extreme_platformone.extract import ports as ports_mod
from orb_extreme_platformone.extract import retrieve as retrieve_mod
from orb_extreme_platformone.extract.correlate import correlated_records, extract_cs_devices, index_unique
from orb_extreme_platformone.extract.ports import attach_interface_id_tables, collect_interface_ids
from orb_extreme_platformone.transform import common as common_mod
from orb_extreme_platformone.transform import wireless_rf
from orb_extreme_platformone.transform.fabric import device_fabric_custom_fields
from orb_extreme_platformone.transform.port_join import _capabilities_by_port
from orb_extreme_platformone.transform.wireless import _split_if_names
from orb_extreme_platformone.urls import _is_local_dev_host, require_https_url
from tests.backend_helpers import _mock_assets, _mock_cs, _mock_port_tables_empty, _policy
from tests.conftest import CS_SWITCH, PORT_CONFIG, PORT_STATE, SWITCH_ASSET, VLAN_PROPERTIES
from tests.transform_helpers import _record, _tables

ASSETS_URL = f"{DEFAULT_BASE_URL}/assets/v1/devices"
LOGIN_URL = f"{DEFAULT_BASE_URL}/login"


def test_package_lazy_backend_and_dir() -> None:
    import orb_extreme_platformone as pkg

    assert pkg.Backend.__name__ == "Backend"
    assert "Backend" in pkg.__dir__()
    with pytest.raises(AttributeError, match="no attribute 'nope'"):
        _ = pkg.nope


def test_app_version_falls_back_when_distribution_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda _name: (_ for _ in ()).throw(PackageNotFoundError()),
    )
    import orb_extreme_platformone.backend as backend_mod

    importlib.reload(backend_mod)
    try:
        assert backend_mod.APP_VERSION == "0.2.0"
    finally:
        importlib.reload(backend_mod)


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        (None, None),
        ({}, None),
        ({"sites": []}, None),
        ({"sites": ["*"]}, None),
        ({"sites": "*"}, None),
        ({"sites": ["HQ"]}, ["HQ"]),
        ("HQ", None),
        ({"sites": 123}, None),
    ],
)
def test_scope_sites_normalizes_policy_scope(scope, expected) -> None:
    assert _scope_sites(scope) == expected


def test_scope_sites_warns_on_string_scope(caplog) -> None:
    assert _scope_sites({"sites": "HQ"}) is None
    assert "Ignoring invalid policy scope.sites string" in caplog.text


def test_records_by_cs_id_warns_on_duplicate_ids(caplog) -> None:
    records = [
        {"cs_device_id": "cs-1", "asset": {"device_id": 1, "host_name": "a"}},
        {"cs_device_id": "cs-1", "asset": {"device_id": 2, "host_name": "b"}},
    ]
    by_id = _records_by_cs_id(records, predicate=lambda _r: True)
    assert by_id == {"cs-1": records[0]}
    assert "Duplicate ConfigState device id" in caplog.text


def test_device_names_skips_empty_hostname(caplog) -> None:
    records = {
        "cs-1": {"asset": {"device_id": 1, "host_name": ""}},
        "cs-2": {"asset": {"device_id": 2, "host_name": "sw-ok"}},
    }
    names = _device_names(records, policy_name="p", kind="ports")
    assert names == {"cs-2": "sw-ok"}
    assert "host_name is empty" in caplog.text


def test_chunked_with_non_positive_size_yields_whole_list() -> None:
    values = [1, 2, 3]
    assert list(_chunked(values, 0)) == [values]
    assert list(_chunked(values, -1)) == [values]


def test_truncate_error_body_with_tiny_limit() -> None:
    assert truncate_error_body("abcdef", limit=3) == "abc"
    assert truncate_error_body("ab", limit=3) == "ab"


def test_client_auth_headers_without_refresh_credentials() -> None:
    client = PlatformOneClient(api_token="tok")
    client._token_expiry = 0.0
    client._username = None
    client._password = None
    with pytest.raises(PlatformOneApiError, match="No credentials"):
        client._auth_headers()


@responses.activate
def test_login_redirect_raises() -> None:
    responses.add(responses.POST, LOGIN_URL, status=302)
    client = PlatformOneClient(username="u", password="p")
    with pytest.raises(PlatformOneApiError, match="unexpected redirect"):
        list(client.get_devices())


@responses.activate
def test_login_missing_access_token_raises() -> None:
    responses.add(responses.POST, LOGIN_URL, json={"expires_in": 3600}, status=200)
    client = PlatformOneClient(username="u", password="p")
    with pytest.raises(PlatformOneApiError, match="access_token"):
        list(client.get_devices())


@responses.activate
def test_api_redirect_raises() -> None:
    responses.add(responses.POST, ASSETS_URL, status=301)
    with pytest.raises(PlatformOneApiError, match="unexpected redirect"):
        list(PlatformOneClient(api_token="tok").get_devices())


@responses.activate
def test_api_non_object_json_raises() -> None:
    responses.add(
        responses.POST,
        ASSETS_URL,
        json=[],
        status=200,
    )
    with pytest.raises(PlatformOneApiError, match="non-object JSON"):
        list(PlatformOneClient(api_token="tok").get_devices())


def test_extract_cs_devices_skips_assets_without_serial() -> None:
    client = MagicMock()
    assert extract_cs_devices(client, [{"host_name": "x"}]) == []
    client.retrieve.assert_not_called()


def test_index_unique_skips_empty_keys(caplog) -> None:
    items = [{"serial": ""}, {"serial": "SN1"}]
    index = index_unique(items, lambda item: item.get("serial") or None, label="serial")
    assert index == {"SN1": items[1]}


@responses.activate
def test_correlated_records_degrades_when_location_fetch_fails(caplog) -> None:
    asset = {**SWITCH_ASSET}
    responses.add(
        responses.POST,
        ASSETS_URL,
        json={"data": [asset], "page": 1, "total_pages": 1, "total_count": 1},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-asset-device",
        json={
            "AssetDevice": [{"id": "cs-uuid-42", "serial_number": "SN42"}],
            "Pagination": {"total_pages": 1},
        },
        status=200,
    )
    responses.add(
        responses.POST,
        f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-asset-location",
        json={"error": "nope"},
        status=500,
    )
    client = PlatformOneClient(api_token="tok")
    records = correlated_records(client, [asset], "policy")
    assert records[0]["location"] is None
    assert "location fetch failed" in caplog.text


def test_extract_inferred_clusters_empty_input() -> None:
    client = MagicMock()
    assert clusters_mod.extract_inferred_clusters(client, []) == []


@responses.activate
def test_extract_inferred_clusters_skips_incomplete_device_rows() -> None:
    client = PlatformOneClient(api_token="tok")
    responses.add(
        responses.POST,
        f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-inferred-device",
        json={"InferredDevice": [{"id": "", "asset_device_id": "cs-1"}], "Pagination": {"total_pages": 1}},
        status=200,
    )
    assert clusters_mod.extract_inferred_clusters(client, ["cs-1"]) == []


@responses.activate
def test_extract_inferred_clusters_skips_out_of_scope_members() -> None:
    client = PlatformOneClient(api_token="tok")
    responses.add(
        responses.POST,
        f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-inferred-device",
        json={
            "InferredDevice": [{"id": "inf-1", "asset_device_id": "cs-1"}],
            "Pagination": {"total_pages": 1},
        },
        status=200,
    )
    cluster = {
        "id": "cluster-1",
        "device_one_id": "inf-1",
        "device_two_id": "inf-2",
    }
    responses.add(
        responses.POST,
        f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-inferred-cluster",
        json={"InferredCluster": [cluster], "Pagination": {"total_pages": 1}},
        status=200,
    )
    assert clusters_mod.extract_inferred_clusters(client, ["cs-1"]) == []


@responses.activate
def test_extract_inferred_clusters_skips_rows_without_cluster_id() -> None:
    client = PlatformOneClient(api_token="tok")
    responses.add(
        responses.POST,
        f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-inferred-device",
        json={
            "InferredDevice": [
                {"id": "inf-1", "asset_device_id": "cs-1"},
                {"id": "inf-2", "asset_device_id": "cs-2"},
            ],
            "Pagination": {"total_pages": 1},
        },
        status=200,
    )
    cluster = {"device_one_id": "inf-1", "device_two_id": "inf-2"}
    responses.add(
        responses.POST,
        f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-inferred-cluster",
        json={"InferredCluster": [cluster], "Pagination": {"total_pages": 1}},
        status=200,
    )
    assert clusters_mod.extract_inferred_clusters(client, ["cs-1", "cs-2"]) == []


def test_extract_inferred_clusters_skips_none_cluster_rows(monkeypatch) -> None:
    monkeypatch.setattr(
        clusters_mod,
        "retrieve_parallel",
        lambda _client, _jobs: [("inferred-cluster", None, None), ("inferred-cluster", None, None)],
    )
    client = MagicMock()
    client.retrieve.return_value = [{"id": "inf-1", "asset_device_id": "cs-1"}]
    assert clusters_mod.extract_inferred_clusters(client, ["cs-1"]) == []


def test_retrieve_parallel_empty_jobs() -> None:
    assert retrieve_mod.retrieve_parallel(MagicMock(), []) == []


def test_retrieve_ok_skips_none_rows(monkeypatch) -> None:
    monkeypatch.setattr(
        retrieve_mod,
        "retrieve_parallel",
        lambda _client, _jobs: [("asset-port-state", None, None)],
    )
    client = MagicMock()
    failed: list[str] = []
    rows = list(
        retrieve_mod.retrieve_ok(
            client,
            [("asset-port-state", {})],
            ["port_states"],
            policy_name="p",
            failed_tables=failed,
            degradation="ports",
        ),
    )
    assert rows == []
    assert failed == []


def test_extract_device_table_buckets_empty_inputs() -> None:
    client = MagicMock()
    buckets, failed = retrieve_mod.extract_device_table_buckets(
        client,
        [],
        {},
        policy_name="p",
        degradation="ports",
    )
    assert buckets == {}
    assert failed == []


def test_extract_device_table_buckets_ignores_unknown_device_rows(monkeypatch) -> None:
    monkeypatch.setattr(
        retrieve_mod,
        "retrieve_ok",
        lambda *_args, **_kwargs: iter(
            [
                (
                    ("port_states", ("asset-port-state", "asset_device_id")),
                    [{"asset_device_id": "other"}],
                ),
            ],
        ),
    )
    client = MagicMock()
    catalog = {"port_states": ("asset-port-state", "asset_device_id")}
    buckets, _failed = retrieve_mod.extract_device_table_buckets(
        client,
        ["cs-1"],
        catalog,
        policy_name="p",
        degradation="ports",
    )
    assert buckets["cs-1"]["port_states"] == []


def test_common_coercion_and_cidr_helpers() -> None:
    assert common_mod._interface_custom_fields(interface_id=None) == {}
    assert common_mod._coerce_bool(1) is True
    assert common_mod._coerce_bool(0) is False
    assert common_mod._coerce_bool("yes") is True
    assert common_mod._coerce_bool("no") is False
    assert common_mod._coerce_bool("maybe") is None
    assert common_mod._coerce_int(True) is None
    assert common_mod._explicit_cidr("") is None
    assert common_mod._explicit_cidr("10.0.0.1", mask_length=999) is None
    assert common_mod._explicit_cidr("not-an-ip", mask_length=24) is None


def test_devices_coord_site_merge_and_primary_ip_guards(stub_sdk) -> None:
    from orb_extreme_platformone.transform import devices as devices_mod

    assert devices_mod._coord(True) is None
    assert devices_mod._coord(float("nan")) is None
    assert devices_mod._coord("bad") is None
    site = devices_mod._site_kwargs("HQ", (95.0, 200.0))
    assert "latitude" not in site
    assert "longitude" not in site

    coords: dict[str, tuple[float | None, float | None]] = {"HQ": (10.0, None)}
    devices_mod._merge_site_coords(coords, "HQ", {"site_latitude": 11.0, "site_longitude": 12.0})
    assert coords["HQ"] == (10.0, 12.0)

    asset = {**SWITCH_ASSET, "host_name": "sw-idf1"}
    del asset["device_id"]
    entities = transform.devices_to_entities([_record(asset=asset)])
    device = next(e._kw["device"]._kw for e in entities if "device" in e._kw)
    assert "platformone_device_id" not in device.get("custom_fields", {})

    assert transform.primary_ip_device_entities([], primary_ips_by_cs_id={}) == []
    assert (
        transform.primary_ip_device_entities(
            [_record(cs_device_id=None)],
            primary_ips_by_cs_id={"cs-uuid-42": {"primary_ip4": "10.0.0.1/24"}},
        )
        == []
    )
    assert (
        transform.primary_ip_device_entities(
            [_record()],
            primary_ips_by_cs_id={"cs-other": {"primary_ip4": "10.0.0.1/24"}},
        )
        == []
    )
    unnamed = transform.primary_ip_device_entities(
        [_record(asset={**SWITCH_ASSET, "host_name": ""})],
        primary_ips_by_cs_id={"cs-uuid-42": {"primary_ip4": "10.0.0.1/24"}},
    )
    assert unnamed == []


def test_primary_ips_from_tables_mgmt_and_asset_match_paths() -> None:
    tables = {
        "interface_ips": [
            {"asset_interface_id": "if-1", "address": "10.0.0.5", "mask_length": 24},
        ],
        "port_capabilities": [
            {"asset_device_id": "cs-uuid-42", "port_name": "1/1", "management_port": True},
        ],
        "port_configs": [
            {"asset_device_id": "cs-uuid-42", "name": "1/1", "asset_interface_id": "if-1"},
        ],
        "port_states": [],
    }
    mgmt = transform.primary_ips_from_tables(tables, asset_ip="10.0.0.5/24")
    assert mgmt["primary_ip4"] == "10.0.0.5/24"

    primary = transform.primary_ips_from_tables(
        {
            **tables,
            "interface_ips": [
                {"asset_interface_id": "if-2", "address": "10.0.0.9", "mask_length": 24, "is_primary": True},
                {"asset_interface_id": "if-1", "address": "not-an-ip", "mask_length": 24},
            ],
        },
    )
    assert primary["primary_ip4"] == "10.0.0.9/24"


def test_orphan_ip_reuses_existing_virtual_interface(stub_sdk) -> None:
    ips = [{"asset_interface_id": "if-svi", "address": "10.0.10.1", "mask_length": 24}]
    second_ip = {"asset_interface_id": "if-svi", "address": "10.0.10.2", "mask_length": 24}
    tables = _tables(
        vlan_properties=[{**VLAN_PROPERTIES, "asset_interface_id": "if-svi", "interface_name": "vlan10"}],
        interface_ips=[*ips, second_ip],
        port_configs=[],
        port_states=[],
    )
    entities = transform.ports_to_entities(tables, device="sw-idf1")
    svi_interfaces = [
        e for e in entities if "interface" in e._kw and e._kw["interface"]._kw["name"] == "vlan10"
    ]
    assert len(svi_interfaces) == 1


def test_lag_member_list_ignores_non_dict_entries(stub_sdk) -> None:
    from orb_extreme_platformone.transform.lags import _member_interface_names

    assert _member_interface_names({"member_ports": ["bad", {"interface_name": "1/1"}]}) == ["1/1"]
    blank_member = {"member_ports": [{"interface_name": ""}, {"interface_name": "1/2"}]}
    assert _member_interface_names(blank_member) == ["1/2"]


def test_lag_admin_enabled_honors_false_port_config(stub_sdk) -> None:
    from orb_extreme_platformone.transform.lags import _lag_admin_enabled

    assert _lag_admin_enabled({"enabled": False}) is False


def test_physical_port_skips_unnamed_and_lag_name_collision(stub_sdk) -> None:
    from orb_extreme_platformone.transform.physical_ports import _physical_port_entities

    config = {**PORT_CONFIG, "name": "", "asset_interface_id": "if-empty"}
    state = {**PORT_STATE, "name": "", "asset_interface_id": "if-empty"}
    entities, emitted = _physical_port_entities(
        device="sw-idf1",
        configs={"if-empty": [config]},
        states={"if-empty": [state]},
        vlans={},
        capabilities={},
        poe_states={},
        poe_configs={},
        interface_ips={},
        lag_names={"1/1"},
        lag_interface_ids=set(),
        membership={},
    )
    assert entities == []
    assert emitted == {}


def test_capabilities_by_port_skips_blank_names() -> None:
    assert _capabilities_by_port([{"port_name": "", "asset_device_id": "cs-1"}]) == {}


def test_virtual_chassis_skip_paths(stub_sdk, caplog) -> None:
    entities, memberships = transform.virtual_chassis_to_entities(
        [{"device_one_id": "", "device_two_id": "cs-2"}],
        records_by_cs_id={},
    )
    assert entities == []
    assert memberships == {}

    entities, _memberships = transform.virtual_chassis_to_entities(
        [{"id": "c1", "device_one_id": "cs-1", "device_two_id": "cs-2"}],
        records_by_cs_id={
            "cs-1": _record(asset={**SWITCH_ASSET, "host_name": ""}),
            "cs-2": _record(asset={**SWITCH_ASSET, "device_id": 43, "host_name": ""}, cs_device_id="cs-2"),
        },
    )
    assert entities == []
    assert "no Assets host_name" in caplog.text

    entities, _memberships = transform.virtual_chassis_to_entities(
        [{"id": "c2", "device_one_id": "cs-1", "device_two_id": "cs-2"}],
        records_by_cs_id={
            "cs-1": _record(asset={**SWITCH_ASSET, "host_name": "same"}),
            "cs-2": _record(
                asset={**SWITCH_ASSET, "device_id": 43, "host_name": "same"},
                cs_device_id="cs-2",
            ),
        },
    )
    assert entities == []
    assert "no distinct peer or member names" in caplog.text

    no_cluster_cluster = {
        "device_one_id": "cs-1",
        "device_two_id": "cs-2",
        "device_one_peer_name": "a",
        "device_two_peer_name": "b",
    }
    no_cluster_id, _memberships = transform.virtual_chassis_to_entities(
        [no_cluster_cluster],
        records_by_cs_id={
            "cs-1": _record(),
            "cs-2": _record(
                asset={**SWITCH_ASSET, "device_id": 43, "host_name": "sw-2"},
                cs_device_id="cs-2",
            ),
        },
    )
    assert len(no_cluster_id) == 1
    vc_fields = no_cluster_id[0]._kw["virtual_chassis"]._kw.get("custom_fields", {})
    assert "platformone_cluster_id" not in vc_fields


def test_split_if_names_variants() -> None:
    assert _split_if_names(None) == []
    assert _split_if_names([]) == []
    assert _split_if_names("") == []
    assert _split_if_names("  ") == []
    assert _split_if_names([" wifi0 ", ""]) == ["wifi0"]
    assert _split_if_names("wifi0, wifi1") == ["wifi0", "wifi1"]
    assert _split_if_names("wifi0") == ["wifi0"]


def test_wireless_rf_edge_cases() -> None:
    assert wireless_rf._channel_frequency_mhz(None, 36) is None
    assert wireless_rf._channel_frequency_mhz("5GHz", "bad") is None
    assert wireless_rf._channel_frequency_mhz(None, 36) is None
    assert wireless_rf._channel_frequency_mhz("weird", 36) is None
    assert wireless_rf._channel_frequency_mhz("6", 1) == 5955.0
    assert wireless_rf._channel_frequency_mhz("5", 36) == 5180.0
    assert wireless_rf._radio_type("custom_11ax_mode") == "ieee802.11ax"
    assert wireless_rf._channel_width_mhz(15) is None


def test_wireless_skips_unnamed_radios_and_blank_ssids(stub_sdk) -> None:
    tables = {
        "cs-ap-1": {
            "wireless_interfaces": [
                {"asset_device_id": "cs-ap-1", "asset_interface_id": "r1", "enabled": True},
            ],
            "wireless_states": [{"asset_device_id": "cs-ap-1", "asset_interface_id": "r1"}],
            "ssid_configs": [{"asset_device_id": "cs-ap-1", "name": "", "if_names": "wifi0"}],
            "ssid_states": [{"asset_device_id": "cs-ap-1", "name": " ", "if_names": "wifi0"}],
        },
    }
    entities = transform.radios_to_entities(tables, device_names={"cs-ap-1": "ap-1"})
    assert not [e for e in entities if "interface" in e._kw]


def test_fabric_skips_blank_values() -> None:
    from orb_extreme_platformone import bootstrap

    fields = device_fabric_custom_fields(
        {
            "isis_global_configs": [{"manual_area_address": "   ", "area_name": "home"}],
            "isis_global_states": [],
            "spbm_instances": [],
        },
    )
    assert bootstrap.CF_ISIS_AREA in fields


def test_vlan_fields_ignore_non_dict_maps_and_reserved_only(stub_sdk) -> None:
    from orb_extreme_platformone.transform.vlans import _vlan_fields

    fields = _vlan_fields(
        [
            {
                "asset_interface_id": "if-1",
                "port_vlan": 0,
                "vlans": ["bad", {"vlan_number": 20}],
            },
        ],
    )
    assert fields["tagged_vlans"][0]._kw["vid"] == 20
    assert fields["mode"] == "tagged"

    reserved_only = _vlan_fields([{"port_vlan": 4094, "vlans": [{"vlan_number": 4094}]}])
    assert reserved_only == {}


def test_urls_local_host_and_missing_hostname() -> None:
    assert _is_local_dev_host(None) is False
    assert _is_local_dev_host("") is False
    with pytest.raises(ValueError, match="host"):
        require_https_url("https:///only-path", what="TEST")


@responses.activate
def test_bootstrap_request_rejects_redirect() -> None:
    from orb_extreme_platformone.bootstrap import _request

    netbox = "https://netbox.example.com"
    responses.add(responses.GET, f"{netbox}/api/extras/custom-fields/", status=302)
    with pytest.raises(requests.HTTPError, match="redirect"):
        _request("GET", f"{netbox}/api/extras/custom-fields/", "token")


@responses.activate
def test_run_bootstrap_enabled_calls_ensure_schema(monkeypatch) -> None:
    from orb_extreme_platformone import bootstrap

    seen: list[tuple[str, str]] = []

    def _fake_ensure(url, token) -> None:
        seen.append((url, token))

    monkeypatch.setattr(bootstrap, "ensure_schema", _fake_ensure)
    _mock_assets([SWITCH_ASSET])
    _mock_cs("asset-device", "AssetDevice", [])
    _mock_cs("asset-location", "AssetLocation", [])
    _mock_port_tables_empty()

    policy = Policy(
        config=Config(
            package="orb_extreme_platformone",
            BOOTSTRAP=True,
            NETBOX_API_URL="https://netbox.example.com",
            NETBOX_API_TOKEN="nb-tok",
            PLATFORMONE_API_TOKEN="tok",
        ),
        scope={"sites": ["*"]},
    )
    list(Backend().run("platformone_worker", policy))
    assert seen == [("https://netbox.example.com", "nb-tok")]


@responses.activate
def test_run_skips_port_fanout_for_unnamed_switch() -> None:
    unnamed = {**SWITCH_ASSET, "host_name": ""}
    _mock_assets([unnamed])
    _mock_cs("asset-device", "AssetDevice", [{**CS_SWITCH, "serial_number": "SN42"}])
    _mock_cs("asset-location", "AssetLocation", [])
    _mock_port_tables_empty()
    responses.add(
        responses.POST,
        f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-asset-port-config",
        json={"AssetPortConfig": [PORT_CONFIG], "Pagination": {"total_pages": 1}},
        status=200,
    )

    entities = list(Backend().run("platformone_worker", _policy()))
    assert not [e for e in entities if e.HasField("interface")]


def test_collect_interface_ids_skips_blank_ids() -> None:
    mapping = collect_interface_ids(
        {
            "cs-1": {
                "port_configs": [{"asset_interface_id": ""}],
                "port_states": [],
                "lag_configs": [],
                "lag_states": [],
                "vlan_properties": [],
                "poe_states": [],
            },
        },
    )
    assert mapping == {}


def test_attach_interface_id_tables_buckets_rows_and_skips_unknown_devices(monkeypatch) -> None:
    tables_by_device = {
        "cs-1": {
            "port_configs": [{"asset_interface_id": "if-1"}],
            "port_states": [],
            "lag_configs": [],
            "lag_states": [],
            "vlan_properties": [],
            "poe_states": [],
            "interface_ips": [],
        },
    }
    monkeypatch.setattr(
        ports_mod,
        "retrieve_ok",
        lambda *_args, **_kwargs: iter(
            [
                (
                    "interface_ips",
                    [
                        {"asset_interface_id": "if-1"},
                        {"asset_interface_id": "if-orphan"},
                    ],
                ),
            ],
        ),
    )
    attach_interface_id_tables(MagicMock(), tables_by_device, "p", [])
    assert tables_by_device["cs-1"]["interface_ips"] == [{"asset_interface_id": "if-1"}]


def test_attach_interface_id_tables_skips_rows_for_unknown_devices(monkeypatch) -> None:
    tables_by_device = {
        "cs-1": {
            "port_configs": [{"asset_interface_id": "if-1"}],
            "port_states": [],
            "lag_configs": [],
            "lag_states": [],
            "vlan_properties": [],
            "poe_states": [],
            "interface_ips": [],
        },
    }
    monkeypatch.setattr(
        ports_mod,
        "collect_interface_ids",
        lambda _tables: {"if-1": "cs-1", "if-other": "cs-missing"},
    )
    monkeypatch.setattr(
        ports_mod,
        "retrieve_ok",
        lambda *_args, **_kwargs: iter(
            [
                (
                    "interface_ips",
                    [
                        {"asset_interface_id": "if-1"},
                        {"asset_interface_id": "if-other"},
                    ],
                ),
            ],
        ),
    )
    attach_interface_id_tables(MagicMock(), tables_by_device, "p", [])
    assert tables_by_device["cs-1"]["interface_ips"] == [{"asset_interface_id": "if-1"}]


def test_common_interface_identity_and_inline_cidr() -> None:
    kwargs = common_mod._interface_identity_kwargs(device="sw", name="1/1", interface_id="if-1", enabled=True)
    assert kwargs["custom_fields"]
    without_id = common_mod._interface_identity_kwargs(device="sw", name="1/1")
    assert "custom_fields" not in without_id
    assert common_mod._explicit_cidr("10.0.0.1/24") == "10.0.0.1/24"


def test_devices_virtual_chassis_without_cluster_id(stub_sdk) -> None:
    peer = _record(asset={**SWITCH_ASSET, "device_id": 43, "host_name": "sw-2"}, cs_device_id="cs-2")
    vc_entities, memberships = transform.virtual_chassis_to_entities(
        [{"device_one_id": "cs-1", "device_two_id": "cs-2"}],
        records_by_cs_id={
            "cs-1": _record(cs_device_id="cs-1"),
            "cs-2": peer,
        },
    )
    entities = transform.devices_to_entities(
        [_record(cs_device_id="cs-1"), peer],
        virtual_chassis_entities=vc_entities,
        vc_memberships=memberships,
    )
    device = next(
        e._kw["device"]._kw
        for e in entities
        if "device" in e._kw and e._kw["device"]._kw["name"] == "sw-idf1"
    )
    vc_ref = device["virtual_chassis"]._kw
    assert "custom_fields" not in vc_ref


def test_primary_ips_invalid_and_asset_host_paths(monkeypatch) -> None:
    from orb_extreme_platformone.transform import ips as ips_mod

    invalid_mask = {"interface_ips": [{"address": "10.0.0.1", "mask_length": 99}]}
    assert transform.primary_ips_from_tables(invalid_mask) == {}
    matched = transform.primary_ips_from_tables(
        {
            "interface_ips": [{"asset_interface_id": "if-1", "address": "10.0.0.5", "mask_length": 24}],
            "port_capabilities": [],
            "port_configs": [],
            "port_states": [],
        },
        asset_ip="not-an-ip",
    )
    assert matched == {}
    host_match = transform.primary_ips_from_tables(
        {
            "interface_ips": [{"asset_interface_id": "if-1", "address": "10.0.0.5", "mask_length": 24}],
            "port_capabilities": [],
            "port_configs": [],
            "port_states": [],
        },
        asset_ip="10.0.0.5",
    )
    assert host_match["primary_ip4"] == "10.0.0.5/24"
    cidr_match = transform.primary_ips_from_tables(
        {
            "interface_ips": [{"asset_interface_id": "if-1", "address": "10.0.0.5", "mask_length": 24}],
            "port_capabilities": [],
            "port_configs": [],
            "port_states": [],
        },
        asset_ip="10.0.0.5/24",
    )
    assert cidr_match["primary_ip4"] == "10.0.0.5/24"
    no_host_match = transform.primary_ips_from_tables(
        {
            "interface_ips": [{"asset_interface_id": "if-1", "address": "10.0.0.5", "mask_length": 24}],
            "port_capabilities": [],
            "port_configs": [],
            "port_states": [],
        },
        asset_ip="10.0.0.9",
    )
    assert no_host_match == {}
    mgmt_no_match = transform.primary_ips_from_tables(
        {
            "interface_ips": [{"asset_interface_id": "if-other", "address": "10.0.0.5", "mask_length": 24}],
            "port_capabilities": [
                {"asset_device_id": "cs-uuid-42", "port_name": "1/1", "management_port": True},
            ],
            "port_configs": [
                {"asset_device_id": "cs-uuid-42", "name": "1/1", "asset_interface_id": "if-mgmt"},
                {"asset_device_id": "cs-uuid-42", "name": "1/2", "asset_interface_id": "if-other"},
            ],
            "port_states": [],
        },
    )
    assert mgmt_no_match == {}

    def _raise_bad_interface(_cidr) -> NoReturn:
        msg = "bad"
        raise ValueError(msg)

    monkeypatch.setattr(ips_mod, "_interface_ip_cidr", lambda _row: "10.0.0.1/24")
    monkeypatch.setattr(ips_mod.ipaddress, "ip_interface", _raise_bad_interface)
    assert ips_mod.primary_ips_from_tables({"interface_ips": [{}]}) == {}


def test_lag_enabled_and_mac_branches(stub_sdk) -> None:
    from orb_extreme_platformone.transform.lags import _lag_admin_enabled, _lag_kwargs

    assert _lag_admin_enabled({"enabled": None}) is True
    lag_kwargs = _lag_kwargs(
        device="sw",
        name="lag1",
        interface_id="lag-if",
        config={"name": "lag1"},
        vlan_records=[],
        port_state={"mac_address": "aa:bb:cc:dd:ee:ff"},
    )
    assert lag_kwargs["primary_mac_address"] == "AA:BB:CC:DD:EE:FF"
    no_mac = _lag_kwargs(
        device="sw",
        name="lag1",
        interface_id="lag-if",
        config={"name": "lag1"},
        vlan_records=[],
        port_state={"mac_address": ""},
    )
    assert "primary_mac_address" not in no_mac


def test_physical_port_skips_name_in_lag_names(stub_sdk) -> None:
    from orb_extreme_platformone.transform.physical_ports import _physical_port_entities

    entities, emitted = _physical_port_entities(
        device="sw-idf1",
        configs={"if-1": [PORT_CONFIG]},
        states={"if-1": [PORT_STATE]},
        vlans={},
        capabilities={},
        poe_states={},
        poe_configs={},
        interface_ips={},
        lag_names={"1/1"},
        lag_interface_ids=set(),
        membership={},
    )
    assert entities == []
    assert emitted == {}


def test_vlan_fields_warns_on_conflicting_port_vlan(stub_sdk, caplog) -> None:
    from orb_extreme_platformone.transform.vlans import _vlan_fields

    _vlan_fields(
        [
            {"asset_interface_id": "if-1", "port_vlan": 10, "vlans": [{"vlan_number": 20}]},
            {"asset_interface_id": "if-1", "port_vlan": 20, "vlans": [{"vlan_number": 30}]},
        ],
    )
    assert "Conflicting port_vlan" in caplog.text

    repeated = _vlan_fields(
        [
            {"asset_interface_id": "if-1", "port_vlan": 10, "vlans": [{"vlan_number": 20}]},
            {"asset_interface_id": "if-1", "port_vlan": 10, "vlans": [{"vlan_number": 30}]},
        ],
    )
    assert repeated["untagged_vlan"]._kw["vid"] == 10


def test_wireless_rf_matches_remaining_modes() -> None:
    assert wireless_rf._radio_type("legacy_11a_radio") == "ieee802.11a"
    assert wireless_rf._radio_type("legacy_11b_radio") == "ieee802.11b"
    assert wireless_rf._radio_type("totally_unknown_mode") is None


def test_orphan_ip_emits_one_interface_for_shared_name(stub_sdk) -> None:
    from orb_extreme_platformone.transform.ips import _orphan_ip_entities

    interface_ips = {
        "if-a": [{"address": "10.0.0.1", "mask_length": 24}],
        "if-b": [{"address": "10.0.0.2", "mask_length": 24}],
    }
    entities = _orphan_ip_entities(
        device="sw-idf1",
        interface_ips=interface_ips,
        emitted_keys={},
        interface_names={"if-a": "vlan10", "if-b": "vlan10"},
    )
    assert sum(1 for entity in entities if "interface" in entity._kw) == 1
    assert sum(1 for entity in entities if "ip_address" in entity._kw) == 2


def test_require_https_url_rejects_missing_hostname() -> None:
    with pytest.raises(ValueError, match="host"):
        require_https_url("https://:8080", what="TEST")
