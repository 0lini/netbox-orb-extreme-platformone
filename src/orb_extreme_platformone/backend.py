"""Orb Agent worker entrypoint for the Extreme Platform ONE integration.

Implements the `worker.backend.Backend` contract from `netboxlabs-orb-worker`:
`describe()` reports identity, `run()` returns the Diode entities for one
policy tick. The PolicyRunner owns scheduling and the Diode client; this
module only produces entities.

ConfigState table catalogs and batched retrieves live in `extract/`; entity
transform lives in `transform/`.
"""

from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import TYPE_CHECKING, NamedTuple, overload

from worker.backend import Backend as WorkerBackend
from worker.models import Metadata, Policy

from . import bootstrap, transform
from .client import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    PlatformOneApiError,
    PlatformOneClient,
)
from .extract import (
    CLUSTER_MEMBER_FILTERS,
    FABRIC_DEVICE_TABLES,
    INTERFACE_ID_TABLES,
    PORT_TABLES,
    WIRELESS_TABLES,
    correlated_records,
)
from .extract.clusters import extract_inferred_clusters
from .extract.fabric import extract_fabric_tables
from .extract.ports import extract_port_tables
from .extract.wireless import extract_wireless_tables
from .identity import asset_label, device_name, is_ap, is_switch, resolve_location
from .logging_context import get_logger, set_policy_name

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from netboxlabs.diode.sdk.ingester import Entity

logger = get_logger(__name__)

APP_NAME = "netbox-orb-extreme-platformone"
try:
    APP_VERSION = pkg_version("netbox-orb-extreme-platformone")
except PackageNotFoundError:
    APP_VERSION = "0.2.0"
# Sync every Assets device class by default (switches, APs, routers, ...);
# narrow with the `classification` policy key. Port sync stays gated on
# switch-OS devices regardless (see is_switch).
DEFAULT_CLASSIFICATION = "ALL"

# Per-tick discovery phases an operator can switch off with `disable_domains`
# when one starts misbehaving, without editing code and redeploying.
DISCOVERY_DOMAINS = ("virtual_chassis", "ports", "wireless")

# Re-exported for tests / contract checks that historically imported catalogs
# from this module.
__all__ = [
    "APP_NAME",
    "APP_VERSION",
    "CLUSTER_MEMBER_FILTERS",
    "DEFAULT_CLASSIFICATION",
    "FABRIC_DEVICE_TABLES",
    "INTERFACE_ID_TABLES",
    "PORT_TABLES",
    "WIRELESS_TABLES",
    "Backend",
]


def _log_failed_tables(policy_name: str, failed_tables: list[str], *, domain: str = "") -> None:
    """Warn once when any ConfigState table degraded during a fan-out."""
    if not failed_tables:
        return
    label = f"ConfigState {domain}degradation" if domain else "ConfigState degradation"
    logger.warning(
        "Policy %s: %s this tick; failed tables: %s",
        policy_name,
        label,
        ", ".join(failed_tables),
    )


def _policy_value(config: object, key: str, default: object = None) -> object:
    return getattr(config, key, default) if config is not None else default


@overload
def _policy_or_env(config: object, key: str, *, env_key: str | None = ..., default: str) -> str: ...


@overload
def _policy_or_env(
    config: object,
    key: str,
    *,
    env_key: str | None = ...,
    default: None = ...,
) -> str | None: ...


def _policy_or_env(
    config: object,
    key: str,
    *,
    env_key: str | None = None,
    default: str | None = None,
) -> str | None:
    """Policy config wins when set (including empty string); else environment.

    ``env_key`` names the environment variable when it differs from the policy
    key (the policy key is the documented ``agent.yaml`` contract, while the
    environment spelling is conventionally upper-case).
    """
    value = _policy_value(config, key, None)
    if value is not None:
        return str(value)
    return os.environ.get(env_key or key, default)


