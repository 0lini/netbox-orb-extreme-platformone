"""Thin Extreme Platform ONE client (token or user/pass auth) on plain `requests`.

Covers the two API families this worker consumes, both served from the same
host with the same bearer token:

  - Assets API (POST /assets/v1/devices): `page`/`limit` query params, a
    filter JSON body, and a response with top-level `data` + `total_pages`.
  - ConfigState API (POST /configstate/v1/retrieve-*): `page_number`/
    `page_size` query params, a per-table GetRequest body whose filter
    fields all take lists, and a response keyed by the table's schema name
    plus a `Pagination` object.

Auth is either a static API token or username/password via ``POST /login``
(ExtremeCloud IQ login on the same host). Password login refreshes the
bearer token before expiry and retries once on 401.

Contracts verified against the Platform ONE OpenAPI specs; see
tests/test_openapi_contract.py.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

import requests

from .urls import require_https_url

if TYPE_CHECKING:
    from collections.abc import Iterator

DEFAULT_BASE_URL = "https://cloudapi.extremecloudiq.com"
ASSETS_PAGE_LIMIT = 500  # documented max for the Assets `limit` query param
CONFIGSTATE_PAGE_SIZE = 500
# Cap filter ID lists per GetRequest so large estates do not blow gateway /
# body limits. retrieve() transparently chunks a single list-valued filter.
CONFIGSTATE_FILTER_CHUNK_SIZE = 200
# Keep API error text short so logs/exceptions do not retain full upstream
# bodies (which can include sensitive diagnostics).
_ERROR_BODY_LIMIT = 200
# Refresh a minute early so a request never races token expiry.
_TOKEN_REFRESH_SKEW_SECONDS = 60
_DEFAULT_TOKEN_TTL_SECONDS = 86400

logger = logging.getLogger("orb_extreme_platformone.client")


def _chunked(values: list, size: int):
    """Yield successive slices of `values` with at most `size` items."""
    if size <= 0:
        yield values
        return
    for index in range(0, len(values), size):
        yield values[index : index + size]


class PlatformOneApiError(RuntimeError):
    """Raised on a non-2xx response from a Platform ONE API."""


def truncate_error_body(text: str, *, limit: int = _ERROR_BODY_LIMIT) -> str:
    """Collapse whitespace and truncate an HTTP error body for safe logging."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    if limit <= 3:
        return cleaned[:limit]
    return cleaned[: limit - 3] + "..."


def configstate_response_key(table: str) -> str:
    """Derive a ConfigState response key from its table name.

    Every retrieve-<table> endpoint wraps its records under the table's
    PascalCase schema name: retrieve-asset-port-state -> "AssetPortState".
    """
    return "".join(part.capitalize() for part in table.split("-"))


