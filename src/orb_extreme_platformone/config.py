"""Policy-config and environment resolution for the worker tick."""

from __future__ import annotations

import logging
import os

from .client import DEFAULT_BASE_URL, PlatformOneClient

logger = logging.getLogger(__name__)

# Credential keys: environment wins when set so checked-in YAML cannot pin an
# empty string or stale literal over the runtime secret store (SECURITY.md).
_SECRET_KEYS = frozenset(
    {
        "PLATFORMONE_API_TOKEN",
        "PLATFORMONE_USERNAME",
        "PLATFORMONE_PASSWORD",
        "NETBOX_API_TOKEN",
    },
)

__all__ = [
    "build_client",
    "policy_bool",
    "policy_or_env",
    "policy_value",
    "scope_sites",
]


def policy_value(config: object, key: str, default: object = None) -> object:
    """Read ``key`` from a policy config object, or ``default`` when absent."""
    return getattr(config, key, default) if config is not None else default


def policy_or_env(
    config: object,
    key: str,
    *,
    env_key: str | None = None,
) -> str | None:
    """Resolve a policy key with environment fallback.

    Non-secret keys: policy wins when set (including empty string); only a
    missing key falls through to the environment.

    Credential keys listed in ``_SECRET_KEYS``: environment wins when present
    so Orb policy objects never override a secret store with an empty YAML
    value. Missing env falls back to policy (including ``${VAR}``-substituted
    values when Orb injects them only into config).
    """
    resolved_env_key = env_key or key
    if key in _SECRET_KEYS or resolved_env_key in _SECRET_KEYS:
        env_value = os.environ.get(resolved_env_key)
        if env_value is not None:
            return env_value
        value = policy_value(config, key, None)
        return str(value) if value is not None else None

    value = policy_value(config, key, None)
    if value is not None:
        return str(value)
    return os.environ.get(resolved_env_key)


def policy_bool(config: object, key: str, *, default: bool = False) -> bool:
    """Resolve a boolean policy key, coercing environment strings.

    Environment values arrive as strings, so a bare truthiness test would read
    ``BOOTSTRAP=false`` as enabled.
    """
    value = policy_value(config, key, None)
    if isinstance(value, bool):
        return value
    raw = os.environ.get(key) if value is None else str(value)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def scope_sites(scope: object) -> set[str] | None:
    """Return an explicit site allow-list, or None for all sites.

    ``sites: ["*"]``, missing/empty ``sites``, and a non-dict scope all mean
    "no filter". A bare string is accepted as one site rather than split into
    characters the way ``set("HQ")`` would.
    """
    if not isinstance(scope, dict):
        return None
    sites = scope.get("sites")
    if sites in (None, [], ["*"], "*"):
        return None
    if isinstance(sites, str):
        return {sites.strip()} if sites.strip() else None
    if not isinstance(sites, (list, tuple, set)):
        logger.warning("Ignoring invalid policy scope.sites %r; syncing all sites", sites)
        return None
    return {str(site).strip() for site in sites if str(site).strip()} or None


def build_client(config: object) -> PlatformOneClient:
    """Build a Platform ONE client from policy config and environment."""
    return PlatformOneClient(
        base_url=policy_or_env(config, "PLATFORMONE_API_URL") or DEFAULT_BASE_URL,
        api_token=policy_or_env(config, "PLATFORMONE_API_TOKEN"),
        username=policy_or_env(config, "PLATFORMONE_USERNAME"),
        password=policy_or_env(config, "PLATFORMONE_PASSWORD"),
    )
