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


def _parse_odata_date(value):
    """OData v2 JSON dates are serialized as '/Date(epoch_ms)/'."""
    if not value or not value.startswith("/Date("):
        return None
    from datetime import datetime, timezone
    epoch_ms = int(value[len("/Date("):-len(")/")])
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).date().isoformat()


# Sep 20 2026, user's explicit ask ("filter by supplier first and pull")
# - this SAME report already used by fetch_open_po_quantities above also
# exposes CSELLER (the vendor code) as a REAL, working server-side filter
# (live-verified: $filter=CSELLER eq 'H1330' returned only that vendor's
# 54 rows in ~3s) - unlike sap_po_client.py's SOAP service, whose vendor
# filter is silently ignored by this tenant (see that module's own
# docstring). CITM_LFCYCLE_ST carries the exact same lifecycle codes as
# sap_po_client.LIFECYCLE_STATUS_TEXT (live-verified: 1/4/8/10 = In
# Preparation/Rejected/Cancelled/Finished - the same "not open" states
# sap_po_client._parse_pos excludes via 2 separate SOAP fields).
EXCLUDED_ITEM_LIFECYCLE_CODES = {"1", "4", "8", "10"}


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

    def fetch_pos_for_vendor(self, vendor_code: str) -> list:
        """Sep 20 2026, user's explicit ask - a single vendor-scoped
        fetch for the "Pull Latest POs" buttons, in the SAME row shape
        `sap_po_client._parse_pos` produces (so it merges into the exact
        same `supplier_portal_po_cache` via
        supplier_shipment_service.refresh_po_cache) - just via this
        report's real, working CSELLER filter instead of a whole-tenant
        SOAP ID-range scan. Typically a few seconds, not 1-3+ minutes."""
        try:
            with sap_semaphore:
                resp = requests.get(
                    self.url,
                    auth=self.auth,
                    timeout=30,
                    headers={"Accept": "application/json"},
                    params={"$filter": f"CSELLER eq '{vendor_code}'", "$format": "json", "$top": "2000"},
                )
            if resp.status_code != 200:
                logger.warning(f"Pull Latest POs: analytics query returned HTTP {resp.status_code} for vendor {vendor_code}: {resp.text[:300]}")
                return None
            raw_rows = resp.json().get("d", {}).get("results", [])
        except (requests.exceptions.RequestException, ValueError) as e:
            logger.warning(f"Pull Latest POs: analytics query failed for vendor {vendor_code}: {e}")
            return None
        rows = []
        for row in raw_rows:
            if row.get("CITM_LFCYCLE_ST") in EXCLUDED_ITEM_LIFECYCLE_CODES:
                continue
            po_number, item_number = row.get("CPO_ID"), row.get("CITM_ID")
            if not po_number or not item_number:
                continue
            rows.append({
                "po_number": po_number,
                "item_number": item_number,
                "vendor_code": row.get("CSELLER"),
                "vendor_name": row.get("TSELLER"),
                "product_id": (row.get("CPRD_UUID") or "").strip() or None,
                "description": row.get("CITM_DESCRIPTION"),
                "po_qty": _parse_qty(row.get("KCQUANTITY")),
                "unit_of_measure": row.get("UCQUANTITY"),
                "due_date": _parse_odata_date(row.get("CITM_DLV_STDT")),
                "po_date": _parse_odata_date(row.get("CORDERED_DATE")),
                "buyer_code": row.get("CBUYER"),
                "currency": row.get("RCNET_PRICE"),
                "lifecycle_status_code": row.get("CITM_LFCYCLE_ST"),
                "lifecycle_status_text": row.get("TITM_LFCYCLE_ST"),
                "unit_price": _parse_qty(row.get("KCNET_PRICE")) or None,
                "subtotal": _parse_qty(row.get("KCNET_VALUE_LIMIT")) or None,
                "ship_to_site_id": row.get("CRECEIVING_SITE"),
            })
        return rows
