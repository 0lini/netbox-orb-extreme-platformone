"""LAG interface entity builders."""

from __future__ import annotations

from typing import NamedTuple

from netboxlabs.diode.sdk.ingester import Device, Entity, Interface

from .common import _coerce_bool, _normalized_mac, logger
from .ips import _ip_entities_for_interface
from .physical_ports import _iface_base_kwargs
from .port_constants import LAG_INTERFACE_TYPE
from .port_join import _first_row, _group_by_interface_id, _optional_first_row
from .vlans import _vlan_fields, _vlan_records_for


def _lag_name(config: dict, state: dict) -> str | None:
    """LAG Interface name from Platform ONE ``name`` (switches always set one).

    No ``lag-{n}`` invention from ``lag_number`` — NetBox requires a name, but
    inventing one would diverge from the switch's auto-generated LAG name.
    """
    name = config.get("name") or state.get("name")
    return str(name) if name else None


def _lag_admin_enabled(port_config: dict | None = None) -> bool:
    """Admin state for a LAG parent — always an explicit bool.

    Prefer ``AssetPortConfig.enabled`` when the LAG interface id also appears
    in port tables (same admin signal as physical ports). Bare
    ``AssetLagConfig.enabled`` is false for every in-service MLT in production
    dry-runs while member ports are admin-up; trusting that value disables all
    LAG parents in NetBox. Diode/protobuf also maps an omitted bool to false,
    so this helper never leaves ``enabled`` unset (default admin-up).
    """
    if port_config:
        port_enabled = _coerce_bool(port_config.get("enabled"))
        if port_enabled is not None:
            return port_enabled
    return True


def _member_interface_names(lag_row: dict) -> list[str]:
    """Member port names from a nested `member_ports` list on a LAG row."""
    names: list[str] = []
    seen: set[str] = set()
    for member in lag_row.get("member_ports") or []:
        if not isinstance(member, dict):
            continue
        name = member.get("interface_name")
        if name and str(name) not in seen:
            seen.add(str(name))
            names.append(str(name))
    return names


def _record_membership(membership: dict[str, str], *, member: str, lag: str) -> None:
    """Bind member→LAG; warn when the same member claims two parents."""
    existing = membership.get(member)
    if existing is not None and existing != lag:
        logger.warning(
            "Port %r listed as member of both %r and %r; keeping the first",
            member,
            existing,
            lag,
        )
        return
    membership.setdefault(member, lag)


LagRow = tuple[str, dict, dict]


class LagResult(NamedTuple):
    """LAG entities plus the join bookkeeping physical-port mapping needs.

    Named because positions 2/3 are both ``set[str]`` and 4/5 are both
    ``dict[str, str]``: transposing either pair type-checks cleanly and
    silently corrupts LAG membership.
    """

    entities: list[Entity]
    lag_names: set[str]
    lag_interface_ids: set[str]
    membership: dict[str, str]
    emitted_keys: dict[str, str]


def _joined_lag_rows(configs: list[dict], states: list[dict]) -> list[LagRow]:
    configs_by_id = _group_by_interface_id(configs)
    states_by_id = _group_by_interface_id(states)
    return [
        (
            interface_id,
            _first_row(configs_by_id, interface_id, table="lag_configs"),
            _first_row(states_by_id, interface_id, table="lag_states"),
        )
        for interface_id in sorted(set(configs_by_id) | set(states_by_id))
    ]


def _lag_membership(joined_rows: list[LagRow]) -> dict[str, str]:
    """Map member port names to LAG names from config and/or state rows.

    Prefer ``member_ports`` on lag-config; when config omits members (or a LAG
    appears only in lag-state), use nested members on the state row. The LAG
    ``name`` may appear on either side of the same ``asset_interface_id``.
    """
    membership: dict[str, str] = {}
    for _interface_id, config, state in joined_rows:
        lag = _lag_name(config, state)
        if not lag:
            continue
        members = _member_interface_names(config) or _member_interface_names(state)
        for member in members:
            _record_membership(membership, member=member, lag=lag)
    return membership


