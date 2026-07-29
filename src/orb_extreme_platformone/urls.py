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
    host_msg = f"{what} must be an https:// URL with a host"
    if not parsed.netloc:
        raise ValueError(host_msg)
    # urlparse derives .username/.password by splitting netloc on "@", so the
    # raw "@" test is the same condition — it alone rejects "https://legit@evil.com".
    if "@" in parsed.netloc:
        msg = f"{what} must not include userinfo (user:pass@host)"
        raise ValueError(msg)
    if parsed.query or parsed.fragment:
        msg = f"{what} must not include a query string or fragment"
        raise ValueError(msg)
    # netloc can be non-empty while hostname is empty ("https://:8080").
    if not parsed.hostname or parsed.scheme != "https":
        raise ValueError(host_msg)
    return cleaned
