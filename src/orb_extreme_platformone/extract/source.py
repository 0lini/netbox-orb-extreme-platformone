"""Narrow ConfigState capability the extract layer depends on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterator


@runtime_checkable
class ConfigStateSource(Protocol):
    """The single ConfigState capability the extract layer needs.

    Extract functions never call Assets listing or auth helpers — only
    ``retrieve``. ``PlatformOneClient`` satisfies this structurally.
    """

    def retrieve(
        self,
        table: str,
        filters: dict | None = None,
    ) -> Iterator[dict]:
        """Yield ConfigState rows for ``retrieve-<table>`` with optional filters."""
        ...
