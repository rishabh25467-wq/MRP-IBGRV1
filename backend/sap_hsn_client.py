"""SAP Business ByDesign HSN Code lookup (Aug 27 2026).

The standard `HSNCodeIndia` field on the Material BO, and the `INHSNCode`
field on the CustomerInvoice BO, are both PSM-blocked from custom OData
services on this tenant (confirmed live). The one working path found: a
custom Business Analytics report built on the "Material Master Data" data
source DOES expose the HSN Code as a normal reportable characteristic
(CGLO_IN_HSN_CODE), same analytics-report mechanism already used for
on-hand inventory (see sap_inventory_client.py).

Verified live (Aug 27 2026) against this exact report:
  - CMATR_INT_ID resolves to the real business Material ID (e.g.
    "411-144-073V"), NOT a surrogate key - unlike sap_inventory_client's
    documented $select quirk, filtering (not selecting) by CMATR_INT_ID
    works cleanly and returns exactly that one material's row.
  - CGLO_IN_HSN_CODE is blank ("") for materials that have never had an
    HSN code maintained in Product Data - treated as "no HSN code
    available yet" (never invented/guessed) by get_hsn_codes() below.
"""
import logging

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

logger = logging.getLogger(__name__)

BATCH_SIZE = 15


class SAPHSNError(Exception):
    pass


class SAPHSNClient:
    def __init__(self, report_url: str, username: str, password: str):
        self.report_url = report_url.rstrip("/")
        self.auth = HTTPBasicAuth(username, password)

    @staticmethod
    def _chunks(items, size):
        items = list(items)
        for i in range(0, len(items), size):
            yield items[i:i + size]

    def get_hsn_codes(self, product_ids: list) -> dict:
        """Returns {product_id: hsn_code}. A product with no HSN code
        maintained in SAP, or not found in the report at all, is simply
        omitted from the result - callers must treat a missing key as
        "no HSN code available", never as an error."""
        product_ids = list({p for p in product_ids if p})
        if not product_ids:
            return {}

        codes = {}
        for chunk in self._chunks(product_ids, BATCH_SIZE):
            filter_expr = " or ".join(f"CMATR_INT_ID eq '{pid}'" for pid in chunk)
            with sap_semaphore:
                resp = requests.get(
                    self.report_url,
                    auth=self.auth,
                    timeout=30,
                    headers={"Accept": "application/json"},
                    params={"$format": "json", "$filter": filter_expr},
                )
            if resp.status_code != 200:
                raise SAPHSNError(f"SAP HSN report returned HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            if "error" in data:
                raise SAPHSNError(data["error"].get("message", {}).get("value", "Unknown OData error"))
            for row in data.get("d", {}).get("results", []):
                product_id = row.get("CMATR_INT_ID")
                hsn_code = row.get("CGLO_IN_HSN_CODE")
                if product_id and hsn_code:
                    codes[product_id] = hsn_code
        return codes
