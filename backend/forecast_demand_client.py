"""Client for OMS's "Forecast Demand" feed (Radish Technologies) - server-
to-server, X-Api-Key auth, same host/family as open_po_client.py. See
https://oms.radishtechnologies.com/FORECAST_DEMAND_API.md for the full
field reference (fetched and reviewed directly - not guessed).

Returns the LATEST snapshot per source (customer+vendor for Walmart, or
filename for other customers) - one entry per DC-item, with both a full
week-by-week breakdown (`weeks`) and pre-aggregated forward-looking
rollups (`rollups.wk4/wk13/wk26/wk52`, each a cumulative qty+value_usd
starting from that source's `start_week`). `items[].oms_code` is
confirmed (per the doc) to be the SAME item-code namespace as
open_po_client.py's `item_code` - joinable directly, no translation
needed."""
import requests


class ForecastDemandError(Exception):
    pass


class ForecastDemandAuthError(ForecastDemandError):
    pass


class ForecastDemandClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def get_forecast_demand(self, customer: str = None, updated_since: str = None, limit: int = None) -> dict:
        """Returns {as_of, customer, count, sources: [...], items: [...]}
        - see module docstring for the items[] shape."""
        params = {}
        if customer:
            params["customer"] = customer
        if updated_since:
            params["updated_since"] = updated_since
        if limit:
            params["limit"] = limit
        resp = requests.get(
            f"{self.base_url}/api/integration/forecast-demand",
            headers={"X-Api-Key": self.api_key}, params=params, timeout=30,
        )
        if resp.status_code == 401:
            raise ForecastDemandAuthError("Forecast Demand feed rejected the API key (401)")
        if resp.status_code >= 400:
            raise ForecastDemandError(f"Forecast Demand feed returned HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()
