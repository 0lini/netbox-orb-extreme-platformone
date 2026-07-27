# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Device custom fields for fabric identity: `platformone_isis_area`,
  `platformone_isis_system_id`, and `platformone_spbm_nickname` (from
  ConfigState ISIS global config/state and SPBM instance).
- VirtualChassis custom field `platformone_vsmlt_bmac` for the SMLT/VIST
  cluster-pair backbone MAC from MLAG `peer_bmac`. Re-run bootstrap once so
  NetBox creates the new field definitions.

### Changed
- Adopted `src/` layout, Astral tooling (`uv` / `ruff` / `ty`), and richer packaging metadata.
- Split port and wireless transform modules (and matching tests) by domain.
- Raised `setuptools` build requirement to `>=77` for PEP 639 license metadata.
- Example Orb Agent image pin: `netboxlabs/orb-agent:2.11.0` (was `:latest`).

### Fixed
- ConfigState correlation no longer drops non-int Assets `device_id` values.

## [0.2.0] - 2026-07-25

### Added
- Extreme Platform ONE → NetBox Orb worker (`orb_extreme_platformone`).
