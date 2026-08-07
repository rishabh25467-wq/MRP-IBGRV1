"""Client for the external OMS "Open-PO Demand" feed (see
MRP_OPEN_PO_DEMAND_API.md for the full spec this was built against).

This is a SEPARATE integration from oms_client.py - different base URL,
and a different auth mechanism (a static X-API-Key header, no login/JWT
flow). Read-only: returns one row per open purchase-order line (qty_open >
0), each carrying the customer's target ship date and the item's ERP lead
time - the raw demand signal the Production Plan page's MRP computation
(mrp_service.py) runs against.
"""
import logging

import requests

logger = logging.getLogger(__name__)


class OpenPODemandError(Exception):
    pass


class OpenPODemandClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def get_open_po_demand(self, customer: str = None, plant: str = None, updated_since: str = None, limit: int = None) -> dict:
        """Returns the raw feed response: {count, customer, plant,
        updated_since, max_changed_at, truncated, rows: [...]}. See the spec
        for the full row field list (item_code, qty_open, target_ship_date,
        lead_day, invoice_price, etc)."""
        params = {}
        if customer:
            params["customer"] = customer
        if plant:
            params["plant"] = plant
        if updated_since:
            params["updated_since"] = updated_since
        if limit:
            params["limit"] = limit

        try:
            resp = requests.get(
                f"{self.base_url}/api/integration/open-po-demand",
                headers={"X-API-Key": self.api_key},
                params=params,
                timeout=30,
            )
        except requests.exceptions.RequestException as e:
            raise OpenPODemandError(f"Could not reach Open-PO Demand feed: {e}")

        if resp.status_code == 401:
            raise OpenPODemandError("Open-PO Demand feed rejected the API key (HTTP 401)")
        if resp.status_code == 503:
            raise OpenPODemandError("Open-PO Demand feed has no API key configured on the server side (HTTP 503)")
        if resp.status_code != 200:
            raise OpenPODemandError(f"Open-PO Demand feed request failed: HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()
