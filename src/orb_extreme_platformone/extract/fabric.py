"""Batched ConfigState ISIS / SPBM (fabric) extracts for Device custom fields."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .retrieve import extract_device_table_buckets
from .tables import FABRIC_DEVICE_TABLES

if TYPE_CHECKING:
    from orb_extreme_platformone.client import PlatformOneClient


def extract_fabric_tables(
    client: PlatformOneClient,
    cs_device_ids: list[str],
    policy_name: str,
) -> tuple[dict[str, dict[str, list[dict]]], list[str]]:
    """Batched device-filtered ISIS/SPBM tables for fabric identity CFs.

    Returns ``(tables_by_device, failed_tables)``. Independent tables retrieve
    concurrently; a failed table degrades that CF source for the tick.
    """
    return extract_device_table_buckets(
        client,
        cs_device_ids,
        FABRIC_DEVICE_TABLES,
        policy_name=policy_name,
        degradation="fabric CF sync without it",
    )
