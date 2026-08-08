"""SAP Business ByDesign custom OData service `pricespecificationemergent`
(Business Object: ProcurementPriceSpecification) - reads real purchasing
prices already maintained in SAP (SAP's equivalent of a Purchasing Info
Record), so the Suppliers page can show "who supplies Product X at what
price" as it already exists in SAP, instead of requiring manual re-entry.

Read-only by design (this session): the underlying data is a generic
condition-technique/EAV model (Product ID and Supplier are stored as
`PriceSpecificationElementPropertyValuation` rows keyed by a property code
like `CND_PRODUCT_ID`, not plain fields), and the standard SOAP write
service (`ManageProcurementPriceSpecificIn`) risks creating incomplete
records without confirming all fields SAP's purchasing team actually
requires - so writing new price specs back into SAP is deliberately NOT
implemented yet."""
import requests
from requests.auth import HTTPBasicAuth

PROPERTY_VALUATION_COLLECTION = "ProcurementPriceSpecificationPropertyValuationCollection"
PRICE_SPEC_COLLECTION = "ProcurementPriceSpecificationCollection"
PRODUCT_PROPERTY_CODE = "CND_PRODUCT_ID"


class SAPPriceSpecError(Exception):
    pass


def _parse_odata_date(value):
    # SAP OData JSON dates look like "/Date(1682985600000)/"
    if not value or not value.startswith("/Date("):
        return None
    ms = int(value[6:-2])
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


class SAPPriceSpecClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.auth = HTTPBasicAuth(username, password)

    def _get(self, collection: str, params: dict) -> list:
        resp = requests.get(
            f"{self.base_url}/{collection}",
            auth=self.auth,
            timeout=30,
            headers={"Accept": "application/json"},
            params={**params, "$format": "json"},
        )
        if resp.status_code != 200:
            raise SAPPriceSpecError(f"SAP price specification service returned HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if "error" in data:
            raise SAPPriceSpecError(data["error"].get("message", {}).get("value", "Unknown OData error"))
        results = data.get("d", {}).get("results", data.get("d", {}))
        return results if isinstance(results, list) else [results]

    def get_price_specs_for_product(self, product_id: str) -> list:
        """Returns real SAP purchasing price records for a Product ID:
        [{sap_id, supplier_uuid, price, currency, unit, start_date,
        end_date, release_status_code}, ...]. Empty list if SAP has none -
        that's a normal outcome, not an error."""
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
            results.append({
                "sap_id": s.get("ObjectID"),
                "supplier_uuid": (s.get("BusinessPartnerUUID") or "").lower() or None,
                "price": float(s["Amount"]) if s.get("Amount") not in (None, "") else None,
                "currency": s.get("currencyCode"),
                "unit": s.get("unitCode"),
                "start_date": _parse_odata_date(s.get("StartDate")),
                "end_date": _parse_odata_date(s.get("EndDate")),
                "release_status_code": s.get("ReleaseStatusCode"),
            })
        return results
