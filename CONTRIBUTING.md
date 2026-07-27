# Contributing

Thanks for contributing to **netbox-orb-extreme-platformone**.

## Development setup

```bash
uv sync --group dev
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run ty check
```

Optional: `pre-commit install` to run Ruff + ty on commit (see `.pre-commit-config.yaml`).

## Guidelines

- Keep the Orb worker install path working: `workers.txt` installs the repo root
  (`.`) or a **pinned** published package for production.
- Prefer small, domain-focused modules under `src/orb_extreme_platformone/`.
- Add or update offline pytest coverage for behavior changes.
- Do not commit `.env`, dry-run inventory JSON, or live credentials.
- Security posture and residual risks: see [`SECURITY.md`](SECURITY.md).

## Pull requests

- Target `main`.
- Keep CI green (lint, ty, Python 3.10–3.14 tests, package smoke).
- Update [`CHANGELOG.md`](CHANGELOG.md) under `[Unreleased]` for user-visible changes.
