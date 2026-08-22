"""Rate-limited HTTP helper for llm-wiki fetchers.

Every fetcher/extractor/describer HTTP call goes through this module. It gives:

- A token-bucket limiter (sustained rps, burst) shared across a run
- Automatic retries on 429/503 that respect the Retry-After header
- Exponential backoff with jitter for retryable errors when no Retry-After is set
- Optional circuit breaker for callers that iterate many requests

`get_limiter(cfg_section)` returns a cached `RateLimiter` per config section
(atlassian, nano_banana) so repeated calls within one process share the same
token bucket.

Stdlib + requests only. requests is already required by fetch_*.py so no new
dependency is introduced.
"""

from __future__ import annotations

import email.utils
import random
import threading
import time
from typing import Callable, Dict, Optional

try:
    import requests
    from requests.exceptions import ConnectionError as RequestsConnectionError
    from requests.exceptions import Timeout as RequestsTimeout
except ImportError:  # pragma: no cover
    # rate_limiter.py may be imported by callers that never actually make HTTP
    # calls (e.g. ingest.py orchestrator). Defer the hard failure until the
    # first .request() call.
    requests = None  # type: ignore[assignment]
    RequestsConnectionError = Exception  # type: ignore[assignment]
    RequestsTimeout = Exception  # type: ignore[assignment]


DEFAULTS = {
    "rate_limit_rps": 2.0,
    "burst": 5,
    "max_retries": 5,
    "retry_base_delay_seconds": 2.0,
}


class RateLimitFailure(Exception):
    """Raised when max_retries is exceeded on a single request."""

    def __init__(self, url: str, status_code: Optional[int], attempts: int, last_error: Optional[str]):
        self.url = url
        self.status_code = status_code
        self.attempts = attempts
        self.last_error = last_error
        detail = f" (last HTTP {status_code})" if status_code else ""
        if last_error:
            detail += f" — {last_error}"
        super().__init__(
            f"rate-limit / retry budget exhausted for {url} after {attempts} attempts{detail}"
        )


