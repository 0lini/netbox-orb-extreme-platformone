"""Backend configuration, bootstrap, and extraction helper tests."""

from __future__ import annotations

import pytest
from worker.models import Config, Policy

from orb_extreme_platformone.backend import Backend
from orb_extreme_platformone.extract.ports import collect_interface_ids


def test_describe_reports_stable_identity() -> None:
    metadata = Backend.describe()
    assert metadata.app_name == "netbox-orb-extreme-platformone"
    assert metadata.name == "orb_extreme_platformone"


def test_collect_interface_ids_includes_vlan_only_interfaces() -> None:
    """VLAN-facing interfaces absent from port/LAG rows must still feed IP/PoE fetches."""
    tables_by_device = {
        "cs-uuid-42": {
            "port_configs": [
                {"asset_interface_id": "if-port", "name": "1/1"},
            ],
            "vlan_properties": [
                {"asset_interface_id": "if-port", "interface_name": "1/1"},
                {"asset_interface_id": "if-svi", "interface_name": "vlan10"},
            ],
            "lag_configs": [],
            "lag_states": [],
            "poe_states": [],
            "port_states": [],
            "port_capabilities": [],
        },
    }

    mapping = collect_interface_ids(tables_by_device)

    assert mapping == {
        "if-port": "cs-uuid-42",
        "if-svi": "cs-uuid-42",
    }


def test_bootstrap_true_without_netbox_creds_fails_closed(monkeypatch) -> None:
    """BOOTSTRAP must not silently no-op when NetBox credentials are missing."""
    # Credentials fall back to the environment, so clear any ambient values.
    monkeypatch.delenv("NETBOX_API_URL", raising=False)
    monkeypatch.delenv("NETBOX_API_TOKEN", raising=False)
    policy = Policy(
        config=Config(
            package="orb_extreme_platformone",
            BOOTSTRAP=True,
            PLATFORMONE_API_TOKEN="tok",
        ),
        scope={"sites": ["*"]},
    )
    with pytest.raises(ValueError, match="BOOTSTRAP is enabled"):
        list(Backend().run("platformone_worker", policy))


def test_policy_or_env_prefers_explicit_empty_policy_value(monkeypatch) -> None:
    """Empty policy string wins over environment (None alone falls through)."""
    from orb_extreme_platformone import backend as backend_mod

    monkeypatch.setenv("PLATFORMONE_API_TOKEN", "from-env")

    class _Cfg:
        PLATFORMONE_API_TOKEN = ""

    assert backend_mod._policy_or_env(_Cfg(), "PLATFORMONE_API_TOKEN") == ""

    class _Missing:
        pass

    assert backend_mod._policy_or_env(_Missing(), "PLATFORMONE_API_TOKEN") == "from-env"