def _policy_bool(config: object, key: str, *, default: bool = False) -> bool:
    """Resolve a boolean policy key, coercing environment strings.

    Environment values arrive as strings, so a bare truthiness test would read
    ``BOOTSTRAP=false`` as enabled.
    """
    value = _policy_value(config, key, None)
    if isinstance(value, bool):
        return value
    raw = os.environ.get(key) if value is None else str(value)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _enabled_domains(config: object) -> set[str]:
    """Discovery domains to run this tick; `disable_domains` opts phases out.

    Note the coupling: disabling ``ports`` also drops Device primary IPs and
    the ISIS/SPBM custom fields, because the port fan-out is what supplies them.
    """
    raw = _policy_value(config, "disable_domains", None)
    if raw is None:
        return set(DISCOVERY_DOMAINS)
    if isinstance(raw, str):
        disabled = {raw.strip()}
    elif isinstance(raw, (list, tuple, set)):
        disabled = {str(item).strip() for item in raw if str(item).strip()}
    else:
        logger.warning("Ignoring invalid disable_domains %r; running every domain", raw)
        return set(DISCOVERY_DOMAINS)
    unknown = sorted(disabled - set(DISCOVERY_DOMAINS))
    if unknown:
        logger.warning(
            "Ignoring unknown disable_domains entries %s; known domains: %s",
            ", ".join(unknown),
            ", ".join(DISCOVERY_DOMAINS),
        )
    return set(DISCOVERY_DOMAINS) - disabled


def _scope_sites(scope: object) -> list[str] | None:
    """Return an explicit site allow-list, or None for all sites.

    Accepts ``sites: ["*"]``, missing/empty ``sites``, or a non-dict scope as
    "no filter". A bare string is rejected (``list("HQ")`` would otherwise
    become ``["H", "Q"]``).
    """
    if not isinstance(scope, dict):
        return None
    sites = scope.get("sites")
    if sites in (None, [], ["*"], "*"):
        return None
    if isinstance(sites, str):
        logger.warning("Ignoring invalid policy scope.sites string %r; syncing all sites", sites)
        return None
    if not isinstance(sites, (list, tuple, set)):
        logger.warning("Ignoring invalid policy scope.sites %r; syncing all sites", sites)
        return None
    cleaned = [str(site).strip() for site in sites if str(site).strip()]
    return cleaned or None


def _records_by_cs_id(records: list[dict], *, predicate: Callable[[dict], bool]) -> dict[str, dict]:
    """Index records by cs_device_id, keeping the first and warning on collisions.

    Port/radio/VC fan-out is keyed by ConfigState UUID; two Assets rows that
    correlate to the same UUID would otherwise silently overwrite each other.
    """
    by_id: dict[str, dict] = {}
    for record in records:
        cs_id = record.get("cs_device_id")
        if not cs_id or not predicate(record):
            continue
        if cs_id in by_id:
            duplicate = asset_label(record["asset"])
            logger.warning(
                "Duplicate ConfigState device id %s across Assets rows (%r and %r); "
                "keeping the first — %r will sync as a Device with no ports/radios/VC",
                cs_id,
                asset_label(by_id[cs_id]["asset"]),
                duplicate,
                duplicate,
            )
            continue
        by_id[cs_id] = record
    return by_id


def _device_names(
    records_by_cs_id: dict[str, dict],
    *,
    policy_name: str,
    kind: str,
) -> dict[str, str]:
    """Map ConfigState device id → hostname; warn and omit empty host_name rows."""
    names: dict[str, str] = {}
    for device_id, record in records_by_cs_id.items():
        name = device_name(record["asset"])
        if name:
            names[device_id] = name
            continue
        logger.warning(
            "Policy %s: skipping %s for %s: Assets host_name is empty",
            policy_name,
            kind,
            asset_label(record["asset"]),
        )
    return names


class FanoutContext(NamedTuple):
    """Per-device indexes a ConfigState fan-out phase needs."""

    records_by_cs_id: dict[str, dict]
    cs_device_ids: list[str]
    device_names: dict[str, str]
    device_meta: dict[str, dict]


