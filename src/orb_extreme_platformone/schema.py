"""NetBox schema identifiers shared by bootstrap and transform.

A leaf module by design: ``bootstrap`` owns the *definitions* (and the REST
calls that create them) while ``transform`` only needs the *names* to stamp
onto entities. Both import from here so the pure mapping layer never has to
import the HTTP layer — and therefore never pulls ``requests`` into a
transform-only import graph.
"""

from __future__ import annotations

# Per-object-type Platform ONE correlation keys. See bootstrap.CUSTOM_FIELDS
# for the full definitions, including which of these enforce `unique`.
CF_DEVICE_ID = "platformone_device_id"
CF_INTERFACE_ID = "platformone_interface_id"
CF_CLUSTER_ID = "platformone_cluster_id"

# Fabric identity parameters (not unique — shared areas / nicknames are fine).
CF_ISIS_AREA = "platformone_isis_area"
CF_ISIS_SYSTEM_ID = "platformone_isis_system_id"
CF_SPBM_NICKNAME = "platformone_spbm_nickname"

# Provenance tags stamped on every entity this worker emits, in the order
# bootstrap creates them. See bootstrap.TAGS for colours and descriptions.
TAG_NAMES = ("extreme-networks", "platform-one", "discovered")

MANUFACTURER = "Extreme Networks"
