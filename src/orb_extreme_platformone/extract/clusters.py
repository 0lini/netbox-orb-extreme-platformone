"""InferredCluster extract (VirtualChassis source rows)."""

from __future__ import annotations

import logging

from orb_extreme_platformone.catalog import CLUSTER_MEMBER_FILTERS
from orb_extreme_platformone.client import PlatformOneApiError, PlatformOneClient

from .retrieve import retrieve_parallel

logger = logging.getLogger(__name__)


def _remapped_cluster(cluster: dict, inferred_to_asset: dict[str, str]) -> tuple[str, dict] | None:
    """Rewrite a cluster's member IDs from InferredDevice to AssetDevice UUIDs.

    Returns ``(cluster_id, cluster)``, or None when the row is unusable: either
    member out of scope (no remap) or no cluster id to dedupe both member-side
    queries on.
    """
    one = inferred_to_asset.get(str(cluster.get("device_one_id") or ""))
    two = inferred_to_asset.get(str(cluster.get("device_two_id") or ""))
    cluster_id = str(cluster.get("id") or "")
    if not one or not two or not cluster_id:
        return None
    return cluster_id, {**cluster, "device_one_id": one, "device_two_id": two}


def extract_inferred_clusters(client: PlatformOneClient, cs_device_ids: list[str]) -> list[dict]:
    """Fetch InferredCluster rows for the given AssetDevice UUIDs.

    Filtering `retrieve-inferred-cluster` by AssetDevice UUIDs silently
    returns zero rows: `device_one_id` / `device_two_id` are InferredDevice
    UUIDs. Resolve via `retrieve-inferred-device` (`asset_device_id`), query
    both cluster member filters, then rewrite member IDs back to
    AssetDevice UUIDs so transform can join on `cs_device_id`.

    Each member-side filter degrades independently: a failure on
    ``device_two_id`` still keeps rows from ``device_one_id`` (and vice versa)
    so a one-sided blip does not drop VirtualChassis for the whole tick.
    """
    if not cs_device_ids:
        return []

    inferred_to_asset: dict[str, str] = {}
    for device in client.retrieve("inferred-device", {"asset_device_id": cs_device_ids}):
        inferred_id = str(device.get("id") or "")
        asset_id = str(device.get("asset_device_id") or "")
        if inferred_id and asset_id:
            inferred_to_asset[inferred_id] = asset_id
    if not inferred_to_asset:
        return []

    inferred_ids = sorted(inferred_to_asset)
    by_id: dict[str, dict] = {}
    failures = 0
    jobs = [("inferred-cluster", {filter_field: inferred_ids}) for filter_field in CLUSTER_MEMBER_FILTERS]
    for filter_field, (_table, clusters, exc) in zip(
        CLUSTER_MEMBER_FILTERS,
        retrieve_parallel(client, jobs),
        strict=True,
    ):
        if exc is not None:
            failures += 1
            logger.warning(
                "ConfigState inferred-cluster filter %s failed, continuing with other member side: %s",
                filter_field,
                exc,
            )
            continue
        for cluster in clusters or []:
            remapped = _remapped_cluster(cluster, inferred_to_asset)
            if remapped is not None:
                cluster_id, row = remapped
                by_id[cluster_id] = row
    if failures == len(CLUSTER_MEMBER_FILTERS):
        # Both sides failed — surface as a hard extract error so backend can
        # degrade the whole VC phase (same as the previous all-or-nothing path).
        msg = "ConfigState inferred-cluster fetch failed on both member filters"
        raise PlatformOneApiError(msg)
    return [by_id[key] for key in sorted(by_id)]
