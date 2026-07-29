"""PlatformOneClient tests -- HTTP mocked with `responses`.

Response shapes mirror the two Platform ONE OpenAPI specs: `PagedDevice`
(Assets, top-level data/total_pages) and the ConfigState GetResponse
envelope (records under the table's PascalCase schema name + `Pagination`).
"""

from __future__ import annotations

import json

import pytest
import responses

from orb_extreme_platformone.client import (
    CONFIGSTATE_FILTER_CHUNK_SIZE,
    DEFAULT_BASE_URL,
    PlatformOneApiError,
    PlatformOneClient,
    configstate_response_key,
    truncate_error_body,
)

ASSETS_URL = f"{DEFAULT_BASE_URL}/assets/v1/devices"


def _distinct_chunks(calls) -> list[list[str]]:
    """Filter-ID lists actually requested, deduped in order.

    Transient failures are retried at the adapter, so a raw call count no
    longer tells you how many chunks were attempted.
    """
    seen: list[list[str]] = []
    for call in calls:
        ids = json.loads(call.request.body)["asset_device_id"]
        if ids not in seen:
            seen.append(ids)
    return seen


def _client() -> PlatformOneClient:
    return PlatformOneClient(api_token="tok")


def test_client_requires_credentials() -> None:
    with pytest.raises(ValueError, match="api_token or username/password"):
        PlatformOneClient()


def test_client_accepts_username_password_without_token() -> None:
    client = PlatformOneClient(username="user", password="pass")
    # Token state lives on the transport now; password mode starts expired so
    # the first request logs in.
    assert client._transport._token_expiry == 0.0
    assert "Authorization" not in client._transport._headers


def test_client_requires_https_base_url() -> None:
    with pytest.raises(ValueError, match="https://"):
        PlatformOneClient(base_url="http://cloudapi.extremecloudiq.com", api_token="tok")


def test_truncate_error_body_collapses_and_limits_length() -> None:
    assert truncate_error_body("  a \n b  ") == "a b"
    long = "x" * 500
    truncated = truncate_error_body(long, limit=20)
    assert truncated == ("x" * 17) + "..."
    assert len(truncated) == 20


@pytest.mark.parametrize(
    ("table", "key"),
    [
        ("asset-device", "AssetDevice"),
        ("asset-port-state", "AssetPortState"),
        ("asset-interface-vlan-properties", "AssetInterfaceVlanProperties"),
        ("inferred-cluster", "InferredCluster"),
        ("inferred-device", "InferredDevice"),
        ("asset-wireless-interface-state", "AssetWirelessInterfaceState"),
    ],
)
def test_configstate_response_key_matches_the_spec_schema_names(table, key) -> None:
    """PascalCase unwrap keys for tables this worker actually retrieves."""
    assert configstate_response_key(table) == key


@responses.activate
def test_get_devices_paginates_and_sends_the_classification_filter() -> None:
    for page, data in [(1, [{"device_id": 1}]), (2, [{"device_id": 2}])]:
        responses.add(
            responses.POST,
            ASSETS_URL,
            match=[
                responses.matchers.query_param_matcher({"page": str(page), "limit": "500"}),
                responses.matchers.json_params_matcher({"classification": "ALL"}),
            ],
            json={"data": data, "page": page, "total_pages": 2, "total_count": 2},
            status=200,
        )

    devices = list(_client().get_devices())

    assert [d["device_id"] for d in devices] == [1, 2]


@responses.activate
def test_get_devices_passes_a_custom_classification_through_verbatim() -> None:
    responses.add(
        responses.POST,
        ASSETS_URL,
        match=[responses.matchers.json_params_matcher({"classification": "WIRELESS"})],
        json={"data": [], "page": 1, "total_pages": 1, "total_count": 0},
        status=200,
    )

    assert list(_client().get_devices(classification="WIRELESS")) == []


