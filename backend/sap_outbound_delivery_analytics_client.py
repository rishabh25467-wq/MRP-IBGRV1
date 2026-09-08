"""SAP Business ByDesign Outbound Delivery Analytics client (Sep 9 2026) -
user's own find, same pattern as sap_po_analytics_client.py (a single fast
OData GET replacing/augmenting a fragile Playwright-based detection).

Report `RPSCMOBDB04_Q0001QueryResults`, filtered by `CSTO_REF_ID` (the STO's
own SAP Order ID, e.g. "31297") - confirmed LIVE (STO-000195/order 31297)
to return one row per delivered line with:
  CDELIVERY_UUID   - despite the name, this is the Delivery's human-readable
                      ID ("P2D1-5527"), NOT a GUID - exactly what
                      sap_outbound_delivery_client.get_delivery_object_id_by_id
                      already expects.
  CDELIVERY_STATUS / TDELIVERY_STATUS - "3"/"Finished" once Goods Issue has
                      posted (same 1/2/3 = Not Started/In Process/Finished
                      convention as OrderFulfilmentProcessingStatusCode
                      elsewhere in this app) - anything else means the
                      Delivery exists but Release/GI hasn't happened yet.
  CPRODUCT_UUID, FCDEL_QUANTITY - per-line product/quantity, informational.

Why this matters: this report reflects reality even when SAP's Delivery
Proposals screen (Playwright's own detection mechanism) has already
consumed the underlying request items into a Delivery that a prior
attempt failed to persist locally - see stock_transfer_service.py's
_try_post_goods_issue_multiline docstring for the real incident this
fixes. Querying by CSTO_REF_ID (the order itself) instead of item UUIDs
also sidesteps any risk of the OutboundDeliveryItemBusinessTransaction...
link table lagging behind."""
import logging

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

logger = logging.getLogger(__name__)

REPORT_PATH = "sap/byd/odata/ana_businessanalytics_analytics.svc/RPSCMOBDB04_Q0001QueryResults"
FINISHED_STATUS_CODE = "3"


class SAPOutboundDeliveryAnalyticsError(Exception):
    pass


class SAPOutboundDeliveryAnalyticsClient:
    def __init__(self, instance_url: str, username: str, password: str):
        self.url = f"{instance_url.rstrip('/')}/{REPORT_PATH}"
        self.auth = HTTPBasicAuth(username, password)

    def find_deliveries_for_sto(self, sto_ref_id: str) -> list:
        """Returns [{"delivery_id", "status_code", "status_label",
        "finished", "product_uuid", "quantity", "unit_code",
        "line_item_id"}] - [] if SAP hasn't produced any Delivery for
        this order yet (perfectly normal right after order creation,
        caller should fall back to the existing combine flow)."""
        sto_ref_id = (sto_ref_id or "").lstrip("0") or sto_ref_id
        if not sto_ref_id:
            return []
        try:
            with sap_semaphore:
                resp = requests.get(
                    self.url,
                    auth=self.auth,
                    timeout=30,
                    headers={"Accept": "application/json"},
                    params={"$filter": f"CSTO_REF_ID eq '{sto_ref_id}'", "$format": "json"},
                )
        except requests.exceptions.RequestException as e:
            raise SAPOutboundDeliveryAnalyticsError(f"Could not reach SAP: {e}")
        if resp.status_code != 200:
            raise SAPOutboundDeliveryAnalyticsError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            rows = resp.json().get("d", {}).get("results", [])
        except ValueError as e:
            raise SAPOutboundDeliveryAnalyticsError(f"Could not parse SAP response: {e}")
        results = []
        for row in rows:
            delivery_id = row.get("CDELIVERY_UUID")
            if not delivery_id:
                continue
            status_code = row.get("CDELIVERY_STATUS")
            qty_text = (row.get("FCDEL_QUANTITY") or "").strip()
            qty = None
            if qty_text:
                try:
                    qty = float(qty_text.split()[0].replace(",", ""))
                except ValueError:
                    qty = None
            results.append({
                "delivery_id": delivery_id,
                "status_code": status_code,
                "status_label": row.get("TDELIVERY_STATUS"),
                "finished": status_code == FINISHED_STATUS_CODE,
                "product_uuid": row.get("CPRODUCT_UUID"),
                "quantity": qty,
                "unit_code": row.get("CDEL_QUANTITY_UNIT_CODE"),
                "line_item_id": row.get("CSTO_REF_ITEM_ID"),
            })
        return results
