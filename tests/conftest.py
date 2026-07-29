"""Shared pytest fixtures for orb_extreme_platformone tests."""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest
from netboxlabs.diode.sdk import ingester as _ingester

from orb_extreme_platformone import transform

# ---------------------------------------------------------------------------
# Shared Platform ONE payload shapes (Assets Device + ConfigState port rows)
# ---------------------------------------------------------------------------

SWITCH_ASSET = {
    "device_id": 42,
    "host_name": "sw-idf1",
    "serial_number": "SN42",
    "mac_address": "aabbccddeeff",
    "product_type": "FabricEngine_5320_48P_8XE",
    "function": "Fabric Engine",
    "os_version": "9.2.1.0",
    "is_connected": True,
    "ip_address": "10.0.0.2",
    "site_name": "Assets-Site",
}

CS_SWITCH = {
    "id": "cs-uuid-42",
    "serial_number": "SN42",
    "base_mac_address": "AA:BB:CC:DD:EE:FF",
}

PORT_CONFIG = {
    "asset_device_id": "cs-uuid-42",
    "asset_interface_id": "if-uuid-1",
    "name": "1/1",
    "enabled": True,
    "description": "uplink to core",
}

PORT_STATE = {
    "asset_device_id": "cs-uuid-42",
    "asset_interface_id": "if-uuid-1",
    "name": "1/1",
    "oper_state": 1,
    "oper_speed": 4,
    "oper_duplex": 2,
    "connector_type": 1,
    "mac_address": "aa:bb:cc:dd:ee:01",
    "if_index": 1,
}

VLAN_PROPERTIES = {
    "device_id": "cs-uuid-42",
    "asset_interface_id": "if-uuid-1",
    "interface_name": "1/1",
    "port_vlan": 10,
    "vlans": [{"vlan_number": 10}, {"vlan_number": 20}, {"vlan_number": 30}],
}


class Rec:
    """Records constructor kwargs so tests can assert on them without the real protobuf SDK.

    Rejects any kwarg the real Diode class would reject: the real classes are
    protobuf-backed and raise TypeError on unknown fields, so a permissive stub
    would green-light a transform that fails in production.
    """

    def __init__(self, **kw) -> None:
        real = getattr(_ingester, type(self).__name__, None)
        if real is not None:
            unknown = sorted(set(kw) - set(inspect.signature(real).parameters))
            if unknown:
                msg = f"{type(self).__name__} stub got kwargs the real Diode class rejects: {unknown}"
                raise TypeError(msg)
        self.__dict__.update(kw)
        self._kw = kw

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._kw})"


class Device(Rec):
    pass


class DeviceType(Rec):
    pass


class Platform(Rec):
    pass


class Interface(Rec):
    pass


class Site(Rec):
    pass


class Location(Rec):
    pass


class VLAN(Rec):
    pass


class VirtualChassis(Rec):
    pass


class WirelessLAN(Rec):
    pass


class DeviceRole(Rec):
    pass


class IPAddress(Rec):
    pass


class Entity(Rec):
    pass


class CustomFieldValue(Rec):
    pass


# One stub per Diode SDK class transform imports.
STUB_CLASSES = {
    "Device": Device,
    "DeviceType": DeviceType,
    "DeviceRole": DeviceRole,
    "Platform": Platform,
    "Interface": Interface,
    "IPAddress": IPAddress,
    "Site": Site,
    "Location": Location,
    "VLAN": VLAN,
    "VirtualChassis": VirtualChassis,
    "WirelessLAN": WirelessLAN,
    "Entity": Entity,
    "CustomFieldValue": CustomFieldValue,
}


def _transform_modules() -> list:
    """Every module in the transform package, discovered rather than listed.

    Walking the package means a new transform module cannot be silently left
    unstubbed the way a hand-maintained tuple allows.
    """
    modules = [transform]
    for info in pkgutil.iter_modules(transform.__path__):
        modules.append(importlib.import_module(f"{transform.__name__}.{info.name}"))
    return modules


@pytest.fixture
def stub_sdk(monkeypatch):
    """Swap the real Diode SDK classes transform submodules imported for stubs.

    The real classes build protobuf messages, which are awkward to assert on
    directly; these stand-ins record constructor kwargs on `._kw` instead, so
    tests can assert on the *shape* of what transform builds.
    """
    for name, cls in STUB_CLASSES.items():
        for mod in _transform_modules():
            # Only patch names the module actually binds (imports or defines).
            if name in mod.__dict__:
                monkeypatch.setattr(mod, name, cls)
    return STUB_CLASSES


def cf(custom_field_value_kw: dict):
    """Unwrap a stubbed CustomFieldValue's kwargs back to its plain scalar."""
    return custom_field_value_kw["text"]
