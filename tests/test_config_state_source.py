"""Tests for ConfigStateSource Protocol wiring and extract fakes."""

from __future__ import annotations

from orb_extreme_platformone.extract import ConfigStateSource
from orb_extreme_platformone.extract.correlate import extract_cs_devices
from orb_extreme_platformone.extract.retrieve import retrieve_parallel


class _FakeSource:
    """Five-line ConfigStateSource stand-in (no HTTP)."""

    def __init__(self, rows: dict[str, list[dict]]) -> None:
        self._rows = rows

    def retrieve(self, table: str, filters: dict | None = None):
        del filters  # Protocol-compatible; fakes ignore filters.
        yield from self._rows.get(table, [])


def test_fake_source_satisfies_protocol() -> None:
    assert isinstance(_FakeSource({}), ConfigStateSource)


def test_extract_accepts_narrow_fake_source() -> None:
    fake = _FakeSource(
        {
            "asset-device": [
                {"id": "cs-1", "serial_number": "SN1"},
            ],
        },
    )
    rows = extract_cs_devices(fake, [{"serial_number": "SN1"}])
    assert rows == [{"id": "cs-1", "serial_number": "SN1"}]


def test_retrieve_parallel_with_fake_source() -> None:
    fake = _FakeSource({"asset-port-state": [{"asset_interface_id": "if-1"}]})
    results = retrieve_parallel(fake, [("asset-port-state", {"asset_device_id": ["d1"]})])
    assert len(results) == 1
    assert results[0].error is None
    assert results[0].rows == [{"asset_interface_id": "if-1"}]
