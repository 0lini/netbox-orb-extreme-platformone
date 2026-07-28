"""Join and port-name normalization helpers for switch port transforms."""

from __future__ import annotations

from collections import defaultdict

from orb_extreme_platformone.identity import native_port_name

from .common import logger


def _interface_id_of(record: dict) -> str:
    """Join key across ConfigState port tables: the row's asset_interface_id."""
    return str(record.get("asset_interface_id") or "")


def _group_by_interface_id(records: list[dict]) -> dict[str, list[dict]]:
    """Group rows by asset_interface_id, dropping rows that carry none."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        interface_id = _interface_id_of(record)
        if interface_id:
            grouped[interface_id].append(record)
    return grouped


def _first_row(grouped: dict[str, list[dict]], interface_id: str, *, table: str) -> dict:
    """First row for a join key, or `{}` when the key is absent.

    Warns when multiple rows share the key: callers that take only the first
    row would otherwise silently drop siblings.
    """
    rows = grouped.get(interface_id)
    if not rows:
        return {}
    if len(rows) > 1:
        logger.warning(
            "Multiple %s rows share asset_interface_id %r (%d rows); using the first",
            table,
            interface_id,
            len(rows),
        )
    return rows[0]


def _optional_first_row(grouped: dict[str, list[dict]], interface_id: str, *, table: str) -> dict | None:
    """First row when the id is present, else None (distinguishes missing PoE)."""
    if interface_id not in grouped:
        return None
    return _first_row(grouped, interface_id, table=table)


def _capabilities_by_port(records: list[dict]) -> dict[tuple[str, str], dict]:
    """Index AssetPortCapabilities by (asset_device_id, port_name).

    Capabilities have no asset_interface_id. `port_name` alone is reused across
    every switch (e.g. `1:43`), so the ConfigState device id must be part of
    the join key — same device scope other port tables get from backend
    bucketing / asset_interface_id.
    """
    by_port: dict[tuple[str, str], dict] = {}
    for record in records:
        name = record.get("port_name")
        if not name:
            continue
        device_id = str(record.get("asset_device_id") or "")
        port_key = (device_id, str(name))
        if port_key in by_port:
            logger.warning(
                "Multiple port_capabilities rows share port_name %r on device %r; using the first",
                str(name),
                device_id or "?",
            )
            continue
        by_port[port_key] = record
    return by_port


# Row fields that carry a front-panel port name across the ConfigState port
# tables (port/LAG `name`, vlan/member `interface_name`, capabilities
# `port_name`). Normalized together so name-based joins stay consistent.
_PORT_NAME_FIELDS = ("name", "interface_name", "port_name")


def _native_port_name_row(row: dict, function: str | None) -> dict:
    new = dict(row)
    for field in _PORT_NAME_FIELDS:
        value = new.get(field)
        if isinstance(value, str):
            new[field] = native_port_name(value, function)
    if isinstance(new.get("member_ports"), list):
        new["member_ports"] = [
            _native_port_name_row(member, function) if isinstance(member, dict) else member
            for member in new["member_ports"]
        ]
    return new


def _native_port_name_tables(tables: dict[str, list[dict]], function: str | None) -> dict[str, list[dict]]:
    """Copy `tables` with slot:port names rewritten to the OS-native notation.

    Rows are copied (not mutated) so callers' table dicts stay untouched.
    """
    return {
        table_key: [_native_port_name_row(row, function) for row in rows or []]
        for table_key, rows in tables.items()
    }
