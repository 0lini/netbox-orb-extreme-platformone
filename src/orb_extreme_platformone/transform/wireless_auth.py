"""WirelessLAN authentication and merge helpers."""

from __future__ import annotations

from .common import PROVENANCE_TAGS, _compact_token, logger


def _auth_from_encryption(encryption: str | None) -> tuple[str, str]:
    """Map AssetSsidState.encryption to NetBox WirelessLAN auth_type + auth_cipher.

    Unknown / empty values default to ``open`` / ``auto`` (Cisco Meraki posture).
    Bare ``WPA`` / ``TYPE_WPA`` count as personal (not open).
    """
    if not encryption or not str(encryption).strip():
        return "open", "auto"
    compact = _compact_token(encryption)
    if compact in {"open", "enhancedopen", "none", "owe"} or compact.startswith("open"):
        return "open", "auto"
    if "wep" in compact:
        return "wep", "wep"
    if any(token in compact for token in ("8021x", "enterprise", "radius", "eap", "dot1x")):
        auth_type = "wpa-enterprise"
    elif any(
        token in compact for token in ("psk", "ppsk", "sae", "personal", "wpa2", "wpa3", "wpa")
    ) or compact in {"typewpa", "wpaeap"}:
        auth_type = "wpa-personal"
    else:
        auth_type = "open"

    if "tkip" in compact or compact in {"wpa", "wpaeap", "typewpa"}:
        auth_cipher = "tkip"
    elif any(token in compact for token in ("wpa2", "wpa3", "aes", "ccmp", "gcmp", "sae")):
        auth_cipher = "aes"
    else:
        auth_cipher = "auto"
    return auth_type, auth_cipher


def _wlan_status(enabled) -> str:
    """Map SSID enabled → WirelessLAN status; unknown defaults to active."""
    if enabled is False:
        return "disabled"
    return "active"


def _wlan_kwargs(ssid: str, *, enabled, encryption: str | None) -> dict:
    auth_type, auth_cipher = _auth_from_encryption(encryption)
    return {
        "ssid": ssid,
        "status": _wlan_status(enabled),
        "auth_type": auth_type,
        "auth_cipher": auth_cipher,
        "tags": PROVENANCE_TAGS,
    }


def _ensure_wlan(
    wlans: dict[str, dict],
    ssid: str,
    *,
    enabled=None,
    encryption=None,
) -> dict:
    """Merge per-AP SSID rows into one global WirelessLAN.

    ``enabled`` is OR'd across APs (SSID active if any AP broadcasts it).
    Conflicting ``encryption`` values keep the first and warn.
    """
    entry = wlans.setdefault(ssid, {"enabled": None, "encryption": None})
    if isinstance(enabled, bool):
        if entry["enabled"] is None:
            entry["enabled"] = enabled
        else:
            entry["enabled"] = entry["enabled"] or enabled
    if encryption is not None:
        previous = entry.get("encryption")
        if previous is None:
            entry["encryption"] = encryption
        elif previous != encryption:
            logger.warning(
                "Conflicting encryption for SSID %r (%r vs %r); keeping the first",
                ssid,
                previous,
                encryption,
            )
    return entry
