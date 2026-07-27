"""VLAN helpers for switch interface transforms."""

from __future__ import annotations

from netboxlabs.diode.sdk.ingester import VLAN

from .common import _coerce_int, logger
from .port_constants import EXTREME_RESERVED_VLAN_VID_MAX, EXTREME_RESERVED_VLAN_VID_MIN


def _is_extreme_reserved_vlan(vid: int) -> bool:
    """True for Extreme reserved internal VLAN IDs (4060–4094 inclusive)."""
    return EXTREME_RESERVED_VLAN_VID_MIN <= vid <= EXTREME_RESERVED_VLAN_VID_MAX


def _vlan_ref(vid: int) -> VLAN:
    """Diode VLAN membership ref with NetBox-required name.

    NetBox rejects blank VLAN names on create. Switch-local names are not
    site-scoped, so use the VID string as a stable placeholder (same VID on
    every switch at a site shares one NetBox VLAN).
    """
    return VLAN(vid=vid, name=str(vid))


def _vlan_records_for(vlans_by_id: dict[str, list[dict]], *, interface_id: str | None) -> list[dict]:
    """VLAN rows for an interface, joined only on asset_interface_id."""
    if interface_id and interface_id in vlans_by_id:
        return vlans_by_id[interface_id]
    return []


def _vlan_fields(vlan_records: list[dict]) -> dict:
    """untagged_vlan / tagged_vlans / mode from AssetInterfaceVlanProperties rows.

    `port_vlan` is the untagged VLAN; the nested `vlans` list is every VLAN
    mapped onto the interface, so the tagged set is that list minus the
    untagged VLAN. Extreme reserved VIDs (4060–4094) are omitted from both.
    Interfaces with no VLAN rows — or only reserved VIDs after filtering —
    assert none of the three: on Fabric Engine a port can be mapped straight
    into an I-SID instead of a VLAN, and inventing an access mode would
    misrepresent configuration. VLAN refs use `vid` plus `name=str(vid)`
    (NetBox requires a name; switch-local names are not site-scoped).

    Conflicting ``port_vlan`` values across rows for the same interface keep
    the first and warn (same posture as ``_first_row`` join collisions).
    """
    untagged: int | None = None
    mapped: set[int] = set()
    for record in vlan_records:
        port_vlan = _coerce_int(record.get("port_vlan"))
        if port_vlan is not None and port_vlan > 0:
            if untagged is None:
                untagged = port_vlan
            elif port_vlan != untagged:
                logger.warning(
                    "Conflicting port_vlan values %s and %s on interface %r; keeping the first",
                    untagged,
                    port_vlan,
                    record.get("asset_interface_id") or record.get("interface_name") or "?",
                )
        for vlan_map in record.get("vlans") or []:
            number = _coerce_int(vlan_map.get("vlan_number")) if isinstance(vlan_map, dict) else None
            if number is not None and number > 0:
                mapped.add(number)
    if untagged is not None and _is_extreme_reserved_vlan(untagged):
        untagged = None
    tagged = sorted(
        vid
        for vid in (mapped - {untagged} if untagged is not None else mapped)
        if not _is_extreme_reserved_vlan(vid)
    )

    fields: dict = {}
    if untagged is not None:
        fields["untagged_vlan"] = _vlan_ref(untagged)
    if tagged:
        fields["tagged_vlans"] = [_vlan_ref(vid) for vid in tagged]
        fields["mode"] = "tagged"
    elif untagged is not None:
        fields["mode"] = "access"
    return fields
