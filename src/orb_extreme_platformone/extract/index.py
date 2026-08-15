"""ConfigState fan-out indexes built from correlated device records."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable

    from orb_extreme_platformone.identity import DeviceRecord

logger = logging.getLogger(__name__)

__all__ = [
    "FanoutContext",
    "fanout_context",
    "records_by_cs_id",
]


def records_by_cs_id(
    records: list[DeviceRecord],
    *,
    predicate: Callable[[DeviceRecord], bool],
) -> dict[str, DeviceRecord]:
    """Index records by cs_device_id, keeping the first and warning on collisions.

    Port/radio/VC fan-out is keyed by ConfigState UUID; two Assets rows that
    correlate to the same UUID would otherwise silently overwrite each other.
    """
    by_id: dict[str, DeviceRecord] = {}
    for record in records:
        cs_id = record.cs_device_id
        if not cs_id or not predicate(record):
            continue
        if cs_id in by_id:
            logger.warning(
                "Duplicate ConfigState device id %s across Assets rows (%r and %r); "
                "keeping the first — %r will sync as a Device with no ports/radios/VC",
                cs_id,
                by_id[cs_id].label,
                record.label,
                record.label,
            )
            continue
        by_id[cs_id] = record
    return by_id


class FanoutContext(NamedTuple):
    """Per-device indexes a ConfigState fan-out phase needs.

    ``names`` omits records with no Assets hostname: NetBox cannot create a
    Device without one, so those are skipped rather than invented.
    """

    records: dict[str, DeviceRecord]
    cs_device_ids: list[str]
    names: dict[str, str]


def fanout_context(
    records: list[DeviceRecord],
    *,
    predicate: Callable[[DeviceRecord], bool],
    policy_name: str,
    kind: str,
) -> FanoutContext:
    """Build the ConfigState fan-out indexes for per-device table extracts."""
    by_cs_id = records_by_cs_id(records, predicate=predicate)
    names: dict[str, str] = {}
    for cs_device_id, record in by_cs_id.items():
        if record.name:
            names[cs_device_id] = record.name
        else:
            logger.warning(
                "Policy %s: skipping %s for %s: Assets host_name is empty",
                policy_name,
                kind,
                record.label,
            )
    return FanoutContext(by_cs_id, sorted(by_cs_id), names)
