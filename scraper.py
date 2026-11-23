import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
from playwright.sync_api import sync_playwright

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
WP_WEBHOOK_URL = os.environ.get("WP_WEBHOOK_URL", "").strip()

SEEN_FILE = "seen.json"
OUTPUT_FILE = "news.json"
MAX_NEW_PER_LANG = 3
MAX_FEED_ITEMS = 45

VALID_CATEGORIES = [
    "tunisia",
    "world",
    "economy",
    "sports",
    "culture-tech",
    "society-health",
]

SOURCES = {
    "ar": {
        "name": "Mosaïque FM",
        "listings": [
            ("https://www.mosaiquefm.net/ar/actualites", "tunisia"),
            ("https://www.mosaiquefm.net/ar/actualites/%D8%A7%D9%84%D8%A3%D8%AE%D8%A8%D8%A7%D8%B1-%D8%A7%D9%84%D8%B9%D8%A7%D9%84%D9%85%D9%8A%D8%A9/3", "world"),
        ],
    },
    "fr": {
        "name": "France 24",
        "listings": [
            ("https://www.france24.com/fr/afrique/", "tunisia"),
            ("https://www.france24.com/fr/", "world"),
            ("https://www.france24.com/fr/sports/", "sports"),
            ("https://www.france24.com/fr/culture/", "culture-tech"),
        ],
    },
    "en": {
        "name": "France 24",
        "listings": [
            ("https://www.france24.com/en/africa/", "tunisia"),
            ("https://www.france24.com/en/europe/", "world"),
            ("https://www.france24.com/en/business/", "economy"),
            ("https://www.france24.com/en/sport/", "sports"),
            ("https://www.france24.com/en/culture/", "culture-tech"),
            ("https://www.france24.com/en/health/", "society-health"),
        ],
    },
}


class StealthFetcher:
    """Tier 1: curl_cffi Chrome TLS impersonation. Tier 2: Headless Chromium via Playwright."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._context = None

    def _ensure_browser(self):
        if self._browser is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.
