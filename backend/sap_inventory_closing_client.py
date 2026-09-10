"""SAP Business ByDesign historical/closing inventory client.

Sep 2026, user's explicit ask ("31st Aug inventory data, company wise,
plant wise Qty with value"). This app's own inventory_cache/SAPInventoryClient
only ever hold the LIVE current snapshot - there is no archive of past
dates anywhere in this system. Getting a true "as it stood on {date}"
figure needs a DIFFERENT SAP report than the live On-Hand Inventory one:
the standard "Material Inventories - Balance Summary" analytics report
(RPFININVU14_Q0001, under the standard ana_businessanalytics_analytics.svc
service - NOT a custom report like the other SAP_ODATA_* endpoints in this
app), which supports a PARA_KEYDATE filter.

Verified live against this tenant:
- Querying with only PARA_KEYDATE (no site scope) times out - too much
  data company-wide in one go.
- Scoped to one site via PARA_PERMEST (e.g. 'P1'), a small/medium site
  returns in under a minute, but a large site's full material list (e.g.
  P1 unfiltered) can take ~3+ minutes - timeout is set generously (280s)
  to accommodate this.
- In this tenant, "Business Residence" (PERMEST) is 1:1 with Site (user
  confirmed Company == Site/Plant for their reporting purposes) - CPERMEST
  comes back exactly as the site code (e.g. 'P1'), TPERMEST as its human
  name ('RAY INTERNATIONAL-P1').
- Rows carry historical CFISCYEAR/CFISCPER dimensions (the original
  posting period of whatever balance layer is still outstanding as of the
  key date) - multiple rows can exist per site+material and must be
  SUMMED, not treated as one row each.
- KCINV_QTY / KCINV_VALUE are the real numeric qty/value; CMATERIAL/
  TMATERIAL are the item code/name; RCINV_VALUE is the currency code.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 280
_MAX_ATTEMPTS = 2


class SAPInventoryClosingError(Exception):
    pass


class SAPInventoryClosingClient:
    def __init__(self, report_url: str, username: str, password: str):
        self.report_url = report_url.rstrip("/")
        self.auth = HTTPBasicAuth(username, password)

    def _fetch_site(self, key_date: str, site_id: str) -> list:
        params = {
            "$format": "json",
            "$filter": f"PARA_KEYDATE eq datetime'{key_date}T00:00:00' and PARA_PERMEST eq '{site_id}'",
        }
        last_exc = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = requests.get(self.report_url, auth=self.auth, timeout=_TIMEOUT_SECONDS,
                                     headers={"Accept": "application/json"}, params=params)
                if resp.status_code != 200:
                    raise SAPInventoryClosingError(f"SAP closing inventory report returned HTTP {resp.status_code} for site {site_id}: {resp.text[:300]}")
                data = resp.json()
                if "error" in data:
                    raise SAPInventoryClosingError(data["error"].get("message", {}).get("value", "Unknown OData error"))
                return data.get("d", {}).get("results", [])
            except (requests.exceptions.RequestException, SAPInventoryClosingError) as e:
                last_exc = e
                logger.warning(f"SAP closing inventory fetch failed for site {site_id} (attempt {attempt + 1}/{_MAX_ATTEMPTS}): {e}")
        raise SAPInventoryClosingError(f"SAP closing inventory report failed for site {site_id} after {_MAX_ATTEMPTS} attempts: {last_exc}")

    def get_closing_inventory(self, key_date: str, site_ids: list) -> list:
        """Returns one row per site x material (already summed across any
        historical fiscal-period breakdown SAP returns for that pair):
        {site_id, site_name, product_id, description, qty, uom, value,
        currency}. Queries every site in `site_ids` IN PARALLEL (each call
        alone already takes 60-90s - sequential across ~7 sites would be
        10+ minutes)."""
        rows_by_key = {}
        with ThreadPoolExecutor(max_workers=len(site_ids) or 1) as pool:
            futures = {pool.submit(self._fetch_site, key_date, site_id): site_id for site_id in site_ids}
            for future in as_completed(futures):
                site_id = futures[future]
                for row in future.result():
                    product_id = row.get("CMATERIAL")
                    if not product_id:
                        continue
                    try:
                        qty = float(row.get("KCINV_QTY") or 0)
                        value = float(row.get("KCINV_VALUE") or 0)
                    except (TypeError, ValueError):
                        qty, value = 0.0, 0.0
                    key = (site_id, product_id)
                    if key not in rows_by_key:
                        rows_by_key[key] = {
                            "site_id": row.get("CPERMEST") or site_id,
                            "site_name": row.get("TPERMEST") or site_id,
                            "product_id": product_id,
                            "description": row.get("TMATERIAL") or "",
                            "qty": 0.0,
                            "uom": row.get("UCINV_QTY") or "",
                            "value": 0.0,
                            "currency": row.get("RCINV_VALUE") or "INR",
                        }
                    rows_by_key[key]["qty"] += qty
                    rows_by_key[key]["value"] += value
        return list(rows_by_key.values())
