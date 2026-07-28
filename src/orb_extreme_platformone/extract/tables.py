"""Backwards-compatible re-export of the ConfigState catalogs.

The catalogs live in :mod:`orb_extreme_platformone.catalog` so the transform
layer can derive its table-key sets without importing ``extract``.
"""

from __future__ import annotations

from orb_extreme_platformone.catalog import (
    CLUSTER_MEMBER_FILTERS,
    FABRIC_DEVICE_TABLES,
    INTERFACE_ID_TABLES,
    PORT_TABLES,
    WIRELESS_TABLES,
)

__all__ = [
    "CLUSTER_MEMBER_FILTERS",
    "FABRIC_DEVICE_TABLES",
    "INTERFACE_ID_TABLES",
    "PORT_TABLES",
    "WIRELESS_TABLES",
]
