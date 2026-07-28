"""Concurrent ConfigState retrieve helpers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, NamedTuple

from orb_extreme_platformone.client import PlatformOneApiError, PlatformOneClient
from orb_extreme_platformone.http import MAX_CONCURRENT_REQUESTS
from orb_extreme_platformone.logging_context import current_policy_name, get_logger, set_policy_name

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = get_logger(__name__)

# Catalog: transform key -> (retrieve-* table, GetRequest filter field).
TableCatalog = dict[str, tuple[str, str]]


class RetrieveResult(NamedTuple):
    """One ConfigState retrieve outcome: rows on success, error on failure."""

    table: str
    rows: list[dict] | None
    error: PlatformOneApiError | None


def retrieve_parallel(
    client: PlatformOneClient,
    jobs: list[tuple[str, dict]],
) -> list[RetrieveResult]:
    """Run independent ConfigState retrieves concurrently.

    Returns one result per job in submission order (deterministic merge /
    failure lists). A failed job yields ``(table, None, exc)`` and does not
    abort siblings — including on an unexpected exception type, which would
    otherwise escape through ``future.result()`` and discard the rows every
    healthy sibling table had already fetched.
    """
    if not jobs:
        return []

    # contextvars do not cross into pool threads, so carry the policy name over
    # explicitly and re-bind it inside each worker. (Passing a copied Context to
    # submit() does not work: one Context cannot be entered by two threads.)
    policy_name = current_policy_name()

    def _run_job(table: str, filters: dict) -> RetrieveResult:
        set_policy_name(policy_name)
        try:
            return RetrieveResult(table, list(client.retrieve(table, filters)), None)
        except PlatformOneApiError as exc:
            return RetrieveResult(table, None, exc)
        except Exception as exc:
            logger.exception("ConfigState retrieve-%s raised an unexpected error", table)
            # status_code=None marks it transient, so it is not mistaken for an
            # auth failure by the fail-fast check in retrieve_ok.
            return RetrieveResult(table, None, PlatformOneApiError(f"unexpected error: {exc}"))

    workers = min(len(jobs), MAX_CONCURRENT_REQUESTS)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_run_job, table, filters) for table, filters in jobs]
        # result() in submit order: work still overlaps; merge stays deterministic.
        return [fut.result() for fut in futures]


def retrieve_ok(
    client: PlatformOneClient,
    jobs: list[tuple[str, dict]],
    contexts: list,
    *,
    policy_name: str,
    failed_tables: list[str],
    degradation: str,
) -> Iterator[tuple]:
    """Run jobs concurrently and yield ``(context, rows)`` for the successes.

    ``contexts`` pairs one caller-side value (a table key, per-job metadata,
    ...) with each job. A failed job is logged with ``degradation`` (what the
    tick loses), recorded in ``failed_tables``, and skipped, so callers only
    handle good rows.

    An authentication failure is the exception: bad credentials will fail every
    remaining table too, so it aborts the tick rather than degrading 15 times.
    """
    for context, result in zip(contexts, retrieve_parallel(client, jobs), strict=True):
        if result.error is not None:
            if result.error.is_auth_failure:
                logger.error(
                    "Policy %s: Platform ONE rejected our credentials (%s); aborting the tick",
                    policy_name,
                    result.error,
                )
                raise result.error
            failed_tables.append(result.table)
            logger.warning(
                "Policy %s: ConfigState %s fetch failed, %s: %s",
                policy_name,
                result.table,
                degradation,
                result.error,
            )
            continue
        if result.rows is None:
            continue
        yield context, result.rows


def extract_device_table_buckets(
    client: PlatformOneClient,
    device_ids: list[str],
    catalog: TableCatalog,
    *,
    policy_name: str,
    degradation: str,
    failed_tables: list[str] | None = None,
) -> tuple[dict[str, dict[str, list[dict]]], list[str]]:
    """Batched device-filtered retrieves, bucketed by device UUID.

    Returns ``(tables_by_device, failed_tables)``. Each device gets an empty
    list per catalog key; successful rows append into the matching bucket.
    Rows are keyed by the catalog's GetRequest filter field
    (``asset_device_id`` or ``device_id``) — no cross-field fallback.
    """
    failures = failed_tables if failed_tables is not None else []
    tables_by_device: dict[str, dict[str, list[dict]]] = {
        device_id: {key: [] for key in catalog} for device_id in device_ids
    }
    if not device_ids or not catalog:
        return tables_by_device, failures

    # Preserve catalog order for deterministic retrieve_ok contexts.
    catalog_items = list(catalog.items())
    jobs = [(table, {filter_field: device_ids}) for _, (table, filter_field) in catalog_items]
    for (key, (_, filter_field)), rows in retrieve_ok(
        client,
        jobs,
        catalog_items,
        policy_name=policy_name,
        failed_tables=failures,
        degradation=degradation,
    ):
        for row in rows:
            device_id = str(row.get(filter_field) or "")
            if device_id in tables_by_device:
                tables_by_device[device_id][key].append(row)
    return tables_by_device, failures
