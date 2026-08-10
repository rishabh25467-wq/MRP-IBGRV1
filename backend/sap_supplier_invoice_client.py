"""Client for SAP Business ByDesign's native Supplier Invoice query
(QuerySupplierInvoiceQueryIn, operation FindSimpleByElements / SOAP action
QUERY_BY_ELEMENTS) - reads REAL, posted Supplier Invoice line items for a
given Product ID straight from SAP itself (supplier name, net unit price,
quantity, invoice date). This is the SAP-native counterpart to the external
Price Explorer (MS SQL ERP) data - useful as an authoritative cross-check,
and to prove actual purchase history exists for items that have no
Released SAP Price Specification (e.g. SCR410WM - discussed with user Feb
2026: has real invoices/moving-average cost but no formal Price Spec).

Authorization: requires the `QuerySupplierInvoiceQueryIn` service role on
the `_EMERGENTBOM` technical user (granted Feb 2026 - see
/app/SAP_SUPPLIER_INVOICE_QUERY_AUTHORIZATION_REQUEST.md). WSDL confirmed
endpoint: SAP_SOAP_SUPPLIER_INVOICE_ENDPOINT in backend/.env.
"""
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

from sap_rate_limiter import sap_semaphore


class SAPSupplierInvoiceError(Exception):
    pass


_DATE_FILTER = """  <SelectionByDate>
   <InclusionExclusionCode>I</InclusionExclusionCode>
   <IntervalBoundaryTypeCode>9</IntervalBoundaryTypeCode>
   <LowerBoundaryDate>{min_date}</LowerBoundaryDate>
  </SelectionByDate>
"""

_REQUEST_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
<soapenv:Body>
<n0:SupplierInvoiceSimpleByElementsQueryMBF_sync xmlns:n0="http://sap.com/xi/SAPGlobal20/Global">
 <SupplierInvoiceSimpleSelectionByElements>
{date_filter}  <SelectionByItemProductProductKeyProductID>
   <InclusionExclusionCode>I</InclusionExclusionCode>
   <IntervalBoundaryTypeCode>1</IntervalBoundaryTypeCode>
   <LowerBoundaryProductID>{product_id}</LowerBoundaryProductID>
  </SelectionByItemProductProductKeyProductID>
 </SupplierInvoiceSimpleSelectionByElements>
 <ProcessingConditions>
  <QueryHitsMaximumNumberValue>{limit}</QueryHitsMaximumNumberValue>
  <QueryHitsUnlimitedIndicator>false</QueryHitsUnlimitedIndicator>
 </ProcessingConditions>
