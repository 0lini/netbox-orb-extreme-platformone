"""HTTPS URL helper tests."""

from __future__ import annotations

import pytest

from orb_extreme_platformone.urls import require_https_url


@pytest.mark.parametrize(
    "url",
    [
        "https://cloudapi.extremecloudiq.com",
        "https://cloudapi.extremecloudiq.com/",
        " https://netbox.example.com/ ",
        "https://netbox.example.com/netbox",
        "https://netbox.example.com:443",
    ],
)
def test_require_https_url_accepts_https_hosts(url) -> None:
    cleaned = require_https_url(url, what="TEST_URL")
    assert cleaned.startswith("https://")
    assert not cleaned.endswith("/")


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost",
        "https://localhost:8000",
        "https://127.0.0.1:8000",
        "https://[::1]:8000",
        "https://netbox.local",
        "https://netbox:8080",
        " https://localhost:8000/ ",
    ],
)
def test_require_https_url_accepts_https_for_local_hosts(url) -> None:
    cleaned = require_https_url(url, what="TEST_URL")
    assert cleaned.startswith("https://")
    assert not cleaned.endswith("/")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://netbox.example.com",
        "http://evil.example.com",
        "http://metadata",
        "http://kubernetes",
        "ftp://netbox.example.com",
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
        "http://netbox.local",
        "http://netbox:8080",
        "https://",
        "not-a-url",
        "https://cloudapi.extremecloudiq.com@evil.com",
        "https://user:pass@cloudapi.extremecloudiq.com",
        "http://user@localhost:8000",
        "https://netbox.example.com?x=1",
        "https://netbox.example.com#frag",
    ],
)
def test_require_https_url_rejects_non_https_userinfo_or_hostless(url) -> None:
    with pytest.raises(ValueError, match="TEST_URL"):
        require_https_url(url, what="TEST_URL")
