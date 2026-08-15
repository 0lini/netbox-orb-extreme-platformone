"""Orb Agent worker entrypoint for the Extreme Platform ONE integration.

Implements the `worker.backend.Backend` contract from `netboxlabs-orb-worker`:
`describe()` reports identity, `run()` yields the Diode entities for one
policy tick. The PolicyRunner owns scheduling and the Diode client; this
module only produces entities.

ConfigState table catalogs and batched retrieves live in `extract/`; entity
transform lives in `transform/`. Policy/env resolution lives in `config`.
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import TYPE_CHECKING

from worker.backend import Backend as WorkerBackend
from worker.models import Metadata, Policy

from . import bootstrap, transform
from .client import PlatformOneApiError, PlatformOneClient
from .config import build_client, policy_bool, policy_or_env, scope_sites
from .extract import correlated_records
from .extract.clusters import extract_inferred_clusters
from .extract.fabric import extract_fabric_tables
from .extract.index import fanout_context, records_by_cs_id
from .extract.ports import extract_port_tables
from .extract.wireless import extract_wireless_tables

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from netboxlabs.diode.sdk.ingester import Entity

    from .identity import DeviceRecord

logger = logging.getLogger(__name__)

APP_NAME = "netbox-orb-extreme-platformone"
try:
    APP_VERSION = pkg_version("netbox-orb-extreme-platformone")
except PackageNotFoundError:
    APP_VERSION = "0.2.0"
# Sync every Assets device class by default (switches, APs, routers, ...);
# narrow with the `classification` policy key. Port sync stays gated on
# switch-OS devices regardless (see is_switch).
DEFAULT_CLASSIFICATION = "ALL"

__all__ = ["APP_NAME", "APP_VERSION", "DEFAULT_CLASSIFICATION", "Backend"]


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
        """Yield the Diode entities for one policy tick.

        Wraps ``_run`` so a tick that dies is recorded at ERROR with a
        traceback: without it the package logged nothing above WARNING, and a
        total failure was indistinguishable from routine table degradation.

        Entities are streamed in load-bearing order (devices → ports/radios →
        primary-IP follow-ups) without materializing the full estate list.
        """
        try:
            yield from self._run(policy_name, policy)
        except Exception:
            logger.exception("Policy %s: tick failed and produced no entities", policy_name)
            raise

    def _run(self, policy_name: str, policy: Policy) -> Iterator[Entity]:
        config = policy.config

        if policy_bool(config, "BOOTSTRAP"):
            self._bootstrap(config, policy_name)

        client = build_client(config)
        try:
            classification = (
                policy_or_env(config, "classification", env_key="PLATFORMONE_CLASSIFICATION")
                or DEFAULT_CLASSIFICATION
            )
            assets = list(client.get_devices(classification=classification))

            records = correlated_records(client, assets, policy_name)

            # Backend owns scoping: port fan-out and devices_to_entities must see
            # the same filtered list. Pass site_scope=None into transform so it
            # does not re-filter (see transform.scope_devices / devices_to_entities).
            scoped = transform.scope_devices(
                records,
                site_scope=scope_sites(getattr(policy, "scope", None)),
            )
            logger.info(
                "Policy %s: fetched %d devices from Platform ONE (%d in scope)",
                policy_name,
                len(records),
                len(scoped),
            )

            vc_entities, vc_memberships = self._virtual_chassis_entities(
                client,
                scoped,
                policy_name,
            )
            # Port/LAG/IP + fabric (ISIS/SPBM) tables are fetched before Device
            # entities so primary_ip can use ConfigState interface CIDRs and so
            # Device CFs can carry ISIS area / system id / SPBM nickname.
            port_entities, primary_ips_by_cs_id, fabric_by_cs_id = self._port_entities(
                client,
                scoped,
                policy_name,
            )
            radio_entities = self._radio_entities(client, scoped, policy_name)
            # Emit Devices *without* primary_ip* first so serial / custom fields are
            # not bundled into a Diode update that NetBox rejects when the IP is not
            # yet assigned. Assert primary_ip* in a follow-up after port/IP entities.
            device_entities = transform.devices_to_entities(
                scoped,
                virtual_chassis_entities=vc_entities,
                vc_memberships=vc_memberships,
                fabric_by_cs_id=fabric_by_cs_id,
            )
            primary_ip_entities = transform.primary_ip_device_entities(
                scoped,
                primary_ips_by_cs_id=primary_ips_by_cs_id,
            )

            entity_count = (
                len(device_entities) + len(port_entities) + len(radio_entities) + len(primary_ip_entities)
            )
            yield from device_entities
            yield from port_entities
            yield from radio_entities
            yield from primary_ip_entities

            logger.info(
                "Policy %s: tick complete; %d entities from %d in-scope device(s)",
                policy_name,
                entity_count,
                len(scoped),
            )
        finally:
            client.close()

    @staticmethod
    def _bootstrap(config: object, policy_name: str) -> None:
        """Create the NetBox custom fields and provenance tags, or fail closed.

        Failing closed is deliberate: syncing into a NetBox that lacks the
        ``platformone_*`` fields would silently drop provenance, so a bootstrap
        the operator explicitly asked for must not be skipped on error.
        """
        netbox_url = policy_or_env(config, "NETBOX_API_URL")
        netbox_token = policy_or_env(config, "NETBOX_API_TOKEN")
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
        records: list[DeviceRecord],
        policy_name: str,
    ) -> tuple[list[Entity], dict[str, dict]]:
        """Fetch InferredCluster and map to VirtualChassis + memberships.

        A failed fetch degrades to no VC entities for this tick rather than
        aborting the sync.
        """
        records_by_id = records_by_cs_id(records, predicate=lambda _record: True)
        cs_device_ids = sorted(records_by_id)
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
            records_by_cs_id=records_by_id,
        )
        unclustered = sorted(set(records_by_id) - set(memberships))
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
        records: list[DeviceRecord],
        policy_name: str,
    ) -> tuple[list[Entity], dict[str, dict[str, str]], dict[str, dict]]:
        """Fetch port/LAG + fabric tables for in-scope switches and map entities.

        Returns ``(port_entities, primary_ips_by_cs_id, fabric_by_cs_id)`` so
        Device primary IPs and ISIS/SPBM custom fields reuse the same switch
        fan-out.
        """
        switches, cs_device_ids, names = fanout_context(
            records,
            predicate=lambda record: record.is_switch,
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
            record = switches[cs_device_id]
            primary = transform.primary_ips_from_tables(tables, asset_ip=record.asset_ip)
            if primary:
                primary_ips_by_cs_id[cs_device_id] = primary
            if cs_device_id not in names:
                continue
            entities.extend(transform.ports_to_entities(tables, record=record))
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
    def _radio_entities(
        client: PlatformOneClient,
        records: list[DeviceRecord],
        policy_name: str,
    ) -> list[Entity]:
        """Fetch wireless/SSID tables for in-scope APs and map to Diode entities."""
        aps, cs_device_ids, names = fanout_context(
            records,
            predicate=lambda record: record.is_ap,
            policy_name=policy_name,
            kind="radios",
        )
        if not cs_device_ids:
            return []

        tables_by_device, failed_tables = extract_wireless_tables(
            client,
            cs_device_ids,
            policy_name,
        )
        entities = transform.radios_to_entities(
            tables_by_device,
            records={cs_id: aps[cs_id] for cs_id in names},
        )
        logger.info("Policy %s: mapped %d wireless radio/WLAN entities", policy_name, len(entities))
        _log_failed_tables(policy_name, failed_tables, domain="wireless ")
        return entities
