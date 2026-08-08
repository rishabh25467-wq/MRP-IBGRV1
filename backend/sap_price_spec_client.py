"""SAP Business ByDesign custom OData service `pricespecificationemergent`
(Business Object: ProcurementPriceSpecification) - reads real purchasing
prices already maintained in SAP (SAP's equivalent of a Purchasing Info
Record), so the Suppliers page can show "who supplies Product X at what
price" as it already exists in SAP, instead of requiring manual re-entry.

Read-only history: the underlying data is a generic condition-technique/EAV
model (Product ID and Supplier are stored as
`PriceSpecificationElementPropertyValuation` rows keyed by a property code
like `CND_PRODUCT_ID`/`CND_SUPPL_ID`, not plain fields on the Root entity -
`BusinessPartnerUUID` on the Root is NOT the supplier, empirically confirmed
it doesn't match any Supplier in QuerySupplierIn under any lifecycle status;
the real supplier internal ID + readable name live on the `CND_SUPPL_ID`
PropertyValuation row's value/Description).

Write (`create_price_spec`): uses the standard SOAP `ManageProcurementPriceSpecificIn`
MaintainBundle operation - confirmed working via a manual test write (main
agent + user, Session 12): only Product ID + Supplier ID + Rate + a
ValidityPeriod are required for SAP to accept AND auto-release the record
("List Price released" in SAP's own confirmation log) - no extra Purchasing
Org/Company/Price List Type fields needed in this tenant."""
from datetime import date, datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.auth import HTTPBasicAuth

PROPERTY_VALUATION_COLLECTION = "ProcurementPriceSpecificationPropertyValuationCollection"
PRICE_SPEC_COLLECTION = "ProcurementPriceSpecificationCollection"
PRODUCT_PROPERTY_CODE = "CND_PRODUCT_ID"
SUPPLIER_PROPERTY_CODE = "CND_SUPPL_ID"


class SAPPriceSpecError(Exception):
    pass


def _parse_odata_date(value):
    # SAP OData JSON dates look like "/Date(1682985600000)/"
    if not value or not value.startswith("/Date("):
        return None
    ms = int(value[6:-2])
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


