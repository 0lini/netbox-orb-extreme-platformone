"""Authenticated POST transport for Platform ONE: credentials, sessions, errors.

Owns the concerns that change for auth or transport reasons — token lifecycle,
thread-local sessions, retry/backoff, and mapping HTTP failures onto
``PlatformOneApiError``. Pagination and filter chunking live one layer up in
:mod:`orb_extreme_platformone.client`, which changes for API-shape reasons.

Every Platform ONE read is a POST (the Assets and ConfigState APIs both take
filter bodies), so the retry policy must opt POST in explicitly — urllib3
excludes non-idempotent methods by default. Retrying is safe here: the calls
are reads despite the verb, and ``/login`` is idempotent in effect.
"""

from __future__ import annotations

import logging
import re
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .urls import require_https_url

DEFAULT_BASE_URL = "https://cloudapi.extremecloudiq.com"
DEFAULT_TIMEOUT_SECONDS = 60.0

# Concurrency cap for ConfigState fan-out; connection pools are sized to match
# so worker threads never queue waiting for a socket.
MAX_CONCURRENT_REQUESTS = 8

# Keep API error text short so logs/exceptions do not retain full upstream
# bodies (which can include sensitive diagnostics).
_ERROR_BODY_LIMIT = 200
# Refresh a minute early so a request never races token expiry.
_TOKEN_REFRESH_SKEW_SECONDS = 60
_DEFAULT_TOKEN_TTL_SECONDS = 86400

_ELLIPSIS = "..."
_REDACTED = "[REDACTED]"
# Some gateways echo the request body back in an error response. Redact the
# secret-bearing JSON fields before that text reaches an exception or a log.
_SECRET_FIELD_RE = re.compile(
    r'("(?:password|client_secret|access_token|refresh_token|api_token|authorization)"\s*:\s*)"[^"]*"',
    re.IGNORECASE,
)

# Retry transient upstream failures in-tick. Without this a single 503 costs a
# whole ConfigState table until the next scheduled run.
RETRY_TOTAL = 3
RETRY_BACKOFF_FACTOR = 0.5  # 0.5s, 1s, 2s (urllib3 adds jitter)
RETRY_STATUSES = (429, 500, 502, 503, 504)

_HTTP_REDIRECT_MIN = 300
_HTTP_CLIENT_ERROR_MIN = 400
_AUTH_FAILURE_STATUSES = (401, 403)
_UNAUTHORIZED = 401

logger = logging.getLogger(__name__)


