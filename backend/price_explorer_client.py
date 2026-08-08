"""Client for the existing "Price Explorer" API (a separate app, hosted at
PRICE_EXPLORER_BASE_URL) which reads real billed-purchase history from the
company's MS SQL ERP (tables GPurci/GPurc/party/PoRequesti) and computes
lowest/last/6-month-average price per supplier for a Product ID.

This is a much more reliable "who supplies Product X at what price" source
than SAP's "List Prices" (Procurement Price Specification) object, which
was found to be mostly unpopulated placeholder data for most parts - this
ERP data comes from actual billed purchase invoices.

Auth: static scoped API key sent as `X-Api-Key` header on every request
(no login/token flow - simpler and avoids using a human's personal
username/password, which is what this originally did before Session 13)."""
import requests


class PriceExplorerError(Exception):
    pass


class PriceExplorerClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def search(self, query: str, lookback_days: int = 180, limit: int = 25) -> list:
        """Returns items: [{icode, iname, lowest, last, average}, ...] where
        lowest/last are {rate, supplier, pcode, bill_date} and average is
        {rate, bill_count}. Empty list if nothing matches - normal outcome."""
        try:
            resp = requests.get(
                f"{self.base_url}/api/price-explorer/search",
                params={"q": query, "lookback_days": lookback_days, "limit": limit},
                headers={"X-Api-Key": self.api_key},
                timeout=20,
            )
        except requests.exceptions.RequestException as e:
            raise PriceExplorerError(f"Could not reach Price Explorer service: {e}")
        if resp.status_code != 200:
            raise PriceExplorerError(f"Price Explorer search failed (HTTP {resp.status_code}): {resp.text[:200]}")
        return resp.json().get("items", [])
