"""Map ConfigState port capability speed-duplex arrays to Interface CFs.

NetBox ``Interface.speed`` is a single operational kbps value. Platform ONE
``AssetPortCapabilities`` instead exposes *arrays* of supported speed-duplex
tokens (forced vs advertised). Those land here as a JSON custom field with
human labels like ``1G-full`` — never stuffed into ``Interface.speed``.
"""

from __future__ import annotations

import re

from .common import CF_SUPPORTED_SPEEDS, _cf_json
from .port_constants import _SPEED_MBPS_LABEL

# Compact token (casefold, separators stripped) → label. Prefer this over the
# regex path when Platform ONE returns opaque enum-style strings we have
# verified. Grow this table from dry-run samples the same way as
# VERIFIED_OPER_SPEED_KBPS.
VERIFIED_SPEED_DUPLEX_TOKENS: dict[str, str] = {
    # Common Extreme / OpenConfig-style enum names seen in port capability APIs.
    "speed10half": "10M-half",
    "speed10full": "10M-full",
    "speed100half": "100M-half",
    "speed100full": "100M-full",
    "speed1000half": "1G-half",
    "speed1000full": "1G-full",
    "speed2500full": "2.5G-full",
    "speed5000full": "5G-full",
    "speed10000full": "10G-full",
    "speed25000full": "25G-full",
    "speed40000full": "40G-full",
    "speed50000full": "50G-full",
    "speed100000full": "100G-full",
    "speed_10_half": "10M-half",
    "speed_10_full": "10M-full",
    "speed_100_half": "100M-half",
    "speed_100_full": "100M-full",
    "speed_1000_half": "1G-half",
    "speed_1000_full": "1G-full",
    "speed_2500_full": "2.5G-full",
    "speed_5000_full": "5G-full",
    "speed_10000_full": "10G-full",
    "speed_25000_full": "25G-full",
    "speed_40000_full": "40G-full",
    "speed_50000_full": "50G-full",
    "speed_100000_full": "100G-full",
    "10half": "10M-half",
    "10full": "10M-full",
    "100half": "100M-half",
    "100full": "100M-full",
    "1000half": "1G-half",
    "1000full": "1G-full",
    "10mhalf": "10M-half",
    "10mfull": "10M-full",
    "100mhalf": "100M-half",
    "100mfull": "100M-full",
    "1ghalf": "1G-half",
    "1gfull": "1G-full",
    "2.5gfull": "2.5G-full",
    "5gfull": "5G-full",
    "10gfull": "10G-full",
    "25gfull": "25G-full",
    "40gfull": "40G-full",
    "50gfull": "50G-full",
    "100gfull": "100G-full",
}

_COMPACT_DROP = " _-/"

# SPEED_1000_FULL, 1000FULL, 1G-full, 10000/FULL, 2.5G_FULL, …
_TOKEN_RE = re.compile(
    r"(?:speed[_-]?)?"
    r"(?P<rate>\d+(?:\.\d+)?)\s*(?P<unit>g|gb|gbps|m|mb|mbps)?"
    r"[_/\s-]?"
    r"(?P<duplex>half|full|h|f)?$",
    re.IGNORECASE,
)


def _compact(token: str) -> str:
    text = str(token).casefold()
    for ch in _COMPACT_DROP:
        text = text.replace(ch, "")
    return text


def _mbps_from_rate(rate: float, unit: str | None) -> int | None:
    """Interpret a parsed rate+unit as Mbps; bare numbers are Mbps."""
    unit_key = (unit or "").casefold()
    if unit_key in {"g", "gb", "gbps"}:
        mbps = rate * 1000
    elif unit_key in {"m", "mb", "mbps", ""}:
        mbps = rate
    else:
        return None
    rounded = round(mbps)
    # Allow fractional G labels that land on known Mbps (2.5G → 2500).
    if abs(mbps - rounded) > 0.01 and rounded not in _SPEED_MBPS_LABEL:
        return None
    return rounded


def map_speed_duplex_token(token) -> str | None:
    """Map one capability token to a label like ``1G-full``, or None if unknown."""
    if token is None or isinstance(token, bool):
        return None
    text = str(token).strip()
    if not text:
        return None

    compact = _compact(text)
    if compact in VERIFIED_SPEED_DUPLEX_TOKENS:
        return VERIFIED_SPEED_DUPLEX_TOKENS[compact]

    # Normalize separators so the regex can see unit/duplex boundaries.
    normalized = re.sub(r"[\s_/]+", "-", text.strip())
    match = _TOKEN_RE.fullmatch(normalized.replace("--", "-"))
    if not match:
        # Retry with underscores as separators (SPEED_1000_FULL).
        match = _TOKEN_RE.fullmatch(text.strip().replace("_", "-"))
    if not match:
        return None

    rate = float(match.group("rate"))
    mbps = _mbps_from_rate(rate, match.group("unit"))
    if mbps is None:
        return None
    speed_label = _SPEED_MBPS_LABEL.get(mbps)
    if speed_label is None:
        return None

    duplex_raw = (match.group("duplex") or "").casefold()
    if duplex_raw in {"half", "h"}:
        duplex = "half"
    elif duplex_raw in {"full", "f"}:
        duplex = "full"
    elif duplex_raw == "":
        # Speed-only tokens (e.g. channelization "10G") — keep as speed label.
        return speed_label
    else:
        return None
    return f"{speed_label}-{duplex}"


def _map_token_list(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    labels: list[str] = []
    seen: set[str] = set()
    for token in raw:
        label = map_speed_duplex_token(token)
        if label is None or label in seen:
            continue
        seen.add(label)
        labels.append(label)
    return labels


def supported_speeds_payload(capability: dict | None) -> dict | None:
    """Build JSON payload from a port-capabilities row, or None if empty."""
    if not capability:
        return None
    forced = _map_token_list(capability.get("auto_neg_off_supported_speed_duplex_list"))
    advertised = _map_token_list(capability.get("auto_neg_on_supported_adv_list"))
    if not forced and not advertised:
        return None
    payload: dict = {}
    if forced:
        payload["forced"] = forced
    if advertised:
        payload["advertised"] = advertised
    return payload


def supported_speeds_custom_fields(capability: dict | None) -> dict:
    """Interface CF dict for mapped supported speeds, or empty."""
    payload = supported_speeds_payload(capability)
    if payload is None:
        return {}
    return {CF_SUPPORTED_SPEEDS: _cf_json(payload)}