class SAPPriceSpecClient:
    def __init__(self, base_url: str, username: str, password: str,
                 soap_endpoint: str = None, soap_username: str = None, soap_password: str = None):
        self.base_url = base_url.rstrip("/")
        self.auth = HTTPBasicAuth(username, password)
        self.soap_endpoint = soap_endpoint
        self.soap_auth = HTTPBasicAuth(soap_username, soap_password) if soap_username else None

    def _get(self, collection: str, params: dict) -> list:
        try:
            resp = requests.get(
                f"{self.base_url}/{collection}",
                auth=self.auth,
                timeout=30,
                headers={"Accept": "application/json"},
                params={**params, "$format": "json"},
            )
        except requests.exceptions.RequestException as e:
            raise SAPPriceSpecError(f"Could not reach SAP: {e}")
        if resp.status_code != 200:
            raise SAPPriceSpecError(f"SAP price specification service returned HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if "error" in data:
            raise SAPPriceSpecError(data["error"].get("message", {}).get("value", "Unknown OData error"))
        results = data.get("d", {}).get("results", data.get("d", {}))
        return results if isinstance(results, list) else [results]

    def get_price_specs_for_product(self, product_id: str) -> list:
        """Returns real SAP purchasing price records for a Product ID:
        [{sap_id, supplier_internal_id, supplier_name, price, currency,
        unit, start_date, end_date, release_status_code}, ...]. Empty list
        if SAP has none - that's a normal outcome, not an error."""
        escaped = product_id.replace("'", "''")
        rows = self._get(PROPERTY_VALUATION_COLLECTION, {
            "$filter": f"PriceSpecificationElementPropertyRefe eq '{PRODUCT_PROPERTY_CODE}' "
                       f"and PriceSpecificationElementPropertyValu eq '{escaped}'",
        })
        parent_ids = {r["ParentObjectID"] for r in rows if r.get("ParentObjectID")}

        results = []
        for object_id in parent_ids:
            spec = self._get(PRICE_SPEC_COLLECTION, {"$filter": f"ObjectID eq '{object_id}'"})
            if not spec:
                continue
            s = spec[0]

            supplier_internal_id, supplier_name = None, None
            props = self._get(PROPERTY_VALUATION_COLLECTION, {"$filter": f"ParentObjectID eq '{object_id}'"})
            for p in props:
                if p.get("PriceSpecificationElementPropertyRefe") == SUPPLIER_PROPERTY_CODE:
                    supplier_internal_id = p.get("PriceSpecificationElementPropertyValu") or None
                    supplier_name = p.get("Description") or None
                    break

            results.append({
                "sap_id": s.get("ObjectID"),
                "supplier_internal_id": supplier_internal_id,
                "supplier_name": supplier_name,
                "price": float(s["Amount"]) if s.get("Amount") not in (None, "") else None,
                "currency": s.get("currencyCode"),
                "unit": s.get("unitCode"),
                "start_date": _parse_odata_date(s.get("StartDate")),
                "end_date": _parse_odata_date(s.get("EndDate")),
                "release_status_code": s.get("ReleaseStatusCode"),
            })
        return results

    def create_price_spec(self, product_id: str, supplier_internal_id: str, price: float, currency: str = "INR") -> None:
        """Creates a new Procurement Price Specification in SAP via SOAP
        MaintainBundle (actionCode 01) - confirmed to auto-release with just
        these fields (see module docstring). Raises SAPPriceSpecError on any
        SOAP fault or connectivity issue."""
        if not self.soap_endpoint or not self.soap_auth:
            raise SAPPriceSpecError("SAP price spec write endpoint is not configured")
        today = date.today().isoformat()
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:glob="http://sap.com/xi/SAPGlobal20/Global">
 <soapenv:Header/>
 <soapenv:Body>
  <glob:ProcurementPriceSpecificationBundleMaintainRequest_sync>
   <BasicMessageHeader/>
   <ProcurementPriceSpecification actionCode="01">
    <ValidityPeriod>
     <IntervalBoundaryTypeCode>1</IntervalBoundaryTypeCode>
     <StartTimePoint><TypeCode>1</TypeCode><Date>{today}</Date></StartTimePoint>
     <EndTimePoint><TypeCode>1</TypeCode><Date>9999-12-31</Date></EndTimePoint>
    </ValidityPeriod>
    <Rate>
     <DecimalValue>{price}</DecimalValue>
     <CurrencyCode>{currency}</CurrencyCode>
    </Rate>
    <PropertyValuation>
     <IdentifyingIndicator>true</IdentifyingIndicator>
     <PriceSpecificationElementPropertyReference><PriceSpecificationElementPropertyID>{PRODUCT_PROPERTY_CODE}</PriceSpecificationElementPropertyID></PriceSpecificationElementPropertyReference>
     <PriceSpecificationElementPropertyValue><ID>{product_id}</ID></PriceSpecificationElementPropertyValue>
    </PropertyValuation>
    <PropertyValuation>
     <IdentifyingIndicator>true</IdentifyingIndicator>
     <PriceSpecificationElementPropertyReference><PriceSpecificationElementPropertyID>{SUPPLIER_PROPERTY_CODE}</PriceSpecificationElementPropertyID></PriceSpecificationElementPropertyReference>
     <PriceSpecificationElementPropertyValue><ID>{supplier_internal_id}</ID></PriceSpecificationElementPropertyValue>
    </PropertyValuation>
   </ProcurementPriceSpecification>
  </glob:ProcurementPriceSpecificationBundleMaintainRequest_sync>
 </soapenv:Body>
</soapenv:Envelope>"""
        try:
            resp = requests.post(
                self.soap_endpoint, data=xml.encode("utf-8"), auth=self.soap_auth,
                headers={"Content-Type": "text/xml; charset=utf-8", "Accept": "text/xml", "SOAPAction": '""'},
                timeout=30,
            )
        except requests.exceptions.RequestException as e:
            raise SAPPriceSpecError(f"Could not reach SAP: {e}")
        if resp.status_code != 200 or "Fault" in resp.text:
            raise SAPPriceSpecError(f"SAP rejected the write (HTTP {resp.status_code}): {resp.text[:300]}")


def bulk_push_erp_prices_to_sap(db, price_spec_client, price_explorer_client, progress_callback=None, is_cancelled=None) -> dict:
    """For every known component (component_master), pushes the ERP's most
    recent real billed price+supplier into SAP as a new Procurement Price
    Specification - but ONLY if that part has no existing Released (status
    3) price in SAP yet, to avoid overwriting/duplicating anything already
    trustworthy there. Skips parts with no ERP history, or whose ERP
    supplier code doesn't match a known SAP supplier (avoids writing an
    orphaned/invalid reference). Uses bounded concurrency across different
    parts (same pattern as bulk_push_to_sap in sap_planning_client.py).
    `is_cancelled` (optional, no-arg callable returning bool) is polled
    after every completed part - if it turns True, no new work is handed
    out and any not-yet-started parts are cancelled (in-flight ones are
    left to finish); already-collected results up to that point are still
    returned, with `cancelled: True`, so a user-initiated Stop reports a
    clean partial summary instead of losing everything done so far.
    Returns {total, pushed, skipped_already_released, skipped_no_erp_data,
    skipped_unknown_supplier, failed: [{product_id, error}],
    pushed_items: [{product_id, supplier, price, currency, bill_date}],
    cancelled}."""
    docs = list(db["component_master"].find({}, {"_id": 1}))
    total = len(docs)
    known_supplier_ids = {
        s["sap_internal_id"] for s in db["suppliers"].find({"sap_internal_id": {"$ne": None}}, {"sap_internal_id": 1})
    }

    def push_one(doc):
        product_id = doc["_id"]
        try:
            existing = price_spec_client.get_price_specs_for_product(product_id)
            if any(s.get("release_status_code") == "3" for s in existing):
                return product_id, "skipped_already_released", None, None

            items = price_explorer_client.search(product_id, lookback_days=180, limit=1)
            match = next((i for i in items if i.get("icode", "").strip().upper() == product_id.strip().upper()), None)
            last = match.get("last") if match else None
            if not last or not last.get("pcode") or last.get("rate") is None:
                return product_id, "skipped_no_erp_data", None, None

            supplier_code = last["pcode"].strip()
            if supplier_code not in known_supplier_ids:
                return product_id, "skipped_unknown_supplier", None, None

            price_spec_client.create_price_spec(product_id, supplier_code, last["rate"], "INR")
            pushed_detail = {
                "product_id": product_id,
                "supplier": last.get("supplier") or supplier_code,
                "price": last["rate"],
                "currency": "INR",
                "bill_date": last.get("bill_date"),
            }
            return product_id, "pushed", None, pushed_detail
        except Exception as e:
            return product_id, "failed", str(e), None

    pushed = skipped_already_released = skipped_no_erp_data = skipped_unknown_supplier = 0
    failed = []
    pushed_items = []
    processed = 0
    cancelled = False

    executor = ThreadPoolExecutor(max_workers=3)
    try:
        futures = {executor.submit(push_one, doc): doc["_id"] for doc in docs}
        for future in as_completed(futures):
            if future.cancelled():
                continue
            product_id, outcome, error, pushed_detail = future.result()
            processed += 1
            if outcome == "pushed":
                pushed += 1
                if pushed_detail:
                    pushed_items.append(pushed_detail)
            elif outcome == "skipped_already_released":
                skipped_already_released += 1
            elif outcome == "skipped_no_erp_data":
                skipped_no_erp_data += 1
            elif outcome == "skipped_unknown_supplier":
                skipped_unknown_supplier += 1
            else:
                failed.append({"product_id": product_id, "error": error})
            if progress_callback:
                progress_callback(processed, total)
            if not cancelled and is_cancelled and is_cancelled():
                cancelled = True
                for f in futures:
                    f.cancel()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return {
        "total": total, "pushed": pushed,
        "skipped_already_released": skipped_already_released,
        "skipped_no_erp_data": skipped_no_erp_data,
        "skipped_unknown_supplier": skipped_unknown_supplier,
        "failed": failed, "pushed_items": pushed_items, "cancelled": cancelled,
    }