class PlatformOneClient:
    """Minimal client for the Platform ONE endpoints this worker consumes.

    HTTP sessions are thread-local so independent ConfigState retrieves can
    run concurrently (see extract parallel table retrieves) without sharing a
    `requests.Session` across threads. Token state is guarded by a lock so
    password-login refresh is safe across those threads.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 60,
    ) -> None:
        if not api_token and not (username and password):
            msg = "PlatformOneClient requires api_token or username/password"
            raise ValueError(msg)
        self._base_url = require_https_url(base_url, what="PLATFORMONE_API_URL")
        self._username = username
        self._password = password
        self._timeout = timeout
        self._local = threading.local()
        self._lock = threading.Lock()
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if api_token:
            self._headers["Authorization"] = f"Bearer {api_token}"
            # None = static API token (never expires from our side) until a
            # login response sets it; password mode starts expired so the
            # first request logs in.
            self._token_expiry: float | None = None
        else:
            self._token_expiry = 0.0

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            self._local.session = session
        return session

    def _ensure_token_locked(self) -> None:
        if self._token_expiry is None:
            return
        if time.time() < self._token_expiry:
            return
        if self._username and self._password:
            self._login_locked()
            return
        msg = "No credentials available to authenticate with Platform ONE"
        raise PlatformOneApiError(msg)

    def _login_locked(self) -> None:
        url = f"{self._base_url}/login"
        payload = {"username": self._username, "password": self._password}
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        # Fail closed on redirects so a 307 cannot replay the password body.
        resp = self._session().post(
            url,
            headers=headers,
            json=payload,
            timeout=self._timeout,
            allow_redirects=False,
        )
        if 300 <= resp.status_code < 400:
            msg = f"Platform ONE login unexpected redirect ({resp.status_code})"
            raise PlatformOneApiError(msg)
        if resp.status_code != 200:
            detail = truncate_error_body(resp.text)
            msg = f"Platform ONE login failed ({resp.status_code}): {detail}"
            raise PlatformOneApiError(msg)
        data = resp.json()
        access_token = data.get("access_token")
        if not access_token:
            msg = "Platform ONE login response did not contain an access_token"
            raise PlatformOneApiError(msg)
        self._headers["Authorization"] = f"Bearer {access_token}"
        self._token_expiry = (
            time.time() + data.get("expires_in", _DEFAULT_TOKEN_TTL_SECONDS) - _TOKEN_REFRESH_SKEW_SECONDS
        )

    def _auth_headers(self) -> dict:
        with self._lock:
            self._ensure_token_locked()
            return dict(self._headers)

    def _post(self, path: str, params: dict, body: dict) -> dict:
        """POST `path`, re-logging in once on a 401 when using username/password.

        Transport failures and invalid JSON are raised as ``PlatformOneApiError``
        so ConfigState fan-out can degrade a single table instead of aborting
        the tick on a timeout/connection blip.
        """
        url = f"{self._base_url}{path}"
        for attempt in (1, 2):
            headers = self._auth_headers()
            try:
                resp = self._session().post(
                    url,
                    headers=headers,
                    params=params,
                    json=body,
                    timeout=self._timeout,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                msg = f"Platform ONE API request failed for {path}: {exc}"
                raise PlatformOneApiError(msg) from exc
            if 300 <= resp.status_code < 400:
                msg = f"Platform ONE API unexpected redirect {resp.status_code} for {path}"
                raise PlatformOneApiError(
                    msg,
                )
            if resp.status_code == 401 and attempt == 1 and self._username and self._password:
                with self._lock:
                    self._token_expiry = 0.0
                    self._login_locked()
                continue
            if resp.status_code >= 400:
                detail = truncate_error_body(resp.text)
                msg = f"Platform ONE API error {resp.status_code} for {path}: {detail}"
                raise PlatformOneApiError(msg)
            try:
                payload = resp.json()
            except ValueError as exc:
                msg = f"Platform ONE API returned invalid JSON for {path}: {exc}"
                raise PlatformOneApiError(
                    msg,
                ) from exc
            if not isinstance(payload, dict):
                msg = f"Platform ONE API returned non-object JSON for {path}: {type(payload).__name__}"
                raise PlatformOneApiError(
                    msg,
                )
            return payload
        msg = "unreachable"
        raise AssertionError(msg)  # pragma: no cover

    def _paginate(
        self,
        path: str,
        *,
        page_param: str,
        size_param: str,
        size: int,
        body: dict,
        response_key: str,
        total_pages,
    ) -> Iterator[dict]:
        page = 1
        while True:
            payload = self._post(path, {page_param: page, size_param: size}, body)
            yield from payload.get(response_key) or []
            last_page = total_pages(payload, page)
            if page >= last_page:
                break
            page += 1

    def get_devices(self, *, classification: str = "ALL", limit: int = ASSETS_PAGE_LIMIT) -> Iterator[dict]:
        """Yield every Assets-API device of `classification`, across all pages.

        `classification` (ALL, SWITCH, WIRELESS, ROUTER, ...) is passed
        through verbatim so new upstream values need no client change.
        """
        yield from self._paginate(
            "/assets/v1/devices",
            page_param="page",
            size_param="limit",
            size=limit,
            body={"classification": classification},
            response_key="data",
            total_pages=lambda payload, page: payload.get("total_pages") or page,
        )

    def _retrieve_pages(
        self,
        table: str,
        filters: dict,
        *,
        page_size: int,
    ) -> Iterator[dict]:
        response_key = configstate_response_key(table)
        yield from self._paginate(
            f"/configstate/v1/retrieve-{table}",
            page_param="page_number",
            size_param="page_size",
            size=page_size,
            body=filters,
            response_key=response_key,
            total_pages=lambda payload, page: (payload.get("Pagination") or {}).get("total_pages") or page,
        )

    def _retrieve_chunk(
        self,
        table: str,
        filters: dict,
        *,
        page_size: int,
    ) -> list[dict] | PlatformOneApiError:
        """Fetch one filter chunk to a list, or return the API error."""
        try:
            return list(self._retrieve_pages(table, filters, page_size=page_size))
        except PlatformOneApiError as exc:
            return exc

    def retrieve(
        self,
        table: str,
        filters: dict | None = None,
        *,
        page_size: int = CONFIGSTATE_PAGE_SIZE,
        filter_chunk_size: int = CONFIGSTATE_FILTER_CHUNK_SIZE,
    ) -> Iterator[dict]:
        """Yield every ConfigState record of retrieve-`table`, across all pages.

        `filters` is the table's GetRequest body; every filter field takes a
        list, e.g. retrieve("asset-port-state", {"asset_device_id": [a, b]}).
        The API rejects an empty filter body (code 1727) -- always pass at
        least one filter attribute with a non-empty list.

        When exactly one filter value is a list longer than
        ``filter_chunk_size``, the request is split into sequential chunked
        retrieves so large device/interface ID sets stay within gateway limits.
        """
        filters = dict(filters or {})
        list_fields = [(key, value) for key, value in filters.items() if isinstance(value, list)]
        if len(list_fields) == 1:
            field, values = list_fields[0]
            if len(values) > filter_chunk_size > 0:
                # Isolate per-chunk failures so a later transient error does not
                # discard rows already fetched from earlier chunks (list() would
                # otherwise drop everything when the iterator raises).
                chunks = list(_chunked(list(values), filter_chunk_size))
                errors: list[PlatformOneApiError] = []
                completed = 0
                for chunk in chunks:
                    result = self._retrieve_chunk(table, {**filters, field: chunk}, page_size=page_size)
                    if isinstance(result, PlatformOneApiError):
                        errors.append(result)
                        logger.warning(
                            "ConfigState retrieve-%s filter chunk failed (%d IDs); "
                            "continuing with remaining chunks: %s",
                            table,
                            len(chunk),
                            result,
                        )
                        continue
                    completed += 1
                    yield from result
                if errors and completed == 0:
                    raise errors[0]
                return

        yield from self._retrieve_pages(table, filters, page_size=page_size)
