"""VirtualChassis mapping from InferredCluster rows."""

from __future__ import annotations

from typing import TYPE_CHECKING

from netboxlabs.diode.sdk.ingester import Entity, VirtualChassis

from .common import PROVENANCE_TAGS, _device_ref, _virtual_chassis_kwargs, logger

# A chassis name needs two distinct names so a shared placeholder ("Default")
# cannot collapse every chassis to one NetBox name.
_MIN_DISTINCT_NAMES = 2


if TYPE_CHECKING:
    from orb_extreme_platformone.identity import DeviceRecord


def _virtual_chassis_name(cluster: dict, device_one_name: str, device_two_name: str) -> str | None:
    """Stable VirtualChassis name from peer names or member device names.

    Requires two distinct peer names so a shared placeholder like "Default" does
    not collapse every chassis to the same NetBox name. Falls back to distinct
    member device names. No invented ``cluster-{uuid}`` name.
    """
    peers = sorted(
        {name for name in (cluster.get("device_one_peer_name"), cluster.get("device_two_peer_name")) if name},
    )
    if len(peers) >= _MIN_DISTINCT_NAMES:
        return " / ".join(peers)
    members = sorted({device_one_name, device_two_name})
    if len(members) >= _MIN_DISTINCT_NAMES:
        return " / ".join(members)
    return None


def virtual_chassis_to_entities(
    clusters: list[dict],
    *,
    records_by_cs_id: dict[str, DeviceRecord],
) -> tuple[list[Entity], dict[str, dict]]:
    """Map ConfigState InferredCluster rows to VirtualChassis entities + memberships.

    `device_one_id` / `device_two_id` must already be AssetDevice UUIDs
    (backend remaps from InferredDevice IDs). Both members must be present in
    `records_by_cs_id` (already site-scoped); partial clusters are skipped so
    Diode never creates an orphan half-chassis.

    Returns (VC entities, {cs_device_id: {"name", "position", "cluster_id"?}})
    for `devices_to_entities` to attach `virtual_chassis` / `vc_position`.
    device_one is the primary/master per the InferredCluster schema.

    Devices absent from the membership map do not get a ``virtual_chassis``
    assertion. Diode upsert cannot clear a prior link, so a device that left
    a cluster may keep a stale NetBox VirtualChassis until edited manually.
    """
    entities: list[Entity] = []
    memberships: dict[str, dict] = {}
    used_names: set[str] = set()

    for cluster in clusters:
        one_id = str(cluster.get("device_one_id") or "")
        two_id = str(cluster.get("device_two_id") or "")
        if not one_id or not two_id:
            continue
        record_one = records_by_cs_id.get(one_id)
        record_two = records_by_cs_id.get(two_id)
        if record_one is None or record_two is None:
            continue

        name_one = record_one.name
        name_two = record_two.name
        if not name_one or not name_two:
            logger.warning(
                "Skipping InferredCluster %s: member device(s) have no Assets host_name",
                cluster.get("id"),
            )
            continue
        chassis_name = _virtual_chassis_name(cluster, name_one, name_two)
        if not chassis_name:
            logger.warning(
                "Skipping InferredCluster %s: no distinct peer or member names for VirtualChassis",
                cluster.get("id"),
            )
            continue
        # Colliding human names are emitted as-is: NetBox does not unique
        # VirtualChassis.name (verified 4.6), so identity is the unique
        # platformone_cluster_id custom field. Warn so upstream hostname
        # collisions stay visible in worker logs.
        if chassis_name in used_names:
            logger.warning(
                "Duplicate VirtualChassis name %r (cluster %s); "
                "identity relies on unique platformone_cluster_id",
                chassis_name,
                cluster.get("id"),
            )
        used_names.add(chassis_name)

        cluster_id = str(cluster["id"]) if cluster.get("id") else None
        entities.append(
            Entity(
                virtual_chassis=VirtualChassis(
                    **_virtual_chassis_kwargs(chassis_name, cluster_id),
                    master=_device_ref(record_one),
                    tags=PROVENANCE_TAGS,
                ),
            ),
        )
        # device_one is the primary/master, so it takes position 1.
        extra = {"cluster_id": cluster_id} if cluster_id else {}
        for position, member_id in ((1, one_id), (2, two_id)):
            memberships[member_id] = {"name": chassis_name, "position": position, **extra}

    return entities, memberships
