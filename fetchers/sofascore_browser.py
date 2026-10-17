"""
Headless-browser transport for Sofascore's data API.

Direct HTTP calls (requests / curl_cffi) get a 403 "challenge" from
api.sofascore.com. Loading www.sofascore.com in a real Chromium session and
calling fetch() from inside the page passes the check. GitHub runners are
IP-blocked regardless, so this only runs on a local machine — see
fetchers/sofascore_collector.py.

Playwright is imported lazily so the GitHub pipeline never needs it.
Setup (local only):  pip install playwright && python -m playwright install chromium
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Optional

from utils.logger import get_logger

logger = get_logger(__name__)

_HOME_URL = "https://www.sofascore.com/"
_SETTLE_MS = 6000          # let the page finish its challenge before calling the API
_API_PREFIX = "/api/v1/"

_FETCH_JS = """async (p) => {
    const r = await fetch(p, {headers: {'Accept': 'application/json'}});
    return {status: r.status, text: await r.text()};
}"""


@contextmanager
def browser_session() -> Iterator[Callable[[str], Optional[dict]]]:
    """
    Yield get_json(path) where path is relative to /api/v1/
    (e.g. "unique-tournament/18/seasons"). Returns parsed JSON or None.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(locale="en-GB")
            page.goto(_HOME_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(_SETTLE_MS)
            logger.info("Sofascore browser session ready (%s)", page.title())

            def get_json(path: str) -> Optional[dict]:
                try:
                    r = page.evaluate(_FETCH_JS, _API_PREFIX + path.lstrip("/"))
                except Exception as e:
                    logger.warning("Sofascore browser fetch failed on %s: %s", path, e)
                    return None
                status = r["status"]
                if status in (403, 429):
                    logger.warning("Sofascore %d on %s (browser) — stopping", status, path)
                    return None
                if status != 200:
                    logger.debug("Sofascore %d on %s (browser)", status, path)
                    return None
                return json.loads(r["text"])

            yield get_json
        finally:
            browser.close()
