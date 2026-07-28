"""Batched ConfigState port / LAG / VLAN / PoE / interface-IP extracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from orb_extreme_platformone.client import CONFIGSTATE_FILTER_CHUNK_SIZE

from .retrieve import extract_device_table_buckets, retrieve_ok
from .tables import INTERFACE_ID_TABLES, PORT_TABLES

if TYPE_CHECKING:
    from orb_extreme_platformone.client import PlatformOneClient

# Capabilities have no asset_interface_id; derive the rest from PORT_TABLES so
# a new interface-id-bearing table cannot be forgotten here.
_INTERFACE_ID_SOURCE_KEYS = tuple(key for key in PORT_TABLES if key != "port_capabilities")


def collect_interface_ids(
    tables_by_device: dict[str, dict[str, list[dict]]],
) -> dict[str, str]:
    """Map each collected asset_interface_id to its device UUID.

    Scans tables that carry ``asset_interface_id`` (port/LAG/VLAN/PoE).
    ``port_capabilities`` has no interface UUID and is skipped. VLAN rows
    matter so interface-IP retrieves cover VLAN-facing interfaces that never
    appear in port/LAG/PoE rows.
    """
    interface_to_device: dict[str, str] = {}
    for device_id, tables in tables_by_device.items():
        for key in _INTERFACE_ID_SOURCE_KEYS:
            for row in tables.get(key) or []:
                interface_id = str(row.get("asset_interface_id") or "")
                if interface_id:
                    interface_to_device.setdefault(interface_id, device_id)
    return interface_to_device


def attach_interface_id_tables(
    client: PlatformOneClient,
    tables_by_device: dict[str, dict[str, list[dict]]],
    policy_name: str,
    failed_tables: list[str],
) -> None:
    """Fetch interface IPs by collected interface UUIDs.

    ``retrieve-asset-interface-ip-address`` has no device filter; rows are
    bucketed back onto devices via the interface→device map from port/LAG/
    VLAN/PoE rows.

    The UUID list spans every in-scope switch, so it is chunked here rather
    than inside ``client.retrieve``: chunking at this level lets the chunks run
    concurrently. Doing it in the client walks them one at a time, which on a
    large estate is the single biggest wall-clock cost of a tick.
    """
    interface_to_device = collect_interface_ids(tables_by_device)
    for tables in tables_by_device.values():
        for key in INTERFACE_ID_TABLES:
            tables.setdefault(key, [])
    if not interface_to_device:
        return

    interface_ids = sorted(interface_to_device)
    jobs: list[tuple[str, dict]] = []
    contexts: list[str] = []
    for key, (table, filter_field) in INTERFACE_ID_TABLES.items():
        for start in range(0, len(interface_ids), CONFIGSTATE_FILTER_CHUNK_SIZE):
            chunk = interface_ids[start : start + CONFIGSTATE_FILTER_CHUNK_SIZE]
            jobs.append((table, {filter_field: chunk}))
            contexts.append(key)

    # retrieve_ok tolerates repeated context values and records per-chunk
    # failures independently, so per-chunk degradation is preserved.
    for key, rows in retrieve_ok(
        client,
        jobs,
        contexts,
        policy_name=policy_name,
        failed_tables=failed_tables,
        degradation="ports sync without it",
    ):
        for row in rows:
            interface_id = str(row.get("asset_interface_id") or "")
            device_id = interface_to_device.get(interface_id)
            if device_id and device_id in tables_by_device:
                tables_by_device[device_id][key].append(row)


def extract_port_tables(
    client: PlatformOneClient,
    device_ids: list[str],
    policy_name: str,
) -> tuple[dict[str, dict[str, list[dict]]], list[str]]:
    """Batched device-filtered port/LAG tables, then interface-UUID tables.

    Returns ``(tables_by_device, failed_tables)``. Independent device-filtered
    tables retrieve concurrently; interface-IP tables run afterward once
    ``asset_interface_id`` values are known. LAG membership comes from
    nested ``member_ports`` on lag-config rows, falling back to lag-state
    when config omits members.
    """
    tables_by_device, failed_tables = extract_device_table_buckets(
        client,
        device_ids,
        PORT_TABLES,
        policy_name=policy_name,
        degradation="ports sync without it",
    )
    attach_interface_id_tables(client, tables_by_device, policy_name, failed_tables)
    return tables_by_device, failed_tables
