import pytest

from jumia_feed_sync import resolve


@pytest.fixture(autouse=True)
def _reset_catalog_cache():
    """resolve.py caches id_label_catalog in-process per kind (real fix
    for a ~1.7s-per-request SQLite re-fetch, see resolve.py). That cache
    is keyed only by kind, not by database -- fine in production (one
    dashboard process, one database), but tests use many different
    databases in the same process, so a stale cache from one test's
    catalog would leak into the next test's assertions. Clear it before
    every test."""
    resolve._catalog_cache.clear()
    yield
    resolve._catalog_cache.clear()