@responses.activate
def test_retrieve_paginates_and_unwraps_the_tables_response_key() -> None:
    url = f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-asset-port-state"
    for page, records in [(1, [{"name": "1/1"}]), (2, [{"name": "1/2"}])]:
        responses.add(
            responses.POST,
            url,
            match=[responses.matchers.query_param_matcher({"page_number": str(page), "page_size": "500"})],
            json={
                "AssetPortState": records,
                "Pagination": {"page": page, "total_pages": 2, "count": 1, "total_count": 2},
            },
            status=200,
        )

    records = list(_client().retrieve("asset-port-state", {"asset_device_id": ["uuid-1"]}))

    assert [r["name"] for r in records] == ["1/1", "1/2"]
    assert json.loads(responses.calls[0].request.body) == {"asset_device_id": ["uuid-1"]}


@responses.activate
def test_retrieve_sends_an_empty_filter_body_by_default() -> None:
    responses.add(
        responses.POST,
        f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-asset-device",
        match=[responses.matchers.json_params_matcher({})],
        json={"AssetDevice": [], "Pagination": {"total_pages": 1}},
        status=200,
    )

    assert list(_client().retrieve("asset-device")) == []


@responses.activate
def test_retrieve_tolerates_a_null_records_key() -> None:
    """ConfigState marks the records array nullable in its spec -- an empty
    table comes back as null, not [].
    """
    responses.add(
        responses.POST,
        f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-asset-port-config",
        json={"AssetPortConfig": None, "Pagination": {"total_pages": 1}},
        status=200,
    )

    assert list(_client().retrieve("asset-port-config")) == []


@responses.activate
def test_retrieve_chunks_large_filter_id_lists() -> None:
    """Oversized asset_device_id lists are split so gateways do not reject the body."""
    url = f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-asset-port-state"
    ids = [f"id-{i}" for i in range(CONFIGSTATE_FILTER_CHUNK_SIZE + 3)]
    chunk_a = ids[:CONFIGSTATE_FILTER_CHUNK_SIZE]
    chunk_b = ids[CONFIGSTATE_FILTER_CHUNK_SIZE:]
    responses.add(
        responses.POST,
        url,
        match=[responses.matchers.json_params_matcher({"asset_device_id": chunk_a})],
        json={"AssetPortState": [{"name": "a"}], "Pagination": {"total_pages": 1}},
        status=200,
    )
    responses.add(
        responses.POST,
        url,
        match=[responses.matchers.json_params_matcher({"asset_device_id": chunk_b})],
        json={"AssetPortState": [{"name": "b"}], "Pagination": {"total_pages": 1}},
        status=200,
    )

    rows = list(_client().retrieve("asset-port-state", {"asset_device_id": ids}))

    assert [r["name"] for r in rows] == ["a", "b"]
    assert len(responses.calls) == 2


@responses.activate
def test_retrieve_keeps_prior_chunk_rows_when_later_chunk_fails() -> None:
    """A failed filter chunk must not discard rows already fetched from earlier chunks."""
    url = f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-asset-port-state"
    ids = [f"id-{i}" for i in range(CONFIGSTATE_FILTER_CHUNK_SIZE + 3)]
    chunk_a = ids[:CONFIGSTATE_FILTER_CHUNK_SIZE]
    chunk_b = ids[CONFIGSTATE_FILTER_CHUNK_SIZE:]
    responses.add(
        responses.POST,
        url,
        match=[responses.matchers.json_params_matcher({"asset_device_id": chunk_a})],
        json={"AssetPortState": [{"name": "a"}], "Pagination": {"total_pages": 1}},
        status=200,
    )
    responses.add(
        responses.POST,
        url,
        match=[responses.matchers.json_params_matcher({"asset_device_id": chunk_b})],
        json={"error": "nope"},
        status=500,
    )

    rows = list(_client().retrieve("asset-port-state", {"asset_device_id": ids}))

    assert [r["name"] for r in rows] == ["a"]
    # Count distinct chunks, not raw calls: the failing chunk is retried.
    assert _distinct_chunks(responses.calls) == [chunk_a, chunk_b]


