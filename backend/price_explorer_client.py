"""Client for the existing "Price Explorer" API (a separate app, hosted at
PRICE_EXPLORER_BASE_URL) which reads real billed-purchase history from the
company's MS SQL ERP (tables GPurci/GPurc/party/PoRequesti) and computes
lowest/last/6-month-average price per supplier for a Product ID.

This is a much more reliable "who supplies Product X at what price" source
than SAP's "List Prices" (Procurement Price Specification) object, which
was found to be mostly unpopulated placeholder data for most parts - this
ERP data comes from actual billed purchase invoices.

Auth: POST /api/auth/login with {username, password} -> {token} (JWT valid
30 days). Token is cached in-memory and refreshed on 401. Switched back to
this username/password flow (Feb 2026) per user's explicit request with
rotated credentials - the earlier scoped API key is no longer used."""
import threading
import time

import requests


class PriceExplorerError(Exception):
    pass


class PriceExplorerClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._token = None
        self._token_fetched_at = 0
        self._lock = threading.Lock()

    def _login(self) -> str:
        try:
            resp = requests.post(
                f"{self.base_url}/api/auth/login",
                json={"username": self.username, "password": self.password},
                timeout=(5, 15),
            )
        except requests.exceptions.RequestException as e:
            raise PriceExplorerError(f"Could not reach Price Explorer service: {e}")
        if resp.status_code != 200:
            raise PriceExplorerError(f"Price Explorer login failed (HTTP {resp.status_code}): {resp.text[:200]}")
        token = resp.json().get("token")
        if not token:
            raise PriceExplorerError("Price Explorer login did not return a token")
        return token

    def _get_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            # Refresh a day early to avoid edge-of-expiry failures (token valid 30 days).
            if force_refresh or self._token is None or (time.time() - self._token_fetched_at) > 29 * 24 * 3600:
                self._token = self._login()
                self._token_fetched_at = time.time()
            return self._token

    def search(self, query: str, lookback_days: int = 180, limit: int = 25) -> list:
        """Returns items: [{icode, iname, lowest, last, average}, ...] where
        lowest/last are {rate, supplier, pcode, bill_date} and average is
        {rate, bill_count}. Empty list if nothing matches - normal outcome.

        Timeouts are deliberately tight (connect=5s, read=15s per leg, worst
        case ~35-40s across login+search+retry) - the platform's own
        ingress has a gateway timeout well under a minute, so if we let a
        stalled upstream (this vendor's search endpoint has been observed
        hanging indefinitely, independent of auth method) run past that,
        the PLATFORM'S generic gateway-timeout page reaches the user instead
        of our own clear PriceExplorerError message - failing fast here is
        what lets that clear message actually get through."""
        token = self._get_token()
        try:
            resp = requests.get(
                f"{self.base_url}/api/price-explorer/search",
                params={"q": query, "lookback_days": lookback_days, "limit": limit},
                headers={"Authorization": f"Bearer {token}"},
                timeout=(5, 15),
            )
            if resp.status_code == 401:
                # token expired/invalid - refresh once and retry
                token = self._get_token(force_refresh=True)
                resp = requests.get(
                    f"{self.base_url}/api/price-explorer/search",
                    params={"q": query, "lookback_days": lookback_days, "limit": limit},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=(5, 15),
                )
        except requests.exceptions.RequestException as e:
            raise PriceExplorerError(f"Could not reach Price Explorer service: {e}")
        if resp.status_code != 200:
            raise PriceExplorerError(f"Price Explorer search failed (HTTP {resp.status_code}): {resp.text[:200]}")
        return resp.json().get("items", [])
