import asyncio
import time

import httpx

from app.core.errors import AppError


class CircuitBreaker:
    """Opens after N consecutive failures, then fails fast until the cool-down passes."""

    def __init__(self, threshold: int = 3, cooldown: float = 30.0, clock=time.monotonic):
        self.threshold, self.cooldown, self.clock = threshold, cooldown, clock
        self.failures = 0
        self.opened_at: float | None = None

    def check(self) -> None:
        if self.opened_at is not None:
            if self.clock() - self.opened_at < self.cooldown:
                raise AppError("PROVIDER_UNAVAILABLE", "The speech service is temporarily unavailable.")
            self.opened_at = None  # half-open: allow one probe
            self.failures = self.threshold - 1

    def success(self) -> None:
        self.failures, self.opened_at = 0, None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = self.clock()


async def send_with_retry(breaker: CircuitBreaker, send, *, retries: int = 1, backoff: float = 0.3) -> httpx.Response:
    """Retries only transport errors/5xx. Safe because STT and TTS calls have no side effects on the bank."""
    breaker.check()
    last: AppError | None = None
    for attempt in range(retries + 1):
        try:
            resp = await send()
            if resp.status_code >= 500:
                raise AppError("PROVIDER_UNAVAILABLE", "The speech service returned an error.")
            if resp.status_code == 429:
                breaker.failure()
                raise AppError("RATE_LIMITED", "The speech service is rate limiting requests.")
            if resp.status_code in (401, 403):
                breaker.failure()
                raise AppError("PROVIDER_UNAVAILABLE", "The speech service rejected the configured credentials.")
            if resp.status_code >= 400:
                raise AppError("PROVIDER_UNAVAILABLE", "The speech service could not process the request.")
            breaker.success()
            return resp
        except httpx.TimeoutException:
            last = AppError("PROVIDER_TIMEOUT", "The speech service timed out.")
        except httpx.TransportError:
            last = AppError("PROVIDER_UNAVAILABLE", "The speech service could not be reached.")
        except AppError as e:
            if e.code != "PROVIDER_UNAVAILABLE" or resp.status_code < 500:
                raise
            last = e
        if attempt < retries:
            await asyncio.sleep(backoff)
    breaker.failure()
    assert last is not None
    raise last
