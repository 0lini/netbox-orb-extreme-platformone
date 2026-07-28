# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Device custom fields for fabric identity: `platformone_isis_area`,
  `platformone_isis_system_id`, and `platformone_spbm_nickname` (from
  ConfigState ISIS global config/state and SPBM instance). Re-run bootstrap
  once so NetBox creates the new field definitions.

### Changed
- Simplified shared Device identity construction, client pagination/retrieve chunking, LAG joins, backend port/radio fan-out, and primary-IP ranking helpers (behavior unchanged).
- Inferred-cluster member-side fetches now run concurrently via the existing parallel retrieve helper.
- Adopted `src/` layout, Astral tooling (`uv` / `ruff` / `ty`), and richer packaging metadata.
- Split port and wireless transform modules (and matching tests) by domain.
- Raised `setuptools` build requirement to `>=77` for PEP 639 license metadata.
- Example Orb Agent image pin: `netboxlabs/orb-agent:2.11.0` (was `:latest`).
- Stricter CI: broader Ruff rule set, 90% coverage floor (temporarily), ty warnings-as-errors,
  pytest `--strict-markers`/`--strict-config`, and package smoke gated on lint+tests.

### Fixed
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