def _lag_kwargs(
    *,
    device: Device,
    name: str,
    interface_id: str | None,
    config: dict,
    vlan_records: list[dict],
    poe_state: dict | None = None,
    poe_config: dict | None = None,
    port_config: dict | None = None,
    port_state: dict | None = None,
) -> dict:
    """Build Diode kwargs for a LAG parent interface.

    Native fields from AssetLagConfig/State: `type=lag`, name, and
    `platformone_interface_id` (`asset_interface_id`). Admin `enabled` prefers
    a duplicate AssetPortConfig row when present; otherwise defaults to True
    (Platform ONE's AssetLagConfig.enabled is observed always-false for
    in-service MLTs, and Diode maps an omitted bool to false). Shared joins
    on that interface id fill VLAN trunk/access, PoE, and (separately)
    IPAddress entities. When port config/state also lists the
    same id, pull fields lag tables lack (`description`, MAC) — never
    `mark_connected` (NetBox rejects it on type=lag) and never
    speed/duplex/connector `type`, which would overwrite `type=lag`. VLANs
    come only from vlan-properties.

    AssetLagConfig also carries LACP `mode` / `lacp_key` / `load_balance_algo`
    / `dynamic` with verified integer enums (see README), but Diode's Interface
    has no matching LACP fields — leave them unmapped. `lag_number` is unused
    for NetBox naming (switches always supply `name`); it is not a second
    custom field (redundant with `platformone_interface_id`).
    """
    kwargs = _iface_base_kwargs(
        device=device,
        name=name,
        interface_id=interface_id,
        config=config,
        poe_state=poe_state,
        poe_config=poe_config,
    )
    kwargs["type"] = LAG_INTERFACE_TYPE
    kwargs["enabled"] = _lag_admin_enabled(port_config)

    kwargs.update(_vlan_fields(vlan_records))

    if port_config and port_config.get("description"):
        kwargs["description"] = port_config["description"]
    if port_state:
        # Do not assert mark_connected on LAG parents: NetBox rejects
        # "LAG interfaces cannot be marked as connected" and the whole
        # change set (including CF / VLAN / MAC) fails to apply.
        mac = _normalized_mac(port_state.get("mac_address"))
        if mac:
            kwargs["primary_mac_address"] = mac
    return kwargs


def _lag_entities(
    *,
    device: Device,
    lag_configs: list[dict],
    lag_states: list[dict],
    vlans: dict[str, list[dict]],
    poe_states: dict[str, list[dict]],
    poe_configs: dict[str, list[dict]],
    interface_ips: dict[str, list[dict]],
    port_configs: dict[str, list[dict]] | None = None,
    port_states: dict[str, list[dict]] | None = None,
) -> LagResult:
    """Emit LAG parent interfaces. Returns entities plus join bookkeeping."""
    joined_rows = _joined_lag_rows(lag_configs, lag_states)
    port_configs = port_configs or {}
    port_states = port_states or {}

    # Only suppress duplicate physical-port rows for LAGs we actually emit.
    # Unnamed LAG rows are skipped below; their interface ids must still be
    # free to surface as ordinary ports when port tables also list them.
    lag_interface_ids: set[str] = set()
    membership = _lag_membership(joined_rows)
    lag_names: set[str] = set()
    entities: list[Entity] = []
    emitted_keys: dict[str, str] = {}

    for interface_id, config, state in joined_rows:
        name = _lag_name(config, state)
        if not name:
            continue
        lag_names.add(name)
        kwargs = _lag_kwargs(
            device=device,
            name=name,
            interface_id=interface_id,
            config=config,
            vlan_records=_vlan_records_for(vlans, interface_id=interface_id),
            poe_state=_optional_first_row(poe_states, interface_id, table="poe_states"),
            poe_config=_optional_first_row(poe_configs, interface_id, table="poe_configs"),
            port_config=_optional_first_row(port_configs, interface_id, table="port_configs"),
            port_state=_optional_first_row(port_states, interface_id, table="port_states"),
        )
        entities.append(Entity(interface=Interface(**kwargs)))
        emitted_keys[interface_id] = name
        lag_interface_ids.add(interface_id)
        entities.extend(
            _ip_entities_for_interface(
                device=device,
                interface_name=name,
                rows=interface_ips.get(interface_id, []),
                interface_type=LAG_INTERFACE_TYPE,
            ),
        )

    return LagResult(entities, lag_names, lag_interface_ids, membership, emitted_keys)
