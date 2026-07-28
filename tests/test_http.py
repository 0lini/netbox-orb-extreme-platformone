"""PlatformOneTransport tests: error taxonomy, retry policy, session lifecycle."""

from __future__ import annotations

import pytest
import responses

from orb_extreme_platformone.http import (
    DEFAULT_BASE_URL,
    MAX_CONCURRENT_REQUESTS,
    RETRY_STATUSES,
    RETRY_TOTAL,
    PlatformOneApiError,
    PlatformOneTransport,
)

PROBE_PATH = "/configstate/v1/retrieve-asset-port-state"
PROBE_URL = f"{DEFAULT_BASE_URL}{PROBE_PATH}"


def _transport(**kwargs) -> PlatformOneTransport:
    return PlatformOneTransport(api_token="token", **kwargs)


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_are_classified(status: int) -> None:
    exc = PlatformOneApiError("nope", status_code=status)
    assert exc.is_auth_failure
    assert not exc.is_transient
    assert not exc.is_not_found


def test_not_found_is_classified() -> None:
    exc = PlatformOneApiError("nope", status_code=404)
    assert exc.is_not_found
    assert not exc.is_transient
    assert not exc.is_auth_failure


@pytest.mark.parametrize("status", RETRY_STATUSES)
def test_transient_statuses_are_classified(status: int) -> None:
    exc = PlatformOneApiError("nope", status_code=status)
    assert exc.is_transient
    assert not exc.is_auth_failure


def test_transport_failures_have_no_status_and_count_as_transient() -> None:
    """A connection blip is worth retrying and must not read as an auth failure."""
    exc = PlatformOneApiError("connection reset")
    assert exc.status_code is None
    assert exc.is_transient
    assert not exc.is_auth_failure
    assert not exc.is_not_found


@responses.activate
def test_error_carries_status_and_path() -> None:
    responses.add(responses.POST, PROBE_URL, json={"error": "denied"}, status=403)
    with pytest.raises(PlatformOneApiError) as excinfo:
        _transport().post(PROBE_PATH, {}, {})
    assert excinfo.value.status_code == 403
    assert excinfo.value.path == PROBE_PATH
    assert excinfo.value.is_auth_failure


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


@responses.activate
@pytest.mark.parametrize("status", RETRY_STATUSES)
def test_transient_statuses_are_retried(status: int) -> None:
    responses.add(responses.POST, PROBE_URL, json={"error": "boom"}, status=status)
    with pytest.raises(PlatformOneApiError):
        _transport().post(PROBE_PATH, {}, {})
    assert len(responses.calls) == RETRY_TOTAL + 1


@responses.activate
def test_a_retried_request_succeeds_once_upstream_recovers() -> None:
    responses.add(responses.POST, PROBE_URL, json={"error": "boom"}, status=503)
    responses.add(responses.POST, PROBE_URL, json={"AssetPortState": []}, status=200)
    assert _transport().post(PROBE_PATH, {}, {}) == {"AssetPortState": []}


@responses.activate
@pytest.mark.parametrize("status", [400, 404, 422])
def test_permanent_statuses_are_not_retried(status: int) -> None:
    responses.add(responses.POST, PROBE_URL, json={"error": "nope"}, status=status)
    with pytest.raises(PlatformOneApiError):
        _transport().post(PROBE_PATH, {}, {})
    assert len(responses.calls) == 1


def test_retry_policy_opts_post_in() -> None:
    """Every Platform ONE read is a POST; urllib3 excludes it by default."""
    adapter = _transport()._session().get_adapter("https://x")
    assert "POST" in adapter.max_retries.allowed_methods
    assert adapter.max_retries.total == RETRY_TOTAL
    assert adapter.max_retries.respect_retry_after_header


def test_connection_pools_match_the_fan_out_width() -> None:
    adapter = _transport()._session().get_adapter("https://x")
    assert adapter._pool_maxsize == MAX_CONCURRENT_REQUESTS
    assert adapter._pool_connections == MAX_CONCURRENT_REQUESTS


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def test_sessions_are_reused_within_a_thread() -> None:
    transport = _transport()
    assert transport._session() is transport._session()


def test_close_releases_the_session_and_is_idempotent() -> None:
    transport = _transport()
    first = transport._session()
    transport.close()
    transport.close()
    assert transport._session() is not first
