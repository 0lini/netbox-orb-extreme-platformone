"""Degradation, fail-fast and observability behaviour across a tick."""

from __future__ import annotations

import logging

import pytest
import responses

from orb_extreme_platformone.backend import Backend
from orb_extreme_platformone.client import PlatformOneApiError
from orb_extreme_platformone.extract.retrieve import retrieve_parallel
from orb_extreme_platformone.logging_context import (
    PolicyContextFilter,
    current_policy_name,
    get_logger,
)

from .backend_helpers import (
    _mock_assets,
    _mock_cs,
    _mock_empty_clusters,
    _mock_empty_port_and_lag_tables,
    _policy,
)
from .conftest import CS_SWITCH, SWITCH_ASSET


class _Source:
    """Minimal ConfigState source: the extract layer only ever calls retrieve."""

    def __init__(self, **behaviour) -> None:
        self._behaviour = behaviour

    def retrieve(self, table: str, filters: dict | None = None):  # noqa: ARG002
        outcome = self._behaviour.get(table, [])
        if isinstance(outcome, Exception):
            raise outcome
        yield from outcome


# ---------------------------------------------------------------------------
# One bad table must not abort the fan-out
# ---------------------------------------------------------------------------


def test_unexpected_exception_degrades_one_table_not_the_fan_out() -> None:
    """A non-API exception used to escape future.result() and kill the tick."""
    source = _Source(
        good1=[{"row": 1}],
        bad=KeyError("unexpected upstream shape"),
        good2=[{"row": 2}],
    )
    results = retrieve_parallel(source, [("good1", {}), ("bad", {}), ("good2", {})])

    assert [r.table for r in results] == ["good1", "bad", "good2"]
    assert results[0].rows == [{"row": 1}]
    assert results[2].rows == [{"row": 2}], "healthy tables must survive a sibling's failure"
    assert isinstance(results[1].error, PlatformOneApiError)
    assert results[1].error.is_transient


def test_api_errors_still_degrade_normally() -> None:
    source = _Source(bad=PlatformOneApiError("boom", status_code=503))
    (result,) = retrieve_parallel(source, [("bad", {})])
    assert result.rows is None
    assert result.error.is_transient


def test_results_are_in_submission_order() -> None:
    source = _Source(**{f"t{i}": [{"i": i}] for i in range(6)})
    jobs = [(f"t{i}", {}) for i in range(6)]
    assert [r.table for r in retrieve_parallel(source, jobs)] == [f"t{i}" for i in range(6)]


# ---------------------------------------------------------------------------
# Auth failures fail fast instead of degrading every table
# ---------------------------------------------------------------------------


@responses.activate
@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_aborts_the_tick(status: int, caplog) -> None:
    """Bad credentials fail every remaining table too; degrading 15 times is noise."""
    _mock_assets([SWITCH_ASSET])
    _mock_cs("asset-device", "AssetDevice", [CS_SWITCH])
    _mock_cs("asset-location", "AssetLocation", [])
    _mock_cs("asset-port-config", "AssetPortConfig", [], status=status)
    _mock_empty_port_and_lag_tables()
    _mock_empty_clusters()

    with caplog.at_level(logging.ERROR), pytest.raises(PlatformOneApiError) as excinfo:
        list(Backend().run("platformone_worker", _policy()))

    assert excinfo.value.is_auth_failure
    assert any("rejected our credentials" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Total failure is logged at ERROR, not silently propagated
# ---------------------------------------------------------------------------


@responses.activate
def test_tick_failure_is_logged_at_error_with_a_traceback(caplog) -> None:
    # The Assets listing is not wrapped in a degradation handler: if it fails
    # there is nothing to sync, so the tick genuinely ends.
    responses.add(
        responses.POST,
        "https://cloudapi.extremecloudiq.com/assets/v1/devices",
        json={"error": "denied"},
        status=403,
    )

    with caplog.at_level(logging.ERROR), pytest.raises(PlatformOneApiError):
        list(Backend().run("platformone_worker", _policy()))

    failures = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert failures, "a tick that produced no entities must not be silent at ERROR"
    assert any(r.exc_info for r in failures), "the ERROR record should carry a traceback"


# ---------------------------------------------------------------------------
# Policy context reaches records that never mention the policy
# ---------------------------------------------------------------------------


def test_policy_context_filter_stamps_every_record() -> None:
    record = logging.LogRecord("x", logging.WARNING, "f", 1, "msg", None, None)
    assert PolicyContextFilter().filter(record)
    assert record.policy == current_policy_name()


@responses.activate
def test_policy_name_is_set_for_the_duration_of_a_tick(caplog) -> None:
    seen: list[str] = []

    class _Probe(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            seen.append(getattr(record, "policy", "<unset>"))
            return True

    _mock_assets([SWITCH_ASSET])
    _mock_cs("asset-device", "AssetDevice", [CS_SWITCH])
    _mock_cs("asset-location", "AssetLocation", [])
    _mock_empty_port_and_lag_tables()
    _mock_empty_clusters()

    # Filters do not propagate to child loggers, so attach to the ones that log.
    probed = [get_logger(f"orb_extreme_platformone.{name}") for name in ("backend", "extract.correlate")]
    probe = _Probe()
    for target in probed:
        target.addFilter(probe)
    try:
        # Filters only run for records that pass the level check.
        with caplog.at_level(logging.INFO, logger="orb_extreme_platformone"):
            list(Backend().run("my_policy", _policy()))
    finally:
        for target in probed:
            target.removeFilter(probe)

    assert seen, "the tick should log something"
    assert all(name == "my_policy" for name in seen), seen