def _fanout_context(
    records: list[dict],
    *,
    predicate: Callable[[dict], bool],
    policy_name: str,
    kind: str,
) -> FanoutContext:
    """Build the ConfigState fan-out indexes for per-device table extracts."""
    records_by_cs_id = _records_by_cs_id(records, predicate=predicate)
    cs_device_ids = sorted(records_by_cs_id)
    device_names = _device_names(records_by_cs_id, policy_name=policy_name, kind=kind)
    device_meta: dict[str, dict] = {}
    for device_id, record in records_by_cs_id.items():
        asset = record["asset"]
        site_name, _ = resolve_location(record.get("location"), asset)
        device_meta[device_id] = {
            "site_name": site_name,
            "function": asset.get("function"),
            "product_type": asset.get("product_type"),
            "asset_ip": asset.get("ip_address"),
        }
    return FanoutContext(records_by_cs_id, cs_device_ids, device_names, device_meta)


def _policy_timeout(config: object) -> float:
    """Per-request timeout in seconds; falls back to the transport default."""
    raw = _policy_or_env(config, "PLATFORMONE_TIMEOUT")
    if raw is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring invalid PLATFORMONE_TIMEOUT %r; using %ss",
            raw,
            DEFAULT_TIMEOUT_SECONDS,
        )
        return DEFAULT_TIMEOUT_SECONDS
    if timeout <= 0:
        logger.warning(
            "Ignoring non-positive PLATFORMONE_TIMEOUT %r; using %ss",
            raw,
            DEFAULT_TIMEOUT_SECONDS,
        )
        return DEFAULT_TIMEOUT_SECONDS
    return timeout


def _build_client(config: object) -> PlatformOneClient:
    return PlatformOneClient(
        base_url=_policy_or_env(config, "PLATFORMONE_API_URL", default=DEFAULT_BASE_URL),
        api_token=_policy_or_env(config, "PLATFORMONE_API_TOKEN"),
        username=_policy_or_env(config, "PLATFORMONE_USERNAME"),
        password=_policy_or_env(config, "PLATFORMONE_PASSWORD"),
        timeout=_policy_timeout(config),
    )


