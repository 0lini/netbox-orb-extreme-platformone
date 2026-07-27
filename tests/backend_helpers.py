"""Shared helpers for backend tests."""

from __future__ import annotations

import responses
from worker.models import Config, Policy

from orb_extreme_platformone.backend import FABRIC_DEVICE_TABLES, INTERFACE_ID_TABLES, PORT_TABLES
from orb_extreme_platformone.client import DEFAULT_BASE_URL, configstate_response_key

ASSETS_URL = f"{DEFAULT_BASE_URL}/assets/v1/devices"


def _cs_url(table: str) -> str:
    return f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-{table}"


def _mock_assets(devices: list[dict]):
    responses.add(
        responses.POST,
        ASSETS_URL,
        json={"data": devices, "page": 1, "total_pages": 1, "total_count": len(devices)},
        status=200,
    )


def _mock_cs(table: str, key: str, records: list[dict], status: int = 200):
    body = {key: records, "Pagination": {"total_pages": 1}} if status == 200 else {"error": "boom"}
    responses.add(responses.POST, _cs_url(table), json=body, status=status)


def _mock_empty_clusters():
    """Existing port-focused tests do not care about VC; no InferredDevice rows
    means the backend skips retrieve-inferred-cluster entirely."""
    _mock_cs("inferred-device", "InferredDevice", [])


def _mock_empty_port_and_lag_tables(*, include_fabric: bool = True):
    """Empty mocks for every PORT_TABLES entry so adding a table cannot drift.

    Set ``include_fabric=False`` when a test supplies its own fabric rows
    (responses matches the first registered mock for a URL).
    """
    for table, _ in PORT_TABLES.values():
        _mock_cs(table, configstate_response_key(table), [])
    if include_fabric:
        _mock_empty_fabric_tables()


def _mock_empty_fabric_tables():
    """Empty mocks for ISIS/SPBM fabric identity tables."""
    for table, _ in FABRIC_DEVICE_TABLES.values():
        _mock_cs(table, configstate_response_key(table), [])


def _mock_interface_id_tables_empty():
    """Empty mocks for interface-IP (fetched when interface UUIDs exist)."""
    for table, _ in INTERFACE_ID_TABLES.values():
        _mock_cs(table, configstate_response_key(table), [])


def _mock_port_tables_empty():
    _mock_empty_port_and_lag_tables()
    _mock_empty_clusters()


def _policy(**config_overrides) -> Policy:
    config = Config(
        package="orb_extreme_platformone",
        BOOTSTRAP=False,
        PLATFORMONE_API_TOKEN="tok",
        **config_overrides,
    )
    return Policy(config=config, scope=config_overrides.get("scope", {"sites": ["*"]}))
