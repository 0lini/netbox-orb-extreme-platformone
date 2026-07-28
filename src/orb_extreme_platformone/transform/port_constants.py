"""Constants used by switch port transforms."""

from __future__ import annotations

from orb_extreme_platformone.extract.tables import INTERFACE_ID_TABLES, PORT_TABLES

# Extreme Networks reserves VIDs 4060–4094 for internal use (e.g. Fabric
# Engine). These are filtered from Interface untagged/tagged memberships.
EXTREME_RESERVED_VLAN_VID_MIN = 4060
EXTREME_RESERVED_VLAN_VID_MAX = 4094

# Keys `ports_to_entities` reads from its `tables` dict — derived from extract
# catalogs so the sets cannot drift.
PORT_ENTITY_TABLE_KEYS = frozenset(PORT_TABLES) | frozenset(INTERFACE_ID_TABLES)

# NetBox requires Interface.type on create. SVI / orphan IP stubs use
# ``virtual``; LAG parents use ``lag``. Physical ports and AP radios assert
# type only from a verified speed/connector or radio_mode map — never invent
# ``other``.
VIRTUAL_INTERFACE_TYPE = "virtual"
LAG_INTERFACE_TYPE = "lag"

# ConfigState reports oper_speed / connector_type as integer codes with no
# OpenAPI value table; only codes verified against production hardware (or
# fixtures derived from that gear) are mapped. Duplex and PoE classification
# enums are verified from the Platform ONE data model (see README).
# Admin `enabled` and link `mark_connected` (IF-MIB-style oper_state) are
# both asserted so admin-down vs link-down stay distinguishable.
#
# Verified in-repo today: oper_speed 4, connector_type 1/2.
# Config-side speed integers remain unverified and are not used as fallbacks.
VERIFIED_OPER_SPEED_KBPS = {4: 1_000_000}
# retrieve-asset-port-state.oper_duplex / retrieve-asset-port-config.duplex:
# 0=UNSET, 1=HALF_DUPLEX, 2=FULL_DUPLEX, 3=NONE, 4=AUTO. Prefer oper_duplex;
# config duplex is fallback when oper is unset. AUTO is config-only.
VERIFIED_OPER_DUPLEX = {1: "half", 2: "full"}
VERIFIED_CONFIG_DUPLEX = {1: "half", 2: "full", 4: "auto"}
# retrieve-asset-poe-power-ports-config.classification → Diode poe_type.
# 0=UNSET, 1=AF, 2=AF_HIGH, 3=AT, 4=BT_TYPE3, 5=BT_TYPE4, 6=PRE_AT, 7=PRE_BT.
# Diode only accepts IEEE type1–4 / passive; AF_HIGH and PRE_* are omitted.
VERIFIED_POE_CLASSIFICATION = {
    1: "type1-ieee802.3af",
    3: "type2-ieee802.3at",
    4: "type3-ieee802.3bt",
    5: "type4-ieee802.3bt",
}
OPER_STATE_UP = 1

# (oper_speed, connector_type) -> NetBox interface type. connector_type:
# 1 = copper, 2 = fiber. Unlisted combinations leave `type` unset.
_TYPE_BY_SPEED_AND_CONNECTOR = {
    (4, 1): "1000base-t",
    (4, 2): "1000base-x-sfp",
}
