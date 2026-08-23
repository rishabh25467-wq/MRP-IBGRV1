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
import time

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

logger = logging.getLogger(__name__)

PAGE_SIZE = 5000
# Aug 2026 - this tenant's connectivity is documented as flaky (frequent
# ConnectTimeoutError seen elsewhere on this same tenant); _fetch_page used
# to have ZERO retry, so a single transient blip on any one page killed
# the WHOLE multi-page pull instantly, discarding every page already
# fetched - the actual root cause behind "the manual Inventory refresh
# fails more often than not" (real user feedback). Short, bounded backoff
# so a genuinely-down SAP still fails fast rather than hammering it.
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = [3, 8]


class SAPInventoryError(Exception):
    pass


class SAPInventoryClient:
    def __init__(self, report_url: str, username: str, password: str):
        self.report_url = report_url.rstrip("/")
        self.auth = HTTPBasicAuth(username, password)

    def _fetch_page(self, skip: int, site_id: str = None, warehouse_ids: list = None) -> list:
        params = {"$format": "json", "$top": PAGE_SIZE, "$skip": skip}
        if warehouse_ids:
            # Verified live (Aug 2026): CLOG_AREA_UUID (the raw warehouse/
            # logistics-area characteristic, e.g. "P9/P9-RM") filters just
            # as cleanly as CSITE_UUID below - even fewer rows, even
            # faster (~7.5s for one warehouse vs ~12s for a whole site).
            # Multiple warehouses (e.g. RM + QC) OR together in one call -
            # verified live too (~6.9s for both at once).
            params["$filter"] = " or ".join(f"CLOG_AREA_UUID eq '{w}'" for w in warehouse_ids)
        elif site_id:
            # Verified live (Aug 2026): $filter on the raw CSITE_UUID
            # characteristic works cleanly and does NOT trigger the
            # CMATERIAL_UUID-resolves-to-a-surrogate-key quirk documented
            # above - that quirk is specifically about restricting
            # $select, and this only adds $filter, $select is untouched.
            # An earlier attempt to filter by CMATERIAL_UUID hit a
            # generic 500, but that was later root-caused (see PRD) to
            # SAP's own "too many CONCURRENT requests against the same
            # analytics data source" limit, not a filter-syntax problem -
            # a single, semaphore-gated filtered call like this is fine.
            params["$filter"] = f"CSITE_UUID eq '{site_id}'"
        last_exc = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with sap_semaphore:
                    resp = requests.get(
                        self.report_url,
                        auth=self.auth,
                        timeout=60,
                        headers={"Accept": "application/json"},
                        params=params,
                    )
                if resp.status_code != 200:
                    raise SAPInventoryError(f"SAP inventory report returned HTTP {resp.status_code}: {resp.text[:300]}")
                data = resp.json()
                if "error" in data:
                    raise SAPInventoryError(data["error"].get("message", {}).get("value", "Unknown OData error"))
                return data.get("d", {}).get("results", [])
            except (requests.exceptions.RequestException, SAPInventoryError) as e:
                last_exc = e
                if attempt < _MAX_ATTEMPTS - 1:
                    logger.warning(f"SAP inventory page fetch (skip={skip}, site={site_id}, warehouses={warehouse_ids}) failed on attempt {attempt + 1}/{_MAX_ATTEMPTS}, retrying: {e}")
                    time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
        raise SAPInventoryError(f"SAP inventory report failed after {_MAX_ATTEMPTS} attempts (skip={skip}, site={site_id}, warehouses={warehouse_ids}): {last_exc}")

    def _fetch_all_rows(self, site_id: str = None, warehouse_ids: list = None) -> list:
        rows = []
        skip = 0
        while True:
            page = self._fetch_page(skip, site_id=site_id, warehouse_ids=warehouse_ids)
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

    def get_inventory_detail(self, site_id: str = None, warehouse_ids: list = None) -> list:
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
        used by the Inventory page's Entity filter (Ray vs Radish).
        Pass `site_id` (e.g. "P9") to scope the live pull down to just one
        site - verified live to take ~12s vs 60s+ for the whole company.
        Pass `warehouse_ids` (e.g. ["P9/P9-RM", "P9/P9-QC"]) to scope down
        to just those warehouses (any site) - verified live even faster
        (~7s for one, ~7s for two OR'd together). warehouse_ids takes
        priority over site_id if both are somehow passed - see
        _fetch_page. See the $filter note on _fetch_page."""
        detail = []
        for row in self._fetch_all_rows(site_id=site_id, warehouse_ids=warehouse_ids):
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
                # Raw SAP Logistics Area ID (CLOG_AREA_UUID, e.g. "P2/P2-RM") -
                # distinct from "logistics_area" above which is the human-
                # readable TEXT ("RAW MATERIAL GODOWN-P2"). Aug 2026 bug fix:
                # the Goods Movement rule matches/calls SAP using THIS raw ID,
                # never the description - a real "approved but stock not
                # moved" case was traced to comparing the description string
                # against the fixed "{site}/{site}-RM" ID and never matching.
                "logistics_area_id": row.get("CLOG_AREA_UUID"),
                "stock_status": row.get("TINV_STOCK_STATUS_CODE"),
                # Aug 2026 bug fix: real SAP incident - a row can report a
                # perfectly normal stock_status ("Not Assigned") while ALSO
                # being flagged Restricted Use (CRESTRICTED_IND / the
                # "Restr." checkbox on SAP's own Stock Overview screen) -
                # this is a SEPARATE SAP field from stock_status entirely,
                # not another status value, so it was silently missed by
                # every "is this stock usable" check in this app (both the
                # Store Approval/Production Confirmation display AND the
                # actual movement-matching logic) until caught against a
                # real 1kg restricted-use row at site P2.
                "restricted": bool(row.get("CRESTRICTED_IND")),
                "qty": qty,
                "uom": row.get("CON_HAND_STOCK_UOM"),
                "company_code": row.get("CCO_UUID"),
                "company_name": row.get("TCO_UUID"),
            })
        return detail
