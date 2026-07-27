"""InferredCluster extract (VirtualChassis source rows)."""

from __future__ import annotations

import logging

from orb_extreme_platformone.client import PlatformOneApiError, PlatformOneClient

from .tables import CLUSTER_MEMBER_FILTERS

logger = logging.getLogger("orb_extreme_platformone.extract")


def extract_inferred_clusters(client: PlatformOneClient, asset_device_ids: list[str]) -> list[dict]:
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
    if not asset_device_ids:
        return []

    inferred_to_asset: dict[str, str] = {}
    for device in client.retrieve("inferred-device", {"asset_device_id": asset_device_ids}):
        inferred_id = str(device.get("id") or "")
        asset_id = str(device.get("asset_device_id") or "")
        if inferred_id and asset_id:
            inferred_to_asset[inferred_id] = asset_id
    if not inferred_to_asset:
        return []

    inferred_ids = sorted(inferred_to_asset)
    by_id: dict[str, dict] = {}
    failures = 0
    for filter_field in CLUSTER_MEMBER_FILTERS:
        try:
            clusters = list(client.retrieve("inferred-cluster", {filter_field: inferred_ids}))
        except PlatformOneApiError as exc:
            failures += 1
            logger.warning(
                "ConfigState inferred-cluster filter %s failed, continuing with other member side: %s",
                filter_field,
                exc,
            )
            continue
        for cluster in clusters:
            one = str(cluster.get("device_one_id") or "")
            two = str(cluster.get("device_two_id") or "")
            one_asset = inferred_to_asset.get(one)
            two_asset = inferred_to_asset.get(two)
            # Skip when either member is out of scope (no AssetDevice remap).
            if not one_asset or not two_asset:
                continue
            remapped = {
                **cluster,
                "device_one_id": one_asset,
                "device_two_id": two_asset,
            }
            cluster_id = str(remapped.get("id") or "")
            if cluster_id:
                by_id[cluster_id] = remapped
    if failures == len(CLUSTER_MEMBER_FILTERS):
        # Both sides failed — surface as a hard extract error so backend can
        # degrade the whole VC phase (same as the previous all-or-nothing path).
        raise PlatformOneApiError("ConfigState inferred-cluster fetch failed on both member filters")
    return [by_id[key] for key in sorted(by_id)]
