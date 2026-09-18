"""SAP Business ByDesign Purchase Order Analytics client (Sep 2 2026) -
replaces the cancelled Playwright-based Open PO Quantity reader (user's
explicit ask: "we cannot go with playwright for this. cancel playwright
path to read open PO qty").

The user pointed at a real, working SAP Analytics OData report -
`RPSRMPO_B02_Q0004QueryResults` (Purchasing analytics, item-level) -
confirmed live to expose exactly what's needed per PO line item:
CPO_ID (PO number), CITM_ID (item number), FCQUANTITY (PO Quantity),
FCDLV_QUANTITY (cumulative Delivery Quantity to date, across ANY
receipt channel - Playwright GR, manual SAP UI entry, etc). Open
Quantity = FCQUANTITY - FCDLV_QUANTITY (live-verified against PO 29086,
whose 2 items show DLV_QUANTITY=7 each after this session's real GRN
posting - matches SAP's own screen exactly).

This is a single fast OData GET per batch (no browser automation, no
per-PO page navigation) - the entire cancelled Playwright approach
(10-20s/PO, sequential, ~100% failure rate on this tenant's SAPUI5
dropdown quirks) is no longer needed.

Sep 19 2026 fix (real user report: Supplier Dashboard PO fetch taking
20-25s, page looks frozen) - `fetch_open_po_quantities` used to run
every 15-PO chunk SEQUENTIALLY even though each individual chunk request
is already independent of the others. Now fetches up to
`SAP_MAX_CONCURRENT_REQUESTS` chunks concurrently via a thread pool -
`sap_semaphore` (already shared app-wide) still caps the real number of
simultaneous SAP HTTP calls, this just lets that existing capacity be
used in parallel instead of one request waiting idle for the previous
one to finish."""
import logging
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore, SAP_MAX_CONCURRENT_REQUESTS

logger = logging.getLogger(__name__)

REPORT_PATH = "sap/byd/odata/ana_businessanalytics_analytics.svc/RPSRMPO_B02_Q0004QueryResults"
BATCH_SIZE = 15


def _parse_qty(value: str) -> float:
    """SAP returns quantities as e.g. '8,000.0000000000 ea'."""
    text = (value or "").strip().split()[0] if (value or "").strip() else "0"
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return 0.0


class SAPPOAnalyticsClient:
    def __init__(self, instance_url: str, username: str, password: str):
        self.url = f"{instance_url.rstrip('/')}/{REPORT_PATH}"
        self.auth = HTTPBasicAuth(username, password)

    @staticmethod
    def _chunks(items, size):
        items = list(items)
        for i in range(0, len(items), size):
            yield items[i:i + size]

    def fetch_open_po_quantities(self, po_numbers: list) -> dict:
        """Returns {po_number: {item_number: {"po_qty", "delivered_qty",
        "open_qty", "delivery_completed"}}} - same shape the caller
        (supplier_shipment_service.store_sap_open_qty_cache) already
        expects, so no downstream changes were needed. A PO missing
        from the result means this batch's read failed (network/SAP
        hiccup) - caller keeps using its last cached value, per the
        existing get_sap_open_qty() contract.

        Sep 19 2026: chunks now fetch concurrently (see module docstring)
        instead of one-at-a-time - order of completion doesn't matter,
        every chunk's rows just merge into the same `results` dict."""
        results = {}
        chunks = list(self._chunks({p for p in po_numbers if p}, BATCH_SIZE))
        if not chunks:
            return results

        def _fetch_chunk(chunk):
            filter_expr = " or ".join(f"CPO_ID eq '{po}'" for po in chunk)
            try:
                with sap_semaphore:
                    resp = requests.get(
                        self.url,
                        auth=self.auth,
                        timeout=30,
                        headers={"Accept": "application/json"},
                        params={"$filter": filter_expr, "$format": "json"},
                    )
                if resp.status_code != 200:
                    logger.warning(f"Open PO Quantity: analytics query returned HTTP {resp.status_code} for chunk of {len(chunk)} PO(s): {resp.text[:300]}")
                    return []
                return resp.json().get("d", {}).get("results", [])
            except (requests.exceptions.RequestException, ValueError) as e:
                logger.warning(f"Open PO Quantity: analytics query failed for a chunk of {len(chunk)} PO(s), skipping this round: {e}")
                return []

        with ThreadPoolExecutor(max_workers=SAP_MAX_CONCURRENT_REQUESTS) as executor:
            for rows in executor.map(_fetch_chunk, chunks):
                for row in rows:
                    po_number = row.get("CPO_ID")
                    item_number = row.get("CITM_ID")
                    if not po_number or not item_number:
                        continue
                    po_qty = _parse_qty(row.get("FCQUANTITY"))
                    delivered_qty = _parse_qty(row.get("FCDLV_QUANTITY"))
                    results.setdefault(po_number, {})[item_number] = {
                        "po_qty": po_qty,
                        "delivered_qty": delivered_qty,
                        "open_qty": round(po_qty - delivered_qty, 4),
                        "delivery_completed": (po_qty - delivered_qty) <= 1e-6,
                    }
        return results
