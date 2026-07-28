"""Standalone dry-run entrypoint tests.

`python -m orb_extreme_platformone` performs a full extract/transform sweep and
prints the resulting entities as JSON. It previously built each line and threw
it away, so the console script produced no output at all.
"""

from __future__ import annotations

import json

import responses

from orb_extreme_platformone.__main__ import _env_bool, _quote_values, main

from .backend_helpers import (
    _mock_assets,
    _mock_cs,
    _mock_empty_clusters,
    _mock_empty_port_and_lag_tables,
)
from .conftest import CS_SWITCH, SWITCH_ASSET


@responses.activate
def test_main_prints_one_json_object_per_entity(capsys, monkeypatch) -> None:
    monkeypatch.setenv("PLATFORMONE_API_TOKEN", "token")
    monkeypatch.delenv("BOOTSTRAP", raising=False)
    _mock_assets([SWITCH_ASSET])
    _mock_cs("asset-device", "AssetDevice", [CS_SWITCH])
    _mock_cs("asset-location", "AssetLocation", [])
    _mock_empty_port_and_lag_tables()
    _mock_empty_clusters()

    main()

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines, "dry run produced no output"
    payloads = [json.loads(line) for line in lines]
    assert any("device" in payload for payload in payloads)
    # Every scalar is rendered as a string so the dry-run JSON quotes uniformly.
    device = next(payload["device"] for payload in payloads if "device" in payload)
    assert device["serial"] == "SN42"
    assert isinstance(device["serial"], str)
    assert all("timestamp" in payload for payload in payloads)


def test_quote_values_stringifies_every_scalar() -> None:
    out = _quote_values({"a": 1, "b": True, "c": [2, False], "d": {"e": None}})
    assert out == {"a": "1", "b": "true", "c": ["2", "false"], "d": {"e": "None"}}


def test_env_bool_reads_common_truthy_spellings(monkeypatch) -> None:
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("SOME_FLAG", value)
        assert _env_bool("SOME_FLAG") is True
    for value in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("SOME_FLAG", value)
        assert _env_bool("SOME_FLAG") is False
    monkeypatch.delenv("SOME_FLAG")
    assert _env_bool("SOME_FLAG", default=True) is True