class RateLimiter:
    """Simple thread-safe token-bucket + retrying HTTP helper.

    Not designed for extreme concurrency — one process, low-single-digit rps
    is the target. Uses a monotonic clock and a lock to hand out tokens.
    """

    # Cap for the manual redirect loop (requests' own default is 30).
    MAX_MANUAL_REDIRECTS = 10

    def __init__(
        self,
        *,
        rps: float,
        burst: int,
        max_retries: int,
        retry_base: float,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
    ):
        if rps <= 0:
            raise ValueError("rps must be > 0")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self.rps = float(rps)
        self.burst = int(burst)
        self.max_retries = int(max_retries)
        self.retry_base = float(retry_base)
        self._sleep = sleep_fn
        self._clock = clock_fn
        self._lock = threading.Lock()
        self._tokens = float(burst)
        self._last_refill = clock_fn()

    def throttle(self) -> None:
        """Block until a token is available; use before making an HTTP call
        via a client that we don't control (e.g. google-genai)."""
        self._acquire()

    def _acquire(self) -> None:
        """Block until at least one token is available, then consume it."""
        while True:
            with self._lock:
                now = self._clock()
                elapsed = max(0.0, now - self._last_refill)
                self._tokens = min(self.burst, self._tokens + elapsed * self.rps)
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                needed = 1.0 - self._tokens
                wait = needed / self.rps
            self._sleep(max(wait, 0.01))

    def request(
        self,
        method: str,
        url: str,
        *,
        session: Optional["requests.Session"] = None,
        follow_redirects_preserving_cookie: bool = False,
        **kwargs,
    ) -> "requests.Response":
        """Execute an HTTP request, honoring rate limits and retrying transient failures.

        Retryable conditions:
          - HTTP 429 / 503 → sleep Retry-After (or exponential backoff)
          - Connection errors / timeouts → exponential backoff

        Non-retryable conditions (returned to caller):
          - Any 2xx / 3xx response
          - 4xx other than 429 (auth, not-found, etc — caller handles)
          - 5xx other than 503

        Raises RateLimitFailure when max_retries is exhausted.

        `follow_redirects_preserving_cookie` works around a requests behavior:
        Session.resolve_redirects() unconditionally does headers.pop("Cookie")
        on every hop and only re-adds cookies from the Session's own jar. We
        pass no Session, so a header-supplied Cookie is silently dropped the
        moment a URL redirects — including the near-universal http→https
        upgrade. When this flag is set and a Cookie header is present, redirects
        are followed manually so the Cookie survives *same-origin* hops (and is
        dropped cross-origin, mirroring what requests already does correctly for
        Authorization). No-op when no Cookie header is present.
        """
        if requests is None:
            raise RuntimeError(
                "requests package is not installed. Run install.sh to install "
                "the llm-wiki plugin's Python dependencies."
            )

        headers = kwargs.get("headers") or {}
        has_cookie = any(k.lower() == "cookie" for k in headers)
        if (
            follow_redirects_preserving_cookie
            and has_cookie
            and kwargs.get("allow_redirects", True)
        ):
            return self._request_manual_redirects(method, url, session, **kwargs)

        return self._request_once(method, url, session, **kwargs)

    def _request_once(
        self,
        method: str,
        url: str,
        session: Optional["requests.Session"],
        **kwargs,
    ) -> "requests.Response":
        sess = session or requests
        attempt = 0
        last_status: Optional[int] = None
        last_error: Optional[str] = None

        while True:
            self._acquire()
            attempt += 1
            try:
                resp = sess.request(method, url, **kwargs)
            except (RequestsConnectionError, RequestsTimeout) as e:  # pragma: no cover
                last_error = f"{type(e).__name__}: {e}"
                last_status = None
                if attempt > self.max_retries:
                    raise RateLimitFailure(url, last_status, attempt, last_error)
                self._sleep(self._backoff_seconds(attempt))
                continue

            if resp.status_code not in (429, 503):
                return resp

            last_status = resp.status_code
            last_error = f"HTTP {resp.status_code}"
            if attempt > self.max_retries:
                raise RateLimitFailure(url, last_status, attempt, last_error)

            wait = self._retry_after_seconds(resp) or self._backoff_seconds(attempt)
            self._sleep(wait)

    def _request_manual_redirects(
        self,
        method: str,
        url: str,
        session: Optional["requests.Session"],
        **kwargs,
    ) -> "requests.Response":
        """Follow redirects by hand so a header-supplied Cookie survives them.

        Only ever used for GET by our callers, so there's no method-rewriting
        (303 → GET) subtlety to handle: we keep the method as given.
        """
        from urllib.parse import urljoin, urlparse

        def origin(u: str) -> tuple:
            p = urlparse(u)
            return (p.scheme.lower(), p.netloc.lower())

        kwargs = dict(kwargs)
        kwargs["allow_redirects"] = False
        headers = dict(kwargs.get("headers") or {})
        kwargs["headers"] = headers

        current = url
        for _ in range(self.MAX_MANUAL_REDIRECTS):
            resp = self._request_once(method, current, session, **kwargs)
            if resp.status_code not in (301, 302, 303, 307, 308):
                return resp
            location = resp.headers.get("Location")
            if not location:
                return resp

            nxt = urljoin(current, location)
            if origin(nxt) != origin(current):
                # Cross-origin hop: drop the Cookie, exactly as requests does
                # for Authorization. Everything else carries over.
                for key in [k for k in headers if k.lower() == "cookie"]:
                    headers.pop(key)
                # Nothing left to preserve — hand the rest of the chain back to
                # requests so its own redirect semantics apply.
                kwargs["allow_redirects"] = True
                return self._request_once(method, nxt, session, **kwargs)
            current = nxt

        raise RateLimitFailure(
            url, None, self.MAX_MANUAL_REDIRECTS,
            f"exceeded {self.MAX_MANUAL_REDIRECTS} redirects",
        )

    def _retry_after_seconds(self, resp: "requests.Response") -> Optional[float]:
        header = resp.headers.get("Retry-After")
        if not header:
            return None
        header = header.strip()
        if header.isdigit():
            return float(header)
        try:
            when = email.utils.parsedate_to_datetime(header)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        delta = when.timestamp() - time.time()
        return max(delta, 0.0)

    def _backoff_seconds(self, attempt: int) -> float:
        # attempt is 1-indexed. Grow: base * 2^(attempt-1) + jitter in [0, base).
        base = self.retry_base * (2 ** (attempt - 1))
        return base + random.uniform(0.0, self.retry_base)


_CACHE: Dict[str, RateLimiter] = {}
_CACHE_LOCK = threading.Lock()


def _resolve(section: Optional[dict], key: str) -> float:
    if section is None:
        return DEFAULTS[key]  # type: ignore[return-value]
    value = section.get(key)
    if value is None:
        return DEFAULTS[key]  # type: ignore[return-value]
    return value


def get_limiter(section_name: str, section: Optional[dict]) -> RateLimiter:
    """Return a cached RateLimiter keyed by section name.

    section_name is a label like "atlassian" or "nano_banana".
    section is the dict from load_config(); may be None for tests.
    """
    with _CACHE_LOCK:
        cached = _CACHE.get(section_name)
        if cached is not None:
            return cached
        limiter = RateLimiter(
            rps=float(_resolve(section, "rate_limit_rps")),
            burst=int(_resolve(section, "burst")),
            max_retries=int(_resolve(section, "max_retries")),
            retry_base=float(_resolve(section, "retry_base_delay_seconds")),
        )
        _CACHE[section_name] = limiter
        return limiter


def reset_cache() -> None:
    """Clear cached limiters — used by tests."""
    with _CACHE_LOCK:
        _CACHE.clear()