class ApiError(RuntimeError):
    """Base for upstream API failures, with the status code kept structured.

    ``status_code`` is the HTTP status when the failure came from a response, or
    ``None`` for transport failures and malformed bodies. Callers branch on the
    ``is_*`` properties rather than parsing the message text.
    """

    upstream = "API"

    def __init__(self, message: str, *, status_code: int | None = None, path: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.path = path

    @property
    def is_auth_failure(self) -> bool:
        """Credentials are wrong or revoked — retrying will not help."""
        return self.status_code in _AUTH_FAILURE_STATUSES

    @property
    def is_not_found(self) -> bool:
        """Endpoint or resource absent — permanent for the tick."""
        return self.status_code == requests.codes.not_found

    @property
    def is_transient(self) -> bool:
        """Worth retrying: rate limit, gateway error, or a transport failure."""
        return self.status_code is None or self.status_code in RETRY_STATUSES


class PlatformOneApiError(ApiError):
    """Raised on a failed Platform ONE API call."""

    upstream = "Platform ONE"


def raise_for_response(resp: requests.Response, *, path: str, error: type[ApiError]) -> None:
    """Raise ``error`` for a redirect or >=400 response; return otherwise.

    Redirects fail closed rather than being followed: neither upstream should
    ever move our bearer token or API token to another origin.
    """
    if _HTTP_REDIRECT_MIN <= resp.status_code < _HTTP_CLIENT_ERROR_MIN:
        msg = f"{error.upstream} unexpected redirect {resp.status_code} for {path}"
        raise error(msg, status_code=resp.status_code, path=path)
    if resp.status_code >= _HTTP_CLIENT_ERROR_MIN:
        detail = truncate_error_body(resp.text)
        msg = f"{error.upstream} API error {resp.status_code} for {path}: {detail}"
        raise error(msg, status_code=resp.status_code, path=path)


def truncate_error_body(text: str, *, limit: int = _ERROR_BODY_LIMIT) -> str:
    """Collapse whitespace, redact echoed secrets, and truncate for safe logging.

    Truncation alone bounds length but not content: an upstream that echoes the
    request body in an error response would otherwise put the login password
    into the exception message and every log line that formats it.
    """
    cleaned = " ".join((text or "").split())
    cleaned = _SECRET_FIELD_RE.sub(rf'\1"{_REDACTED}"', cleaned)
    if len(cleaned) <= limit:
        return cleaned
    if limit <= len(_ELLIPSIS):
        return cleaned[:limit]
    return cleaned[: limit - len(_ELLIPSIS)] + _ELLIPSIS


def _retry_policy() -> Retry:
    return Retry(
        total=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF_FACTOR,
        status_forcelist=RETRY_STATUSES,
        # Every Platform ONE read is a POST; urllib3 would otherwise retry none.
        allowed_methods=frozenset({"POST"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )


class PlatformOneTransport:
    """Authenticated POST transport against one Platform ONE host.

    HTTP sessions are thread-local so independent ConfigState retrieves can run
    concurrently without sharing a ``requests.Session``. Token state is guarded
    by a lock so password-login refresh is safe across those threads.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not api_token and not (username and password):
            msg = "PlatformOneClient requires api_token or username/password"
            raise ValueError(msg)
        self.base_url = require_https_url(base_url, what="PLATFORMONE_API_URL")
        self._username = username
        self._password = password
        self._timeout = timeout
        self._local = threading.local()
        self._lock = threading.Lock()
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if api_token:
            self._headers["Authorization"] = f"Bearer {api_token}"
            # None = static API token (never expires from our side) until a
            # login response sets it; password mode starts expired so the
            # first request logs in.
            self._token_expiry: float | None = None
        else:
            self._token_expiry = 0.0

    # -- session / auth ----------------------------------------------------

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = HTTPAdapter(
                pool_connections=MAX_CONCURRENT_REQUESTS,
                pool_maxsize=MAX_CONCURRENT_REQUESTS,
                max_retries=_retry_policy(),
            )
            session.mount("https://", adapter)
            self._local.session = session
        return session

    def close(self) -> None:
        """Close this thread's HTTP session. Safe to call more than once."""
        session = getattr(self._local, "session", None)
        if session is not None:
            session.close()
            self._local.session = None

    def _ensure_token_locked(self) -> None:
        if self._token_expiry is None:
            return
        if time.time() < self._token_expiry:
            return
        if self._username and self._password:
            self._login_locked()
            return
        msg = "No credentials available to authenticate with Platform ONE"
        raise PlatformOneApiError(msg)

    def _login_locked(self) -> None:
        url = f"{self.base_url}/login"
        payload = {"username": self._username, "password": self._password}
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        # Fail closed on redirects so a 307 cannot replay the password body.
        resp = self._session().post(
            url,
            headers=headers,
            json=payload,
            timeout=self._timeout,
            allow_redirects=False,
        )
        raise_for_response(resp, path="/login", error=PlatformOneApiError)
        data = resp.json()
        access_token = data.get("access_token")
        if not access_token:
            msg = "Platform ONE login response did not contain an access_token"
            raise PlatformOneApiError(msg, path="/login")
        self._headers["Authorization"] = f"Bearer {access_token}"
        self._token_expiry = (
            time.time() + data.get("expires_in", _DEFAULT_TOKEN_TTL_SECONDS) - _TOKEN_REFRESH_SKEW_SECONDS
        )

    def _auth_headers(self) -> dict:
        with self._lock:
            self._ensure_token_locked()
            return dict(self._headers)

    # -- request -----------------------------------------------------------

    def post(self, path: str, params: dict, body: dict) -> dict:
        """POST `path`, re-logging in once on a 401 when using username/password.

        Transport failures and invalid JSON are raised as ``PlatformOneApiError``
        so ConfigState fan-out can degrade a single table instead of aborting
        the tick on a timeout/connection blip.
        """
        url = f"{self.base_url}{path}"
        for attempt in (1, 2):
            headers = self._auth_headers()
            try:
                resp = self._session().post(
                    url,
                    headers=headers,
                    params=params,
                    json=body,
                    timeout=self._timeout,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                msg = f"Platform ONE API request failed for {path}: {exc}"
                raise PlatformOneApiError(msg, path=path) from exc
            if resp.status_code == _UNAUTHORIZED and attempt == 1 and self._username and self._password:
                with self._lock:
                    self._token_expiry = 0.0
                    self._login_locked()
                continue
            raise_for_response(resp, path=path, error=PlatformOneApiError)
            try:
                payload = resp.json()
            except ValueError as exc:
                msg = f"Platform ONE API returned invalid JSON for {path}: {exc}"
                raise PlatformOneApiError(msg, path=path) from exc
            if not isinstance(payload, dict):
                msg = f"Platform ONE API returned non-object JSON for {path}: {type(payload).__name__}"
                raise PlatformOneApiError(msg, path=path)
            return payload
        msg = "unreachable"
        raise AssertionError(msg)  # pragma: no cover
