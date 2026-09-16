"""SAP "Inbound Delivery Detailed Details" analytics report client.

Sep 11 2026, user's explicit ask: a few live GRNs actually posted/were
received in SAP, confirmed by the user directly in SAP's own report
(e.g. PO 29368 -> Inbound Delivery 52897), but our own Playwright
automation never captured the resulting Inbound Delivery ID (a silent
mid-automation hiccup) - this field stays blank in the app forever
with no other way to recover it. Staff can now pull the real number
straight from SAP on demand via a "Fetch from SAP" button.

Same standard `ana_businessanalytics_analytics.svc` OData service as
sap_inventory_closing_client.py (a different ReportID though -
RPZ9645E136D1191FF78E36A7). Matches on PO number (CREF_MST_ID) + the
EXACT supplier bill/invoice number staff entered (CREF_ID, SAP's own
"Inbound Delivery Notification" reference) - both confirmed live
against real data.

IMPORTANT, confirmed live: the field literally named "CCONF_INB_DEL_ID"
in this report is a DEAD field in this tenant - always blank, verified
across 300+ real rows regardless of filter/date/site. The real Inbound
Delivery ID lives under `CDELIVERY_UUID` instead (despite the
misleading "UUID" name, it holds the same plain numeric ID shown in
SAP's own UI, e.g. "52897") - only visible using the SAP_ODATA_BUSINESS_USER
credentials (SAP_ODATA_USERNAME, the technical user, never returned it
in testing - same class of Key-User-field authorization gap already
seen elsewhere in this app, e.g. the "Printed PO #" investigation)."""
import logging

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 200


class SAPInboundDeliveryReportError(Exception):
    pass


class SAPInboundDeliveryReportClient:
    def __init__(self, report_url: str, username: str, password: str):
        self.report_url = report_url.rstrip("/")
        self.auth = HTTPBasicAuth(username, password)

    def find_confirmation_rows(self, po_number: str, supplier_doc_num: str) -> list:
        params = {
            "$format": "json",
            "$filter": f"CREF_MST_ID eq '{po_number}' and CREF_ID eq '{supplier_doc_num}'",
            "$select": "CDELIVERY_UUID,CPRODUCT_UUID,TPRODUCT_UUID,FCCONF_QUAN,FCINV_QUAN,CTA_DATE",
        }
        try:
            resp = requests.get(self.report_url, auth=self.auth, params=params,
                                 headers={"Accept": "application/json"}, timeout=_TIMEOUT_SECONDS)
        except requests.exceptions.RequestException as e:
            raise SAPInboundDeliveryReportError(f"Could not reach SAP: {e}")
        if resp.status_code != 200:
            raise SAPInboundDeliveryReportError(f"SAP returned HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if "error" in data:
            raise SAPInboundDeliveryReportError(data["error"].get("message", {}).get("value", "Unknown OData error"))
        return data.get("d", {}).get("results", [])
