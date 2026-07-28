"""Extreme Platform ONE discovery worker for the NetBox Labs Orb Agent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._version import __version__

if TYPE_CHECKING:
    from .backend import Backend as Backend

__all__ = ["Backend", "__version__"]


def __getattr__(name: str):
    # Orb's load_class() importlib-loads this package and inspect.getmembers()
    # for a Backend subclass — keep the import lazy so `import
    # orb_extreme_platformone` / version metadata does not require the SDKs.
    if name == "Backend":
        from .backend import Backend

        return Backend
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