</n0:SupplierInvoiceSimpleByElementsQueryMBF_sync>
</soapenv:Body>
</soapenv:Envelope>"""


class SAPSupplierInvoiceClient:
    # How many hits to ask SAP for per query - a safety cap, not a
    # substitute for date filtering (see get_invoices_for_product).
    FETCH_LIMIT = 200
    # Cascading recency windows (days) tried in order, narrowest first. A
    # narrow window is both fast AND provably complete when the hit count
    # comes back under FETCH_LIMIT (we know we got every invoice in that
    # window, so no truncation-order ambiguity) - only widen if a window
    # comes back completely empty (this product just wasn't invoiced that
    # recently). Falls back to a plain undated query if even the widest
    # window has zero hits (a rarely-purchased item).
    RECENT_WINDOWS_DAYS = [30, 90, 400]

    def __init__(self, endpoint: str, username: str, password: str):
        self.endpoint = endpoint
        self.username = username
        self.password = password

    def _run_query(self, product_id: str, limit: int, min_date: str = None) -> list:
        date_filter = _DATE_FILTER.format(min_date=min_date) if min_date else ""
        body = _REQUEST_TEMPLATE.format(product_id=product_id, limit=limit, date_filter=date_filter)
        try:
            with sap_semaphore:
                resp = requests.post(
                    self.endpoint,
                    auth=(self.username, self.password),
                    data=body.encode("utf-8"),
                    headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": "QUERY_BY_ELEMENTS"},
                    timeout=60,
                )
        except requests.exceptions.RequestException as e:
            raise SAPSupplierInvoiceError(f"Could not reach SAP: {e}")

        if resp.status_code != 200 or "Fault" in resp.text[:2000]:
            raise SAPSupplierInvoiceError(f"SAP Supplier Invoice query failed (HTTP {resp.status_code}): {resp.text[:400]}")

        root = ET.fromstring(resp.text)
        rows = []
        for invoice in root.findall(".//SupplierInvoice"):
            invoice_id = invoice.findtext("ID")
            date = invoice.findtext("Date")
            seller = invoice.find("SellerParty")
            supplier_name = None
            supplier_internal_id = None
            if seller is not None:
                supplier_name = seller.findtext("AddressSnapshot/FormattedAddress/FormattedName")
                supplier_internal_id = seller.findtext("PartyKey/PartyID")
            # The vendor's own invoice number (as printed on their physical/PDF
            # bill) is NOT the top-level <ID> above (that's SAP's internal
            # document number) - it's under ExternalDocumentID, identified by
            # TypeCode 28. Falls back to any ExternalDocumentID ref without a
            # TypeCode if 28 isn't present (some tenants omit it).
            supplier_invoice_number = None
            for ext_ref in invoice.findall("ExternalDocumentID/BusinessTransactionDocumentReference"):
                ref_id = ext_ref.findtext("ID")
                if not ref_id:
                    continue
                if ext_ref.findtext("TypeCode") == "28":
                    supplier_invoice_number = ref_id
                    break
                if supplier_invoice_number is None:
                    supplier_invoice_number = ref_id
            for item in invoice.findall("Item"):
                # Items with a ParentItemUUID are SAP's own sub-item
                # breakdown/reference detail of another item on this same
                # invoice (e.g. when one commercial line is matched against
                # multiple PO/goods-receipt references) - NOT an independent
                # second purchase. Counting them produces phantom "duplicate"
                # rows with identical qty/price to their parent - skip them.
                if item.findtext("ParentItemUUID"):
                    continue
                item_product_id = item.findtext("Product/ProductKey/ProductID")
                if not item_product_id or item_product_id.strip().upper() != product_id.strip().upper():
                    continue
                qty_el = item.find("Quantity")
                price_el = item.find("NetUnitPrice/Amount")
                rows.append({
                    "invoice_id": invoice_id,
                    "supplier_invoice_number": supplier_invoice_number,
                    "date": date,
                    "supplier_name": (supplier_name or "").strip() or None,
                    "supplier_internal_id": supplier_internal_id,
                    "quantity": float(qty_el.text) if qty_el is not None and qty_el.text else None,
                    "unit_of_measure": qty_el.get("unitCode") if qty_el is not None else None,
                    "price": float(price_el.text) if price_el is not None and price_el.text else None,
                    "currency": price_el.get("currencyCode") if price_el is not None else None,
                })
        return rows

    def get_invoices_for_product(self, product_id: str, limit: int = 20) -> list:
        """Returns real SAP Supplier Invoice line items for this Product ID:
        [{invoice_id, date, supplier_name, supplier_internal_id, quantity,
        unit_of_measure, price, currency}, ...] - one row per genuine
        top-level commercial item (SAP's own ParentItemUUID sub-items,
        which duplicate their parent's product/qty/price as reference
        breakdown detail, are filtered out - see the ParentItemUUID check
        above), sorted newest-first, capped to `limit` rows for display.

        IMPORTANT: SAP's FindSimpleByElements query does NOT return hits in
        date order - it's some internal/creation-order sequence, and a
        heavily-used component can have hundreds of invoice lines even
        within a single recent month. Asking SAP for a capped number of
        hits and THEN sorting client-side silently misses genuinely newer
        invoices that weren't inside that arbitrary batch, no matter how
        large the cap - empirically confirmed on a real product (SPC5WM):
        even a 200-hit query with a 400-day SelectionByDate filter still
        under-reported the newest date by 2+ months compared to raising
        the cap further, because 400+ lines existed inside that window
        alone. Fix: try progressively wider recency windows
        (RECENT_WINDOWS_DAYS) - a narrow window (30 days) both responds in
        seconds AND is provably complete whenever its hit count comes back
        under FETCH_LIMIT (no ordering ambiguity possible if we got every
        row that exists in that window). Only widen if a window is
        genuinely empty; fall back to an undated query only if the item
        has no invoices at all within the widest window (a rarely-
        purchased part)."""
        rows = []
        for days in self.RECENT_WINDOWS_DAYS:
            min_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
            rows = self._run_query(product_id, self.FETCH_LIMIT, min_date=min_date)
            if rows:
                break
        if not rows:
            rows = self._run_query(product_id, self.FETCH_LIMIT)
        rows.sort(key=lambda r: r["date"] or "", reverse=True)
        return rows[:limit]
