"""
Disk-based cache wrapping diskcache.Cache.

Used to avoid re-calling API-Football (100 req/day free tier) on retries
or re-runs within the same week. Cache is keyed by a string and has a
configurable TTL defaulting to 7 days.

Usage:
    cache = get_cache()
    data = cache.get("api_football_fixture_123")
    if data is None:
        data = fetch_from_api(...)
        cache.set("api_football_fixture_123", data)
"""

import diskcache
from functools import wraps
from typing import Any, Callable, Optional

from config import CACHE_DIR, CACHE_TTL_SECONDS
from utils.logger import get_logger

logger = get_logger(__name__)

_cache: Optional[diskcache.Cache] = None


def get_cache() -> diskcache.Cache:
    global _cache
    if _cache is None:
        _cache = diskcache.Cache(str(CACHE_DIR))
        logger.debug("Disk cache opened at %s", CACHE_DIR)
    return _cache


def cached(key_fn: Callable[..., str], ttl: int = CACHE_TTL_SECONDS):
    """
    Decorator that caches a function's return value to disk.

    key_fn receives the same args/kwargs as the decorated function and
    must return a string cache key.

    Example:
        @cached(lambda fixture_id: f"fixture_{fixture_id}")
        def fetch_fixture(fixture_id: int) -> dict: ...
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            key = key_fn(*args, **kwargs)
            store = get_cache()
            value = store.get(key)
            if value is not None:
                logger.debug("Cache HIT: %s", key)
                return value
            logger.debug("Cache MISS: %s — calling %s", key, func.__name__)
            value = func(*args, **kwargs)
            if value is not None:
                store.set(key, value, expire=ttl)
            return value
        return wrapper
    return decorator


def bust(key: str) -> None:
    """Remove a single key from the cache."""
    get_cache().delete(key)
    logger.debug("Cache busted: %s", key)


def clear_all() -> None:
    """Wipe the entire cache. Use with caution — costs API calls."""
    get_cache().clear()
    logger.warning("Entire cache cleared")