class Backend(WorkerBackend):
    """Extreme Platform ONE discovery worker backend."""

    @classmethod
    def describe(cls) -> Metadata:
        """Report this worker's identity without constructing it."""
        return Metadata(
            name="orb_extreme_platformone",
            app_name=APP_NAME,
            app_version=APP_VERSION,
            description=(
                "Extreme Platform ONE discovery worker: ingests devices, sites, "
                "ports, and AP radios/WLANs into NetBox."
            ),
        )

    def run(self, policy_name: str, policy: Policy, **_kwargs) -> Iterable[Entity]:
        """Produce the Diode entities for one policy tick.

        Wraps ``_run`` so a tick that dies is recorded at ERROR with a
        traceback: without it the package logged nothing above WARNING, and a
        total failure was indistinguishable from routine table degradation.
        """
        set_policy_name(policy_name)
        try:
            return self._run(policy_name, policy)
        except Exception:
            logger.exception("Policy %s: tick failed and produced no entities", policy_name)
            raise

    def _run(self, policy_name: str, policy: Policy) -> list[Entity]:
        config = policy.config

        if _policy_bool(config, "BOOTSTRAP"):
            self._bootstrap(config, policy_name)

        client = _build_client(config)
        classification = _policy_or_env(
            config,
            "classification",
            env_key="PLATFORMONE_CLASSIFICATION",
            default=DEFAULT_CLASSIFICATION,
        )
        assets = list(client.get_devices(classification=classification))

        records = correlated_records(client, assets, policy_name)

        scope_sites = _scope_sites(getattr(policy, "scope", None))
        # Backend owns scoping: port fan-out and devices_to_entities must see
        # the same filtered list. Pass site_scope=None into transform so it
        # does not re-filter (see transform.scope_devices / devices_to_entities).
        scoped = transform.scope_devices(
            records,
            site_scope=set(scope_sites) if scope_sites else None,
        )
        logger.info(
            "Policy %s: fetched %d devices from Platform ONE (%d in scope)",
            policy_name,
            len(records),
            len(scoped),
        )

        domains = _enabled_domains(config)
        if domains != set(DISCOVERY_DOMAINS):
            logger.info(
                "Policy %s: discovery domains disabled by policy: %s",
                policy_name,
                ", ".join(sorted(set(DISCOVERY_DOMAINS) - domains)),
            )

        vc_entities, vc_memberships = (
            self._virtual_chassis_entities(client, scoped, policy_name)
            if "virtual_chassis" in domains
            else ([], {})
        )
        # Port/LAG/IP + fabric (ISIS/SPBM) tables are fetched before Device
        # entities so primary_ip can use ConfigState interface CIDRs and so
        # Device CFs can carry ISIS area / system id / SPBM nickname.
        port_entities, primary_ips_by_cs_id, fabric_by_cs_id = (
            self._port_entities(client, scoped, policy_name) if "ports" in domains else ([], {}, {})
        )
        radio_entities = self._radio_entities(client, scoped, policy_name) if "wireless" in domains else []
        # Emit Devices *without* primary_ip* first so serial / custom fields are
        # not bundled into a Diode update that NetBox rejects when the IP is not
        # yet assigned. Assert primary_ip* in a follow-up after port/IP entities.
        entities = transform.devices_to_entities(
            scoped,
            virtual_chassis_entities=vc_entities,
            vc_memberships=vc_memberships,
            fabric_by_cs_id=fabric_by_cs_id,
        )
        entities.extend(port_entities)
        entities.extend(radio_entities)
        entities.extend(
            transform.primary_ip_device_entities(scoped, primary_ips_by_cs_id=primary_ips_by_cs_id),
        )

        client.close()
        logger.info(
            "Policy %s: tick complete; %d entities from %d in-scope device(s)",
            policy_name,
            len(entities),
            len(scoped),
        )
        return entities

    @staticmethod
    def _bootstrap(config: object, policy_name: str) -> None:
        """Create the NetBox custom fields and provenance tags, or fail closed.

        Failing closed is deliberate: syncing into a NetBox that lacks the
        ``platformone_*`` fields would silently drop provenance, so a bootstrap
        the operator explicitly asked for must not be skipped on error.
        """
        netbox_url = _policy_or_env(config, "NETBOX_API_URL")
        netbox_token = _policy_or_env(config, "NETBOX_API_TOKEN")
        if not netbox_url or not netbox_token:
            msg = (
                "BOOTSTRAP is enabled but NETBOX_API_URL / NETBOX_API_TOKEN "
                "are missing; provide both or set BOOTSTRAP: false"
            )
            raise ValueError(msg)
        logger.info("Policy %s: running bootstrap (custom fields + provenance tags)", policy_name)
        try:
            bootstrap.ensure_schema(netbox_url, netbox_token)
        except bootstrap.NetBoxApiError as exc:
            msg = (
                f"Bootstrap failed against NetBox ({exc}); custom fields and tags may be "
                "incomplete. Fix NetBox connectivity and re-run with BOOTSTRAP: true, "
                "or set BOOTSTRAP: false to sync without schema setup."
            )
            raise RuntimeError(msg) from exc

    @staticmethod
    def _virtual_chassis_entities(
        client: PlatformOneClient,
        records: list[dict],
        policy_name: str,
    ) -> tuple[list[Entity], dict[str, dict]]:
        """Fetch InferredCluster and map to VirtualChassis + memberships.

        A failed fetch degrades to no VC entities for this tick rather than
        aborting the sync.
        """
        records_by_cs_id = _records_by_cs_id(records, predicate=lambda _record: True)
        cs_device_ids = sorted(records_by_cs_id)
        if not cs_device_ids:
            return [], {}

        try:
            clusters = extract_inferred_clusters(client, cs_device_ids)
        except PlatformOneApiError as exc:
            # Diode upsert cannot clear virtual_chassis when omitted, so a failed
            # fetch leaves prior NetBox memberships sticky until a later success.
            logger.warning(
                "Policy %s: ConfigState inferred-cluster fetch failed, syncing without "
                "VirtualChassis updates (prior NetBox memberships may remain): %s",
                policy_name,
                exc,
            )
            return [], {}

        entities, memberships = transform.virtual_chassis_to_entities(
            clusters,
            records_by_cs_id=records_by_cs_id,
        )
        unclustered = sorted(set(records_by_cs_id) - set(memberships))
        if unclustered:
            # Diode has no null-clear for virtual_chassis; devices that left a
            # cluster keep any prior NetBox membership until edited manually.
            logger.info(
                "Policy %s: %d in-scope device(s) have no InferredCluster membership "
                "this tick; Diode will not clear a prior VirtualChassis link",
                policy_name,
                len(unclustered),
            )
        logger.info(
            "Policy %s: mapped %d VirtualChassis entities from %d InferredCluster rows",
            policy_name,
            len(entities),
            len(clusters),
        )
        return entities, memberships

    @staticmethod
    def _port_entities(
        client: PlatformOneClient,
        records: list[dict],
        policy_name: str,
    ) -> tuple[list[Entity], dict[str, dict[str, str]], dict[str, dict]]:
        """Fetch port/LAG + fabric tables for in-scope switches and map entities.

        Returns ``(port_entities, primary_ips_by_cs_id, fabric_by_cs_id)`` so
        Device primary IPs and ISIS/SPBM custom fields reuse the same switch
        fan-out.
        """
        _switches, cs_device_ids, device_names, device_meta = _fanout_context(
            records,
            predicate=lambda record: is_switch(record["asset"].get("function")),
            policy_name=policy_name,
            kind="ports",
        )
        if not cs_device_ids:
            return [], {}, {}

        tables_by_device, failed_tables = extract_port_tables(client, cs_device_ids, policy_name)
        fabric_tables, fabric_failed = extract_fabric_tables(client, cs_device_ids, policy_name)
        failed_tables.extend(fabric_failed)

        fabric_by_cs_id: dict[str, dict] = {}
        for device_id, fabric in fabric_tables.items():
            fields = transform.device_fabric_custom_fields(fabric)
            if fields:
                fabric_by_cs_id[device_id] = fields

        entities: list[Entity] = []
        primary_ips_by_cs_id: dict[str, dict[str, str]] = {}
        for cs_device_id in cs_device_ids:
            tables = tables_by_device[cs_device_id]
            meta = device_meta[cs_device_id]
            primary = transform.primary_ips_from_tables(tables, asset_ip=meta.get("asset_ip"))
            if primary:
                primary_ips_by_cs_id[cs_device_id] = primary
            name = device_names.get(cs_device_id)
            if not name:
                continue
            entities.extend(
                transform.ports_to_entities(
                    tables,
                    device=name,
                    function=meta.get("function"),
                    site_name=meta.get("site_name"),
                    product_type=meta.get("product_type"),
                ),
            )
        logger.info("Policy %s: mapped %d wired port entities", policy_name, len(entities))
        if fabric_by_cs_id:
            logger.info(
                "Policy %s: attached fabric custom fields for %d switch(es)",
                policy_name,
                len(fabric_by_cs_id),
            )
        _log_failed_tables(policy_name, failed_tables)
        return entities, primary_ips_by_cs_id, fabric_by_cs_id

    @staticmethod
    def _radio_entities(client: PlatformOneClient, records: list[dict], policy_name: str) -> list[Entity]:
        """Fetch wireless/SSID tables for in-scope APs and map to Diode entities."""
        _aps, cs_device_ids, device_names, device_meta = _fanout_context(
            records,
            predicate=lambda record: is_ap(record["asset"].get("function")),
            policy_name=policy_name,
            kind="radios",
        )
        if not cs_device_ids:
            return []

        tables_by_device, failed_tables = extract_wireless_tables(client, cs_device_ids, policy_name)
        entities = transform.radios_to_entities(
            tables_by_device,
            device_names=device_names,
            device_meta=device_meta,
        )
        logger.info("Policy %s: mapped %d wireless radio/WLAN entities", policy_name, len(entities))
        _log_failed_tables(policy_name, failed_tables, domain="wireless ")
        return entities
