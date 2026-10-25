import logging
import os
from time import monotonic
from typing import Final

from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

logger = logging.getLogger(__name__)
TTL_SECONDS: Final = 60
MAX_ENTRY_BYTES: Final = 2 * 1024 * 1024


class ItemCache:
    def __init__(self) -> None:
        url = os.environ.get('NARA_WEB_REDIS_URL')
        self.client = Redis.from_url(url, socket_connect_timeout=0.2, socket_timeout=0.2,
            retry=Retry(NoBackoff(), 0), max_connections=8) if url else None
        self.retry_at = 0.0

    def get(self, key: str) -> bytes | None:
        if self.client is None or monotonic() < self.retry_at:
            return None
        try:
            value = self.client.get(key)
            return value if isinstance(value, bytes) and len(value) <= MAX_ENTRY_BYTES else None
        except RedisError:
            self._unavailable()
            return None

    def put(self, key: str, value: bytes) -> None:
        if self.client is None or monotonic() < self.retry_at or len(value) > MAX_ENTRY_BYTES:
            return
        try:
            self.client.set(key, value, ex=TTL_SECONDS)
        except RedisError:
            self._unavailable()

    def _unavailable(self) -> None:
        self.retry_at = monotonic() + 5
        logger.warning('item_cache_unavailable; serving job files')

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
