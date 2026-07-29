"""Contract checks against the Platform ONE OpenAPI specs.

The Platform ONE specs are published on the developer portal
(https://developer.extremeplatformone.com/api-reference) behind a
browser/login wall -- there is no stable unauthenticated URL to fetch them
from, so unlike the old live-XIQ contract job these checks run against
*local copies*: download the Asset Management and Config State specs from
the portal and point these env vars at them:

    PLATFORMONE_ASSETS_SPEC=/path/to/assets-openapi.json
    PLATFORMONE_CONFIGSTATE_SPEC=/path/to/configstate-openapi.json

Marked `contract` and skipped by default (and always skipped when the env
vars are unset), so the offline test suite stays self-contained. Run with
`pytest -m contract` after refreshing the spec downloads to catch upstream
drift in the endpoints/params client.py and backend.py hardcode.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orb_extreme_platformone.catalog import (
    CLUSTER_MEMBER_FILTERS,
    INTERFACE_ID_TABLES,
    PORT_TABLES,
    WIRELESS_TABLES,
)
from orb_extreme_platformone.client import configstate_response_key

pytestmark = pytest.mark.contract


def _load_spec(env_var: str) -> dict:
    path = os.environ.get(env_var)
    if not path:
        pytest.skip(f"{env_var} not set -- point it at a downloaded spec to run contract checks")
    return json.loads(Path(path).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def assets_spec() -> dict:
    return _load_spec("PLATFORMONE_ASSETS_SPEC")


@pytest.fixture(scope="session")
def configstate_spec() -> dict:
    return _load_spec("PLATFORMONE_CONFIGSTATE_SPEC")


def test_assets_devices_endpoint_and_params_still_exist(assets_spec) -> None:
    post = assets_spec["paths"]["/devices"]["post"]
    param_names = set()
    for param in post.get("parameters", []):
        resolved = param
        if "$ref" in param:
            resolved = assets_spec["components"]["parameters"][param["$ref"].rsplit("/", 1)[-1]]
        param_names.add(resolved["name"])
    assert {"page", "limit"} <= param_names


def test_assets_filter_still_supports_classification(assets_spec) -> None:
    schemas = assets_spec["components"]["schemas"]
    assert "classification" in schemas["ListDevicesRequestFilter"]["properties"]


def test_configstate_tables_client_uses_still_exist(configstate_spec) -> None:
    paths = configstate_spec["paths"]
    used_tables = [
        "asset-device",
        "asset-location",
        "inferred-device",
        "inferred-cluster",
        *(t for t, _ in PORT_TABLES.values()),
        *(t for t, _ in INTERFACE_ID_TABLES.values()),
        *(t for t, _ in WIRELESS_TABLES.values()),
    ]
    for table in used_tables:
        assert f"/retrieve-{table}" in paths, f"retrieve-{table} disappeared from ConfigState"


def test_configstate_response_keys_and_filter_fields_match(configstate_spec) -> None:
    """The response key must equal the schema name the spec wraps records in,
    and the batching filter field must exist on the table's GetRequest.
    """
    schemas = configstate_spec["components"]["schemas"]
    for table, filter_field in [
        ("asset-device", None),
        ("asset-location", "asset_device_id"),
        ("inferred-device", "asset_device_id"),
        ("inferred-cluster", None),
        *PORT_TABLES.values(),
        *INTERFACE_ID_TABLES.values(),
        *WIRELESS_TABLES.values(),
    ]:
        key = configstate_response_key(table)
        response_schema = schemas[f"{key}GetResponse"]["properties"]
        assert key in response_schema, f"{key}GetResponse no longer wraps records under {key}"
        if filter_field:
            request_schema = schemas[f"{key}GetRequest"]["properties"]
            assert filter_field in request_schema, f"{key}GetRequest lost filter field {filter_field}"


def test_asset_lag_schema_fields_still_exist(configstate_spec) -> None:
    """LAG sync depends on name/enabled/member_ports on config and names on members.

    LACP extras (`mode`, `lacp_key`, `load_balance_algo`, `dynamic`) are
    intentionally unmapped today but must remain on the schema so a future
    verified enum / Diode field can pick them up without a silent drop.
    """
    schemas = configstate_spec["components"]["schemas"]
    lag_config = schemas["AssetLagConfig"]["properties"]
    for field in (
        "asset_device_id",
        "asset_interface_id",
        "lag_number",
        "name",
        "enabled",
        "member_ports",
        "id",
        "mode",
        "lacp_key",
        "load_balance_algo",
        "dynamic",
    ):
        assert field in lag_config
    lag_state = schemas["AssetLagState"]["properties"]
    for field in ("asset_device_id", "asset_interface_id", "lag_number", "name", "member_ports", "id"):
        assert field in lag_state
    assert "interface_name" in schemas["AssetLagConfigMemberPort"]["properties"]
    assert "interface_name" in schemas["AssetLagStateMemberPort"]["properties"]


def test_inferred_cluster_member_filters_still_exist(configstate_spec) -> None:
    """VirtualChassis batching filters on both member sides of InferredCluster.

    Those member IDs are InferredDevice UUIDs (schema: "User device"), joined
    from AssetDevice via retrieve-inferred-device.asset_device_id.
    """
    request = configstate_spec["components"]["schemas"]["InferredClusterGetRequest"]["properties"]
    for filter_field in CLUSTER_MEMBER_FILTERS:
        assert filter_field in request, f"InferredClusterGetRequest lost {filter_field}"
    cluster = configstate_spec["components"]["schemas"]["InferredCluster"]["properties"]
    for field in ("device_one_id", "device_two_id", "device_one_peer_name", "device_two_peer_name", "id"):
        assert field in cluster
    inferred_device = configstate_spec["components"]["schemas"]["InferredDevice"]["properties"]
    assert "asset_device_id" in inferred_device
    assert "id" in inferred_device


def test_configstate_pagination_params_still_exist(configstate_spec) -> None:
    post = configstate_spec["paths"]["/retrieve-asset-device"]["post"]
    names = {p.get("name") for p in post.get("parameters", [])}
    assert {"page_number", "page_size"} <= names


# ---------------------------------------------------------------------------
# Declared scalar types
#
# The transform layer reads these fields with strict coercion: a bool that is
# not a bool, or an integer that is not an integer, is dropped rather than
# guessed at. That is only correct while the spec declares them as it does, so
# assert the declaration rather than trusting a comment.
# ---------------------------------------------------------------------------

BOOLEAN_FIELDS = [
    ("AssetPortConfig", "enabled"),
    ("AssetLagConfig", "enabled"),
    ("AssetWirelessInterface", "enabled"),
    ("AssetSsidConfig", "enabled"),
    ("AssetPortCapabilities", "management_port"),
    ("AssetPoePowerPortsState", "supported"),
    ("AssetInterfaceIpAddress", "is_primary"),
]

INTEGER_FIELDS = [
    ("AssetInterfaceIpAddress", "mask_length"),
    ("AssetPortState", "oper_speed"),
    ("AssetPortState", "connector_type"),
    ("AssetPortState", "oper_duplex"),
    ("AssetPortState", "oper_state"),
    ("AssetPortConfig", "duplex"),
    ("AssetInterfaceVlanProperties", "port_vlan"),
    ("AssetPoePowerPortsConfig", "classification"),
    ("AssetWirelessInterfaceState", "channel"),
    ("AssetWirelessInterfaceState", "channel_width"),
    ("AssetWirelessInterfaceState", "power"),
]


def _declared_type(spec: dict, schema: str, field: str) -> str | None:
    properties = spec["components"]["schemas"][schema].get("properties") or {}
    return (properties.get(field) or {}).get("type")


@pytest.mark.parametrize(("schema", "field"), BOOLEAN_FIELDS)
def test_boolean_fields_are_declared_boolean(configstate_spec, schema: str, field: str) -> None:
    assert _declared_type(configstate_spec, schema, field) == "boolean"


@pytest.mark.parametrize(("schema", "field"), INTEGER_FIELDS)
def test_integer_fields_are_declared_integer(configstate_spec, schema: str, field: str) -> None:
    assert _declared_type(configstate_spec, schema, field) == "integer"


def test_no_integer_is_serialised_as_a_string(configstate_spec) -> None:
    """`type: string, format: int*` is the idiom that would justify digit-string coercion."""
    offenders = [
        f"{name}.{field}"
        for name, schema in configstate_spec["components"]["schemas"].items()
        for field, prop in (schema.get("properties") or {}).items()
        if isinstance(prop, dict)
        and prop.get("type") == "string"
        and str(prop.get("format", "")).startswith("int")
    ]
    assert not offenders, f"string-encoded integers would need _coerce_int to accept them: {offenders}"
