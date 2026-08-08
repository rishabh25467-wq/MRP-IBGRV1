"""SAP Business ByDesign On-Hand Inventory client.

Reads live, company-wide on-hand stock quantities from a custom SAP
Analytics report (OData) built on the "On-Hand Inventory" (SCMINVV02) data
source.

Important quirk discovered empirically against this SAP tenant: this
report's OData $select parameter changes which characteristics the OLAP
engine aggregates by, and when only a *subset* of characteristics is
selected, CMATERIAL_UUID stops resolving to its business ID (e.g.
'SI-0038C-2') and is instead returned as an internal numeric surrogate key
(e.g. '430') that cannot be joined back to anything. Requesting the FULL,
un-$select'd row shape avoids this and always returns the real Material ID.
So this client deliberately fetches full rows (paging via $top/$skip) and
picks out the fields it needs. get_on_hand_stock() sums KCON_HAND_STOCK per
material across every row to get one company-wide total (used for
Purchasing Plan netting); get_inventory_detail() keeps each row separate
(Site, Logistics Area, Stock Status) for the Inventory page's location
breakdown.

Despite the field's technical name, CMATERIAL_UUID is SAP's business
Material/Product ID - the same value used elsewhere in this app as
`product_id` (NOT a GUID/UUID, and NOT the same as `product_uuid`).
"""
import logging

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

PAGE_SIZE = 5000


class SAPInventoryError(Exception):
    pass


class SAPInventoryClient:
    def __init__(self, report_url: str, username: str, password: str):
        self.report_url = report_url.rstrip("/")
        self.auth = HTTPBasicAuth(username, password)

    def _fetch_page(self, skip: int) -> list:
        resp = requests.get(
            self.report_url,
            auth=self.auth,
            timeout=60,
            headers={"Accept": "application/json"},
            params={"$format": "json", "$top": PAGE_SIZE, "$skip": skip},
        )
        if resp.status_code != 200:
            raise SAPInventoryError(f"SAP inventory report returned HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if "error" in data:
            raise SAPInventoryError(data["error"].get("message", {}).get("value", "Unknown OData error"))
        return data.get("d", {}).get("results", [])

    def _fetch_all_rows(self) -> list:
        rows = []
        skip = 0
        while True:
            page = self._fetch_page(skip)
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                break
            skip += PAGE_SIZE
        return rows

    def get_on_hand_stock(self) -> dict:
        """Returns {product_id: on_hand_qty}, summed across every
        site/logistics-area/stock-status row reported for that material."""
        stock = {}
        for row in self._fetch_all_rows():
            product_id = row.get("CMATERIAL_UUID")
            if not product_id:
                continue
            try:
                qty = float(row.get("KCON_HAND_STOCK") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            stock[product_id] = stock.get(product_id, 0.0) + qty
        return stock

    def get_inventory_detail(self) -> list:
        """Returns one row per material x site x logistics-area x stock
        status - the un-aggregated counterpart to get_on_hand_stock(), for
        the Inventory page's location breakdown. Each row:
        {product_id, description, site, logistics_area, stock_status,
        qty, uom, company_code, company_name}. `description`/`site`/
        `logistics_area`/`stock_status` use the report's human-readable T*
        fields (e.g. TMATERIAL_UUID, TSITE_UUID, TLOG_AREA_UUID,
        TINV_STOCK_STATUS_CODE). `company_code`/`company_name` come from
        CCO_UUID/TCO_UUID (e.g. 'RI'/'RAY INTERNATIONAL') - this report
        tags every single row with which SAP company code it belongs to,
        used by the Inventory page's Entity filter (Ray vs Radish)."""
        detail = []
        for row in self._fetch_all_rows():
            product_id = row.get("CMATERIAL_UUID")
            if not product_id:
                continue
            try:
                qty = float(row.get("KCON_HAND_STOCK") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            detail.append({
                "product_id": product_id,
                "description": row.get("TMATERIAL_UUID"),
                "site": row.get("TSITE_UUID"),
                "logistics_area": row.get("TLOG_AREA_UUID"),
                "stock_status": row.get("TINV_STOCK_STATUS_CODE"),
                "qty": qty,
                "uom": row.get("CON_HAND_STOCK_UOM"),
                "company_code": row.get("CCO_UUID"),
                "company_name": row.get("TCO_UUID"),
            })
        return detail