@responses.activate
def test_retrieve_raises_when_every_filter_chunk_fails() -> None:
    url = f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-asset-port-state"
    ids = [f"id-{i}" for i in range(CONFIGSTATE_FILTER_CHUNK_SIZE + 1)]
    responses.add(responses.POST, url, json={"error": "nope"}, status=500)

    with pytest.raises(PlatformOneApiError, match="all 2 filter chunks failed"):
        list(_client().retrieve("asset-port-state", {"asset_device_id": ids}))
    assert len(_distinct_chunks(responses.calls)) == 2


@responses.activate
def test_non_2xx_raises_platform_one_api_error() -> None:
    responses.add(responses.POST, ASSETS_URL, json={"error": "nope"}, status=403)

    with pytest.raises(PlatformOneApiError, match="403") as excinfo:
        list(_client().get_devices())
    assert "nope" in str(excinfo.value)


@responses.activate
def test_non_2xx_truncates_long_error_bodies() -> None:
    responses.add(responses.POST, ASSETS_URL, body="e" * 1000, status=500)

    with pytest.raises(PlatformOneApiError) as excinfo:
        list(_client().get_devices())
    message = str(excinfo.value)
    assert "500" in message
    assert "e" * 1000 not in message
    assert message.endswith("...")


LOGIN_URL = f"{DEFAULT_BASE_URL}/login"


@responses.activate
def test_username_password_logs_in_before_api_calls() -> None:
    responses.add(
        responses.POST,
        LOGIN_URL,
        match=[responses.matchers.json_params_matcher({"username": "user", "password": "pass"})],
        json={"access_token": "session-tok", "expires_in": 3600},
        status=200,
    )
    responses.add(
        responses.POST,
        ASSETS_URL,
        match=[responses.matchers.header_matcher({"Authorization": "Bearer session-tok"})],
        json={"data": [{"device_id": 1}], "page": 1, "total_pages": 1, "total_count": 1},
        status=200,
    )

    client = PlatformOneClient(username="user", password="pass")
    assert [d["device_id"] for d in client.get_devices()] == [1]
    assert len(responses.calls) == 2
    assert responses.calls[0].request.url == LOGIN_URL


@responses.activate
def test_username_password_relogs_in_once_on_401() -> None:
    responses.add(
        responses.POST,
        LOGIN_URL,
        json={"access_token": "first-tok", "expires_in": 3600},
        status=200,
    )
    responses.add(responses.POST, ASSETS_URL, json={"error": "expired"}, status=401)
    responses.add(
        responses.POST,
        LOGIN_URL,
        json={"access_token": "second-tok", "expires_in": 3600},
        status=200,
    )
    responses.add(
        responses.POST,
        ASSETS_URL,
        match=[responses.matchers.header_matcher({"Authorization": "Bearer second-tok"})],
        json={"data": [{"device_id": 9}], "page": 1, "total_pages": 1, "total_count": 1},
        status=200,
    )

    client = PlatformOneClient(username="user", password="pass")
    assert [d["device_id"] for d in client.get_devices()] == [9]
    assert len(responses.calls) == 4


@responses.activate
def test_login_failure_raises_platform_one_api_error() -> None:
    responses.add(responses.POST, LOGIN_URL, json={"error": "bad creds"}, status=401)

    client = PlatformOneClient(username="user", password="pass")
    # One message shape for every failed call: "<upstream> API error <code> for <path>".
    with pytest.raises(PlatformOneApiError, match=r"API error 401 for /login") as excinfo:
        list(client.get_devices())
    assert "bad creds" in str(excinfo.value)
    assert excinfo.value.is_auth_failure


