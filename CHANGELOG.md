# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Retry with exponential backoff for transient Platform ONE failures (429/5xx and
  connection errors), honouring `Retry-After`. A single upstream blip previously
  cost a whole ConfigState table until the next scheduled run.
- `PlatformOneApiError` and `NetBoxApiError` carry `status_code` and expose
  `is_auth_failure` / `is_not_found` / `is_transient`, so callers can tell a
  permanent failure from a retryable one. An authentication failure now aborts
  the tick instead of degrading every remaining table in turn.
- Tick failures are logged at ERROR with a traceback; the package previously
  had no ERROR-level record at all, so a dead tick was indistinguishable from
  routine table degradation.
- Device custom fields for fabric identity: `platformone_isis_area`,
  `platformone_isis_system_id`, and `platformone_spbm_nickname` (from
  ConfigState ISIS global config/state and SPBM instance). Re-run bootstrap
  once so NetBox creates the new field definitions.

### Changed
- The pipeline is built around a `DeviceRecord` domain type instead of an
  untyped dict plus a parallel "meta" dict; name, site, OS family and location
  are derived properties resolved once. Entity output is unchanged.
- HTTP transport (credentials, sessions, retry, error mapping) split out of
  `PlatformOneClient` into `http.py`; the client keeps pagination and chunking.
- Custom-field names and provenance tags moved to `schema.py`, and the
  ConfigState catalogs to `catalog.py`, so the transform layer no longer imports
  the NetBox REST module or the extract package.
- Interface-IP retrieves are chunked by the caller so the chunks run
  concurrently; one process-wide thread pool keeps connections pooled across
  fan-out phases.
- Simplified shared Device identity construction, client pagination/retrieve chunking, LAG joins, backend port/radio fan-out, and primary-IP ranking helpers (behavior unchanged).
- Inferred-cluster member-side fetches now run concurrently via the existing parallel retrieve helper.
- Adopted `src/` layout, Astral tooling (`uv` / `ruff` / `ty`), and richer packaging metadata.
- Split port and wireless transform modules (and matching tests) by domain.
- Raised `setuptools` build requirement to `>=77` for PEP 639 license metadata.
- Example Orb Agent image pin: `netboxlabs/orb-agent:2.11.0` (was `:latest`).
- Stricter CI: broader Ruff rule set, 90% coverage floor (temporarily), ty warnings-as-errors,
  pytest `--strict-markers`/`--strict-config`, and package smoke gated on lint+tests.

### Fixed
- `python -m orb_extreme_platformone` produced no output: the standalone dry run
  built each entity's JSON and discarded it.
- Secret-bearing fields are redacted from upstream error bodies before they
  reach an exception message or a log line.
- A malformed but successful ConfigState response (non-list records, or
  `total_pages` as a string) raised `TypeError` out of a worker thread and
  aborted the whole tick; such responses now degrade a single table.
- A NetBox failure during bootstrap no longer aborts the Platform ONE sync with
  a raw `requests` exception; it raises an actionable error naming both remedies.
- Pagination is capped, so a server misreporting `total_pages` cannot loop
  indefinitely.
- ConfigState correlation no longer drops non-int Assets `device_id` values.
- IP assignment stubs no longer re-assert `type=other` when the parent port
  omitted `type` (config-only / port-state degrade).
- Chunked ConfigState retrieves keep rows from successful filter chunks when a
  later chunk fails (raise only if every chunk fails).
- Wireless radio config joins warn on duplicate `asset_interface_id` rows
  (first-row wins, same as ports) and use one primary-state helper for name/RF.
- Stop inventing NetBox values when Platform ONE omits the source field:
  Device `status`, WirelessLAN `status` / `auth_*`, and Interface `type`
  are omitted instead of defaulting to `active` / `open`/`auto` / `other`.

## [0.2.0] - 2026-07-25

### Added
- Extreme Platform ONE → NetBox Orb worker (`orb_extreme_platformone`).
