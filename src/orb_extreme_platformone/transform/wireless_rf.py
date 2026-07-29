"""RF helpers for AP radio Interface mapping."""

from __future__ import annotations

from .common import _coerce_int, _compact_token

# Only modes the substring fallback in `_radio_type` gets wrong need a table
# entry: "11bg" and "11an" contain "11b"/"11a" but mean 11g/11n, so a plain
# substring scan would pick the wrong standard. Everything else — "_11ax_5g",
# "ieee802.11ac", "11ac" — the fallback already resolves correctly.
_RADIO_TYPE_BY_MODE = {
    "_11bg": "ieee802.11g",
    "_11an": "ieee802.11n",
    "11bg": "ieee802.11g",
    "11an": "ieee802.11n",
}

# channel_width is an integer in ConfigState; only values that are already
# standard IEEE channel widths in MHz are asserted.
_VERIFIED_CHANNEL_WIDTH_MHZ = frozenset({20, 40, 80, 160, 320})


def _channel_frequency_mhz(band: str | None, channel: int | None) -> float | None:
    """Channel-center frequency in MHz from band label + channel number.

    Uses standard IEEE 802.11 channel-numbering formulas (not Extreme-specific):
    2.4 GHz = 2407 + 5*channel; 5 GHz = 5000 + 5*channel; 6 GHz = 5950 + 5*channel.
    """
    channel_number = _coerce_int(channel)
    if channel_number is None or not band:
        return None
    # Collapse separators so BAND_5_GHZ, "5 GHz" and "5g" all normalize alike:
    # BAND_2_4_GHZ -> "band24ghz", matched on the "24g" substring.
    normalized = _compact_token(band)
    if "6g" in normalized:
        offset = 5950.0
    elif "24g" in normalized or "2.4" in normalized:
        offset = 2407.0
    elif "5g" in normalized:
        offset = 5000.0
    else:
        return None
    return offset + 5.0 * channel_number


def _radio_type(radio_mode: str | None) -> str | None:
    if not radio_mode:
        return None
    key = str(radio_mode).strip().casefold()
    mapped = _RADIO_TYPE_BY_MODE.get(key)
    if mapped:
        return mapped
    compact = _compact_token(key, drop=" -.")
    # NetBox 4.6 accepts ieee802.11be; rf_role / RF fields require a wireless type
    # (type=other is rejected with "Wireless role may be set only on wireless
    # interfaces").
    if "11be" in compact:
        return "ieee802.11be"
    for needle, iface_type in (
        ("11ax", "ieee802.11ax"),
        ("11ac", "ieee802.11ac"),
        ("11n", "ieee802.11n"),
        ("11g", "ieee802.11g"),
        ("11b", "ieee802.11b"),
        ("11a", "ieee802.11a"),
    ):
        if needle in compact:
            return iface_type
    return None


def _is_wireless_interface_type(iface_type: str | None) -> bool:
    return bool(iface_type) and str(iface_type).startswith("ieee802.11")


def _channel_width_mhz(value) -> float | None:
    width = _coerce_int(value)
    if width is not None and width in _VERIFIED_CHANNEL_WIDTH_MHZ:
        return float(width)
    return None
