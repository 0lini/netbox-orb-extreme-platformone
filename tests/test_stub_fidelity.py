"""Guards that the Diode SDK test doubles stay faithful to the real classes.

The transform suites run entirely against stubs. If a stub accepts a kwarg the
real protobuf-backed class rejects, those suites go green while production
fails, so the doubles themselves need covering.
"""

from __future__ import annotations

import inspect

import pytest
from netboxlabs.diode.sdk import ingester

from .conftest import STUB_CLASSES, _transform_modules


@pytest.mark.parametrize("name", sorted(STUB_CLASSES))
def test_stub_rejects_kwargs_the_real_class_rejects(name: str) -> None:
    with pytest.raises(TypeError, match="the real Diode class rejects"):
        STUB_CLASSES[name](definitely_not_a_diode_field="x")


@pytest.mark.parametrize("name", sorted(STUB_CLASSES))
def test_stub_accepts_every_real_constructor_kwarg(name: str) -> None:
    """A stub must not be stricter than the real class either."""
    real_params = set(inspect.signature(getattr(ingester, name)).parameters)
    accepted = STUB_CLASSES[name](**dict.fromkeys(real_params))
    assert set(accepted._kw) == real_params


def test_every_imported_sdk_class_has_a_stub() -> None:
    """A transform module importing an unstubbed Diode class would run unstubbed."""
    sdk_names = {n for n in dir(ingester) if n[:1].isupper()}
    for mod in _transform_modules():
        imported = {n for n in mod.__dict__ if n in sdk_names}
        missing = imported - set(STUB_CLASSES)
        assert not missing, f"{mod.__name__} imports unstubbed Diode classes: {sorted(missing)}"