@responses.activate
def test_static_api_token_does_not_call_login() -> None:
    responses.add(
        responses.POST,
        ASSETS_URL,
        match=[responses.matchers.header_matcher({"Authorization": "Bearer tok"})],
        json={"data": [], "page": 1, "total_pages": 1, "total_count": 0},
        status=200,
    )

    assert list(_client().get_devices()) == []
    assert all("/login" not in call.request.url for call in responses.calls)


@responses.activate
def test_transport_failure_raises_platform_one_api_error() -> None:
    responses.add(
        responses.POST,
        ASSETS_URL,
        body=responses.ConnectionError("boom"),
    )

    with pytest.raises(PlatformOneApiError, match="request failed"):
        list(_client().get_devices())


@responses.activate
def test_invalid_json_raises_platform_one_api_error() -> None:
    responses.add(responses.POST, ASSETS_URL, body="not-json", status=200)

    with pytest.raises(PlatformOneApiError, match="invalid JSON"):
        list(_client().get_devices())


# ---------------------------------------------------------------------------
# Error-body redaction and malformed-response tolerance
# ---------------------------------------------------------------------------


def test_truncate_error_body_redacts_echoed_credentials() -> None:
    """A gateway that echoes the request body must not leak the login password."""
    body = '{"error": "bad creds for {"username": "admin", "password": "hunter2"}"}'
    out = truncate_error_body(body, limit=500)
    assert "hunter2" not in out
    assert "[REDACTED]" in out


@pytest.mark.parametrize(
    "field",
    ["password", "client_secret", "access_token", "refresh_token", "api_token", "authorization"],
)
def test_truncate_error_body_redacts_every_secret_field(field: str) -> None:
    assert "s3cret" not in truncate_error_body(f'{{"{field}": "s3cret"}}', limit=500)


@responses.activate
def test_login_failure_does_not_leak_password_into_the_exception() -> None:
    responses.add(
        responses.POST,
        f"{DEFAULT_BASE_URL}/login",
        body='{"error": "rejected {"username": "admin", "password": "hunter2"}"}',
        status=401,
    )
    client = PlatformOneClient(username="admin", password="hunter2")
    with pytest.raises(PlatformOneApiError) as excinfo:
        list(client.get_devices())
    assert "hunter2" not in str(excinfo.value)


@responses.activate
def test_retrieve_rejects_non_list_records_as_api_error() -> None:
    """A malformed 200 must degrade this table, not raise TypeError out of the thread."""
    responses.add(
        responses.POST,
        f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-asset-port-state",
        json={"AssetPortState": 5, "Pagination": {"total_pages": 1}},
        status=200,
    )
    client = PlatformOneClient(api_token="t")
    with pytest.raises(PlatformOneApiError, match="non-list"):
        list(client.retrieve("asset-port-state", {"asset_device_id": ["x"]}))


@responses.activate
def test_retrieve_tolerates_a_string_total_pages() -> None:
    """`total_pages: "2"` used to raise TypeError comparing int >= str."""
    responses.add(
        responses.POST,
        f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-asset-port-state",
        json={"AssetPortState": [{"a": 1}], "Pagination": {"total_pages": "2"}},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-asset-port-state",
        json={"AssetPortState": [{"a": 2}], "Pagination": {"total_pages": "2"}},
        status=200,
    )
    client = PlatformOneClient(api_token="t")
    assert list(client.retrieve("asset-port-state", {"asset_device_id": ["x"]})) == [{"a": 1}, {"a": 2}]


@responses.activate
def test_retrieve_tolerates_a_garbage_total_pages() -> None:
    """An unparseable page count stops after the current page instead of raising."""
    responses.add(
        responses.POST,
        f"{DEFAULT_BASE_URL}/configstate/v1/retrieve-asset-port-state",
        json={"AssetPortState": [{"a": 1}], "Pagination": {"total_pages": None}},
        status=200,
    )
    client = PlatformOneClient(api_token="t")
    assert list(client.retrieve("asset-port-state", {"asset_device_id": ["x"]})) == [{"a": 1}]
