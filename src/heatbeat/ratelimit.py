"""Rate limiting per tenant (token bucket).

What this file does: each tenant has a bucket of tokens that refills at a fixed
rate. Every request takes one token; an empty bucket means "429 Too Many
Requests" for that tenant only, so one noisy customer cannot slow down the
others (scenario S17, System Design Interview Vol. 1, ch. 4).

Limitation: the buckets live in this process's memory. With several API
instances the limit would move to Redis (same algorithm, shared counters).
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    def __init__(
        self,
        per_minute: int,
        burst: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate = per_minute / 60.0  # tokens per second
        self._capacity = float(burst or per_minute)
        self._clock = clock
        self._buckets: dict[object, _Bucket] = {}
        self._lock = threading.Lock()

    def acquire(self, key: object) -> float:
        """Take one token for `key`. Return 0 if allowed, else seconds until the next token."""
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = self._buckets[key] = _Bucket(self._capacity, now)
            bucket.tokens = min(self._capacity, bucket.tokens + (now - bucket.updated_at) * self._rate)
            bucket.updated_at = now
            if bucket.tokens >= 1:
                bucket.tokens -= 1
                return 0.0
            return (1 - bucket.tokens) / self._rate
