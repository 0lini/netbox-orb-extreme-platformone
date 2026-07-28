"""URL validation helpers for outbound API clients."""

from __future__ import annotations

from urllib.parse import urlparse


def require_https_url(url: str, *, what: str) -> str:
    """Return a cleaned base URL safe for sending API tokens.

    Requires ``https://`` with a host.

    Rejects userinfo (``user:pass@host`` / ``legit@evil``) so credentials
    cannot be redirected to an attacker-controlled host via URL confusion.
    Rejects query strings and fragments. Path is preserved (NetBox may be
    mounted under a subpath) and trailing slashes are stripped.

    Raises ``ValueError`` for empty, hostless, userinfo-bearing, or
    non-https values so tokens are never sent to an unencrypted endpoint.
    """
    cleaned = (url or "").strip().rstrip("/")
    parsed = urlparse(cleaned)
    if not parsed.netloc:
        msg = f"{what} must be an https:// URL with a host"
        raise ValueError(msg)
    # urlparse puts userinfo in .username/.password; also reject raw "@"
    # in netloc so "https://legit@evil.com" cannot slip through.
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        msg = f"{what} must not include userinfo (user:pass@host)"
        raise ValueError(msg)
    if parsed.query or parsed.fragment:
        msg = f"{what} must not include a query string or fragment"
        raise ValueError(msg)
    hostname = parsed.hostname
    if not hostname:
        msg = f"{what} must be an https:// URL with a host"
        raise ValueError(msg)

    if parsed.scheme == "https":
        return cleaned
    msg = f"{what} must be an https:// URL with a host"
    raise ValueError(msg)
