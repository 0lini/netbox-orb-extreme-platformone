"""ISIS / SPBM ConfigState rows → Device text custom fields.

Focused on fabric identity parameters operators care about in NetBox:
ISIS area, ISIS system id, and SPBM nickname.
"""

from __future__ import annotations

from .common import CF_ISIS_AREA, CF_ISIS_SYSTEM_ID, CF_SPBM_NICKNAME, _cf_text


def _first_non_empty_field(rows: list[dict], *field_names: str) -> str | None:
    """First non-empty string value for any of ``field_names``, scanning rows in order."""
    for row in rows:
        for field_name in field_names:
            value = row.get(field_name)
            if value is None or value == "":
                continue
            text = str(value).strip()
            if text:
                return text
    return None


def device_fabric_custom_fields(tables: dict[str, list[dict]]) -> dict:
    """Build Device CFs for ISIS area / system id and SPBM nickname.

    Sources (ConfigState):
    - ``isis_global_configs`` — ``manual_area_address`` / ``area_name``,
      ``sys_id``, ``area_vnode_nickname``
    - ``isis_global_states`` — ``default_area_address`` /
      ``dynamically_learned_area`` (area fallback)
    - ``spbm_instances`` — ``node_nick_name`` (preferred nickname)

    Returns an empty dict when none of the three values are present.
    """
    configs = tables.get("isis_global_configs") or []
    states = tables.get("isis_global_states") or []
    instances = tables.get("spbm_instances") or []

    area = _first_non_empty_field(configs, "manual_area_address", "area_name") or _first_non_empty_field(
        states,
        "default_area_address",
        "dynamically_learned_area",
    )
    system_id = _first_non_empty_field(configs, "sys_id")
    nickname = _first_non_empty_field(instances, "node_nick_name") or _first_non_empty_field(
        configs,
        "area_vnode_nickname",
    )

    custom_fields: dict = {}
    if area is not None:
        custom_fields[CF_ISIS_AREA] = _cf_text(area)
    if system_id is not None:
        custom_fields[CF_ISIS_SYSTEM_ID] = _cf_text(system_id)
    if nickname is not None:
        custom_fields[CF_SPBM_NICKNAME] = _cf_text(nickname)
    return custom_fields
