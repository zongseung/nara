import json
from _thread import LockType
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import KW_ONLY, dataclass, field
from decimal import Decimal
from email.utils import parsedate_to_datetime
from math import isfinite
from random import random
from threading import Lock
from time import monotonic, sleep, time

import requests
from requests.adapters import HTTPAdapter

from core.catalog_sources import JsonValue, SourceError


@dataclass(frozen=True, slots=True)
class JsonResponse:
    body: JsonValue
    text: str
    attempts: int


def post_json(url: str, payload: Mapping[str, Mapping[str, str | int]], *, limiter: "RequestLimiter",
              session: requests.Session | None = None, headers: Mapping[str, str] | None = None,
              timeout: float | tuple[float, float] = (10, 35)) -> JsonResponse:
    with nullcontext(session) if session is not None else requests.Session() as client:
        for adapter in client.adapters.values():
            if not isinstance(adapter, HTTPAdapter):
                raise SourceError('unsupported_adapter', 'session')
            if adapter.max_retries.total not in (0, False):
                raise SourceError('nested_retries', 'session')
        for attempt in range(1, 4):
            limiter.before_request()
            try:
                with client.post(url, json=payload, headers=headers, timeout=timeout, allow_redirects=False) as response:
                    if response.status_code != 200:
                        if (response.status_code == 429 or response.status_code >= 500) and attempt < 3:
                            limiter.backoff(attempt, response.headers.get('Retry-After'))
                            continue
                        raise SourceError('http_status', attempts=attempt, status=response.status_code)
                    try:
                        body = json.loads(response.text, parse_float=Decimal)
                    except ValueError as exc:
                        raise SourceError('invalid_json', attempts=attempt, status=200) from exc
                    return JsonResponse(body, response.text, attempt)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == 3:
                    code = 'timeout' if isinstance(exc, requests.Timeout) else 'connection_error'
                    raise SourceError(code, attempts=attempt) from exc
                limiter.backoff(attempt)
            except requests.RequestException as exc:
                raise SourceError('transport_error', attempts=attempt) from exc
    raise AssertionError('bounded request loop must return or raise')


@dataclass(slots=True)
class RequestLimiter:
    rate: float = 1.0
    _: KW_ONLY
    clock: Callable[[], float] = monotonic
    sleep: Callable[[float], None] = sleep
    wall_clock: Callable[[], float] = time
    jitter: Callable[[], float] = random
    _lock: LockType = field(default_factory=Lock, init=False, repr=False)
    _next: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        if not isfinite(self.rate) or self.rate <= 0:
            raise SourceError('invalid_rate', 'rate')

    def before_request(self) -> None:
        with self._lock:
            delay = max(0.0, self._next - self.clock())
            if delay:
                self.sleep(delay)
            self._next = self.clock() + 1.0 / self.rate

    def backoff(self, attempt: int, retry_after: str | None = None) -> None:
        delay = (1.0 if attempt == 1 else 3.0) + min(1.0, max(0.0, self.jitter()))
        if retry_after:
            try:
                if retry_after.strip().isascii() and retry_after.strip().isdigit():
                    requested = float(int(retry_after.strip()))
                else:
                    requested = parsedate_to_datetime(retry_after).timestamp() - self.wall_clock()
            except (ValueError, TypeError, OverflowError):
                requested = 0.0
            delay = max(delay, requested)
        self.sleep(min(120.0, delay))
