"""
Live exchange rates, cached, with the administrator's own rate as the floor under them.

Three rules shape this module, and each exists because of a specific way live FX breaks a
fee system.

**A live rate never re-prices an issued bill.** Rates are resolved when a breakdown is
computed and *frozen* onto an invoice the moment it is issued. Without that, a parent who
opens their bill on Tuesday and pays on Thursday is asked for a different number, and the
school cannot explain why. Freezing happens in `app.services.billing`; this module's job is
only to supply today's number honestly.

**A failed fetch must not fail a fee screen.** The provider is a third party over the public
internet, and a fee page that 500s because a currency API is down is far worse than one
showing yesterday's rate. Every failure path here returns the last good value, or the
administrator's manually configured rate, and says which it used. The caller always gets a
number.

**Rates are cached, not fetched per request.** A fee page hit by thirty parents at once must
not make thirty outbound calls - that is slow, it gets the deployment rate-limited, and it
turns a dependency's bad afternoon into an outage. One fetch per `FX_CACHE_MINUTES` serves
everybody, and a stale entry is still served while a refresh fails.

The provider is open.er-api.com, chosen because it needs no API key: a school deploying this
should not have to sign up for a currency service before their fee page works. It is
configurable, and `fetch_rates` deliberately accepts any provider that returns a
`{"rates": {"AED": 0.044}}`-shaped document, which most of them do.
"""

import logging
import threading
from datetime import datetime, timedelta
from typing import Any

from app.core.config import settings

logger = logging.getLogger("fx")

# One cache for the process, guarded because the fee page is served from a thread pool and
# two simultaneous misses would otherwise both fetch.
_lock = threading.Lock()
_cache: dict[str, dict[str, Any]] = {}


def _cache_key(base: str) -> str:
    return str(base).upper()


def _is_fresh(entry: dict, ttl_minutes: int) -> bool:
    fetched = entry.get("fetched_at")
    if not isinstance(fetched, datetime):
        return False
    return datetime.utcnow() - fetched < timedelta(minutes=max(int(ttl_minutes), 1))


def fetch_rates(base: str, timeout: float | None = None) -> dict[str, float]:
    """
    One call to the provider. Raises on any failure; callers are expected to catch.

    Kept separate from `rates_for` so the caching, fallback and error handling are readable
    on their own, and so a test can exercise the parsing without a network.
    """
    import httpx

    url = settings.FINANCE_FX_PROVIDER_URL.rstrip("/") + f"/{str(base).upper()}"
    timeout = timeout if timeout is not None else settings.FINANCE_FX_TIMEOUT_SECONDS

    response = httpx.get(url, timeout=timeout)
    response.raise_for_status()
    payload = response.json()

    rates = payload.get("rates") or payload.get("conversion_rates")
    if not isinstance(rates, dict):
        raise ValueError(
            f"Rate provider returned no 'rates' object (keys: {sorted(payload)[:6]})"
        )

    cleaned: dict[str, float] = {}
    for code, value in rates.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            cleaned[str(code).upper()] = number

    if not cleaned:
        raise ValueError("Rate provider returned no usable rates")
    return cleaned


def rates_for(base: str, force: bool = False) -> dict[str, Any]:
    """
    Today's rates against a base currency, from cache where possible.

    Returns a result envelope rather than a bare dict, because the caller needs to know
    whether the number is live, cached or stale before it puts "live rate" on a page. Never
    raises: a total failure comes back as `{"rates": {}, "status": "unavailable", ...}` and
    the caller falls through to its configured manual rates.
    """
    key = _cache_key(base)

    if not settings.FINANCE_FX_ENABLED:
        return {"base": key, "rates": {}, "status": "disabled",
                "fetched_at": None, "detail": "Live rates are turned off."}

    with _lock:
        entry = _cache.get(key)
        if entry and not force and _is_fresh(entry, settings.FINANCE_FX_CACHE_MINUTES):
            return {
                "base": key,
                "rates": dict(entry["rates"]),
                "status": "cached",
                "fetched_at": entry["fetched_at"].isoformat(),
                "detail": "Served from cache.",
            }

    try:
        fetched = fetch_rates(key)
    except Exception as exc:
        # A stale cache beats no answer. This is the path a provider outage takes, and it is
        # why the fee page keeps working through one.
        with _lock:
            entry = _cache.get(key)
        if entry:
            logger.warning("Live rate refresh failed (%s); serving the cached rates.", exc)
            return {
                "base": key,
                "rates": dict(entry["rates"]),
                "status": "stale",
                "fetched_at": entry["fetched_at"].isoformat(),
                "detail": f"Refresh failed, using rates from {entry['fetched_at']}: {exc}",
            }

        logger.warning("Live rate fetch failed and nothing is cached (%s).", exc)
        return {
            "base": key, "rates": {}, "status": "unavailable", "fetched_at": None,
            "detail": f"Could not reach the rate provider: {exc}",
        }

    now = datetime.utcnow()
    with _lock:
        _cache[key] = {"rates": fetched, "fetched_at": now}

    logger.info("Fetched %s live rates against %s.", len(fetched), key)
    return {
        "base": key,
        "rates": fetched,
        "status": "live",
        "fetched_at": now.isoformat(),
        "detail": f"Fetched {len(fetched)} rates from the provider.",
    }


def rate_for(base: str, quote: str, force: bool = False) -> dict[str, Any]:
    """
    One pair, as a number plus how it was obtained.

    A base-to-base rate is 1 without consulting anybody - it is a definition, not a
    measurement, and asking a provider for it is a round trip to be told what we already
    know.
    """
    base_code, quote_code = str(base).upper(), str(quote).upper()

    if base_code == quote_code:
        return {"base": base_code, "quote": quote_code, "rate": 1.0,
                "status": "base", "fetched_at": None,
                "detail": "The base currency is 1 by definition."}

    result = rates_for(base_code, force=force)
    rate = result["rates"].get(quote_code)

    if not rate:
        return {
            "base": base_code, "quote": quote_code, "rate": None,
            "status": result["status"] if result["status"] != "live" else "missing",
            "fetched_at": result.get("fetched_at"),
            "detail": (
                result["detail"] if result["status"] in {"unavailable", "disabled"}
                else f"The provider returned no rate for {quote_code}."
            ),
        }

    return {
        "base": base_code, "quote": quote_code, "rate": float(rate),
        "status": result["status"], "fetched_at": result.get("fetched_at"),
        "detail": result["detail"],
    }


def clear_cache() -> None:
    """Drops every cached rate. Used by the admin refresh endpoint and by tests."""
    with _lock:
        _cache.clear()


def cache_status() -> dict:
    """What the cache currently holds, for the admin's rates screen."""
    with _lock:
        return {
            "enabled": settings.FINANCE_FX_ENABLED,
            "provider": settings.FINANCE_FX_PROVIDER_URL,
            "cache_minutes": settings.FINANCE_FX_CACHE_MINUTES,
            "entries": [
                {
                    "base": base,
                    "currencies": len(entry["rates"]),
                    "fetched_at": entry["fetched_at"].isoformat(),
                    "is_fresh": _is_fresh(entry, settings.FINANCE_FX_CACHE_MINUTES),
                }
                for base, entry in _cache.items()
            ],
        }
