"""Extreme Platform ONE client: pagination and filter chunking over a transport.

Covers the two API families this worker consumes, both served from the same
host with the same bearer token:

  - Assets API (POST /assets/v1/devices): `page`/`limit` query params, a
    filter JSON body, and a response with top-level `data` + `total_pages`.
  - ConfigState API (POST /configstate/v1/retrieve-*): `page_number`/
    `page_size` query params, a per-table GetRequest body whose filter
    fields all take lists, and a response keyed by the table's schema name
    plus a `Pagination` object.

Credentials, sessions, retry/backoff and HTTP error mapping live in
:mod:`orb_extreme_platformone.http`; this module changes when an API's
pagination or filter shape changes, not when auth or transport does.

Contracts verified against the Platform ONE OpenAPI specs; see
tests/test_openapi_contract.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .http import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    PlatformOneApiError,
    PlatformOneTransport,
    truncate_error_body,
)
from .logging_context import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

ASSETS_PAGE_LIMIT = 500  # documented max for the Assets `limit` query param
CONFIGSTATE_PAGE_SIZE = 500
# Cap filter ID lists per GetRequest so large estates do not blow gateway /
# body limits. retrieve() transparently chunks a single list-valued filter.
CONFIGSTATE_FILTER_CHUNK_SIZE = 200
# Guard against a server that never stops reporting more pages: without this a
# misreported total_pages loops forever at one timeout per request.
_MAX_PAGES = 10_000

__all__ = [
    "ASSETS_PAGE_LIMIT",
    "CONFIGSTATE_FILTER_CHUNK_SIZE",
    "CONFIGSTATE_PAGE_SIZE",
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "PlatformOneApiError",
    "PlatformOneClient",
    "configstate_response_key",
    "truncate_error_body",
]

logger = get_logger(__name__)


def _coerce_page_count(value, *, default: int) -> int:
    """Page counts arrive as ints; tolerate digit strings, reject anything else.

    A malformed-but-200 body must degrade this table, not raise TypeError out of
    the pool thread and abort the whole fan-out.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


def _chunked(values: list, size: int):
    """Yield successive slices of `values` with at most `size` items."""
    if size <= 0:
        yield values
        return
    for index in range(0, len(values), size):
        yield values[index : index + size]


def configstate_response_key(table: str) -> str:
    """Derive a ConfigState response key from its table name.

    Every retrieve-<table> endpoint wraps its records under the table's
    PascalCase schema name: retrieve-asset-port-state -> "AssetPortState".
    """
    return "".join(part.capitalize() for part in table.split("-"))


class PlatformOneClient:
    """Paginated, chunked access to the Platform ONE endpoints this worker uses.

    Delegates every HTTP concern to :class:`~orb_extreme_platformone.http.PlatformOneTransport`.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: PlatformOneTransport | None = None,
    ) -> None:
        self._transport = transport or PlatformOneTransport(
            base_url=base_url,
            api_token=api_token,
            username=username,
            password=password,
            timeout=timeout,
        )

    def close(self) -> None:
        """Release this thread's HTTP session at the end of a tick."""
        self._transport.close()

    def _post(self, path: str, params: dict, body: dict) -> dict:
        return self._transport.post(path, params, body)

    def _paginate(
        self,
        path: str,
        *,
        page_param: str,
        size_param: str,
        size: int,
        body: dict,
        response_key: str,
        total_pages: Callable[[dict, int], object],
    ) -> Iterator[dict]:
        page = 1
        while True:
            payload = self._post(path, {page_param: page, size_param: size}, body)
            records = payload.get(response_key) or []
            if not isinstance(records, list):
                msg = (
                    f"Platform ONE API returned non-list {response_key!r} for {path}: "
                    f"{type(records).__name__}"
                )
                raise PlatformOneApiError(msg, path=path)
            yield from records
            last_page = _coerce_page_count(total_pages(payload, page), default=page)
            if page >= last_page:
                break
            if page >= _MAX_PAGES:
                logger.warning(
                    "Stopping %s pagination at page %d (server reported %s pages)",
                    path,
                    page,
                    last_page,
                )
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

    def _retrieve_chunk_or_error(
        self,
        table: str,
        filters: dict,
        *,
        page_size: int,
    ) -> list[dict] | PlatformOneApiError:
        """Fetch one filter chunk to a list, or return (not raise) the API error.

        Returning the error keeps rows already fetched from earlier chunks; see
        the caller in ``retrieve``.
        """
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

        When a filter value is a list longer than ``filter_chunk_size``, the
        request is split into sequential chunked retrieves so large device /
        interface ID sets stay within gateway limits. With several list-valued
        filters the longest one is chunked.
        """
        filters = dict(filters or {})
        list_fields = [(key, value) for key, value in filters.items() if isinstance(value, list)]
        if list_fields:
            field, values = max(list_fields, key=lambda item: len(item[1]))
            if len(list_fields) > 1:
                logger.warning(
                    "ConfigState retrieve-%s has %d list filters; chunking only %r (%d values)",
                    table,
                    len(list_fields),
                    field,
                    len(values),
                )
            if len(values) > filter_chunk_size > 0:
                yield from self._retrieve_chunked(
                    table,
                    filters,
                    field=field,
                    values=list(values),
                    page_size=page_size,
                    filter_chunk_size=filter_chunk_size,
                )
                return

        yield from self._retrieve_pages(table, filters, page_size=page_size)

    def _retrieve_chunked(
        self,
        table: str,
        filters: dict,
        *,
        field: str,
        values: list,
        page_size: int,
        filter_chunk_size: int,
    ) -> Iterator[dict]:
        """Retrieve one table in filter chunks, isolating per-chunk failures.

        A later transient error must not discard rows already fetched from
        earlier chunks (``list()`` would drop everything when the iterator
        raises), so each chunk is collected independently and only an
        all-chunks-failed outcome raises.
        """
        chunks = list(_chunked(values, filter_chunk_size))
        errors: list[PlatformOneApiError] = []
        completed = 0
        for chunk in chunks:
            result = self._retrieve_chunk_or_error(table, {**filters, field: chunk}, page_size=page_size)
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
            codes = sorted({exc.status_code for exc in errors if exc.status_code is not None})
            msg = (
                f"ConfigState retrieve-{table}: all {len(chunks)} filter chunks failed "
                f"(status codes: {codes or 'transport/parse errors'}); first error: {errors[0]}"
            )
            raise PlatformOneApiError(
                msg,
                status_code=errors[0].status_code,
                path=f"/configstate/v1/retrieve-{table}",
            ) from errors[0]
