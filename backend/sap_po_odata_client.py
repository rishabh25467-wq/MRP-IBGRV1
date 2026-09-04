"""SAP Business ByDesign Purchase Order creation via the tenant's own
custom OData service `khpurchaseorder` - NOT the standard
ManagePurchaseOrderIn SOAP service (see sap_po_write_client.py's
docstring for the full history of why the SOAP approach could never
get "Business Residence"/"Purchasing Unit" to actually persist).

Sep 5 2026: user shared their OWN working C#/.NET integration code that
creates POs successfully in this exact SAP tenant using ONLY this OData
service - this client is a direct Python port of that proven flow:
  1. POST PurchaseOrderCollection (deep insert: header fields + Item[] +
     PurchasingUnit + Supplier navigation - all settable at create time,
     confirmed via $metadata: their PartyID is sap:creatable="true").
  2. GET .../PurchaseOrderCollection('<id>')/BillToParty, then PATCH
     BillToPartyCollection('<partyObjectId>') with {"PartyID": ...} -
     BillToParty/BuyerParty's PartyID is sap:creatable="false" (per
     $metadata) but sap:updatable="true", so it MUST go through this
     separate get-then-patch step, unlike PurchasingUnit/Supplier.
  3. Same get-then-patch for BuyerParty.
No EmployeeResponsible is set (the reference C# code doesn't set it
either - SAP appears to default it from whichever technical user's
Basic Auth credentials are used).
"""
import requests
from requests.auth import HTTPBasicAuth


class SAPPurchaseOrderODataError(Exception):
    pass


class SAPPurchaseOrderODataNotConfiguredError(SAPPurchaseOrderODataError):
    pass


class SAPPurchaseOrderODataClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 60):
        self.base_url = (base_url or "").rstrip("/") or None
        self.auth = HTTPBasicAuth(username, password)
        self.timeout = timeout

    def _csrf_token(self, session):
        r = session.get(
            f"{self.base_url}/PurchaseOrderCollection",
            headers={"x-csrf-token": "fetch"}, timeout=self.timeout,
        )
        return r.headers.get("x-csrf-token")

    def _patch_party(self, session, token, po_object_id, nav_name, party_id):
        """BillToParty/BuyerParty: PartyID isn't creatable on the deep
        insert, only updatable afterward - get the nested entity's own
        ObjectID first, then PATCH that entity directly. Best-effort:
        doesn't raise, so one failed party patch doesn't undo an
        otherwise-successful PO creation."""
        get_url = f"{self.base_url}/PurchaseOrderCollection('{po_object_id}')/{nav_name}"
        try:
            r = session.get(get_url, params={"$format": "json"}, timeout=self.timeout)
            party_object_id = r.json()["d"]["results"]["ObjectID"]
        except Exception:
            return False
        patch_url = f"{self.base_url}/{nav_name}Collection('{party_object_id}')"
        try:
            r2 = session.request(
                "PATCH", patch_url, json={"PartyID": party_id},
                headers={"x-csrf-token": token}, timeout=self.timeout,
            )
            return r2.ok
        except Exception:
            return False

    def create_purchase_order(
        self, company_code: str, purchase_unit_site: str, supplier_code: str,
        bill_to_company_code: str, po_date: str, currency: str, items: list,
        pr_number: str = None, cash_discount_terms_code: str = None,
    ) -> dict:
        """items: [{"product_id", "description", "quantity",
        "unit_of_measure", "unit_price", "delivery_date" (YYYY-MM-DD),
        "site_id"}, ...].
        Returns {"po_number": str, "po_uuid": str (ObjectID), "raw_xml": str}.
        Raises SAPPurchaseOrderODataError on any rejection."""
        if not self.base_url:
            raise SAPPurchaseOrderODataNotConfiguredError(
                "SAP Purchase Order creation isn't wired up yet - SAP_ODATA_PO_BASE_URL is not set."
            )
        session = requests.Session()
        session.auth = self.auth
        token = self._csrf_token(session)

        item_payload = []
        for it in items:
            description = str(it.get("description") or it["product_id"])[:40]
            item_payload.append({
                "ProductID": str(it["product_id"]),
                "ProductCategoryInternalID": "",
                "Description": description,
                "ItemTypeCode": "18",
                "DirectMaterialIndicator": True,
                "ThirdPartyDealIndicator": False,
                "Quantity": str(it["quantity"]),
                "QuantityUnitCode": it["unit_of_measure"] or "EA",
                "ListUnitPriceAmount": f"{float(it['unit_price']):.2f}",
                "DeliveryStartDateTime": f"{it['delivery_date']}T00:00:00",
                "DeliveryEndDateTime": f"{it['delivery_date']}T00:00:00",
                "GoodsAndServiceReceiptRequirementCode": "01",
                "EvaluatedReceiptSettlementIndicator": False,
                "InvoiceRequirementCode": "01",
                "ItemShipToLocation": {"LocationID": str(it["site_id"])},
            })

        # Sep 5 2026: key order here matches the user's proven-working
        # C# reference exactly (CurrencyCode, PODate_KUT,
        # PortalPRNumber_KUT, BusinesResidence_SDK, PaymentTerms,
        # PurchasingUnit, Supplier, Item) - SAP's deep-insert parser may
        # be order-sensitive (same lesson learned earlier with the SOAP
        # service's element sequence), so this isn't just cosmetic.
        order_data = {"CurrencyCode": currency, "PODate_KUT": f"{po_date}T00:00:00"}
        if pr_number:
            order_data["PortalPRNumber_KUT"] = pr_number
        order_data["BusinesResidence_SDK"] = purchase_unit_site
        if cash_discount_terms_code:
            order_data["PaymentTerms"] = {"PaymentTermsCode": cash_discount_terms_code}
        order_data["PurchasingUnit"] = {"PartyID": f"{purchase_unit_site}-PUR"}
        order_data["Supplier"] = {"PartyID": supplier_code}
        order_data["Item"] = item_payload

        headers = {"x-csrf-token": token, "Content-Type": "application/json", "Accept": "application/json"}
        resp = session.post(
            f"{self.base_url}/PurchaseOrderCollection", json=order_data, headers=headers, timeout=self.timeout,
        )
        if not resp.ok:
            raise SAPPurchaseOrderODataError(
                f"SAP rejected the Purchase Order (HTTP {resp.status_code}): {resp.text[:500]}"
            )
        try:
            body = resp.json()["d"]
            results = body["results"] if "results" in body else body
            po_object_id = results["ObjectID"]
            po_number = results["ID"]
        except (ValueError, KeyError) as e:
            raise SAPPurchaseOrderODataError(
                f"SAP returned an unexpected response creating the Purchase Order: {resp.text[:500]}"
            ) from e

        self._patch_party(session, token, po_object_id, "BillToParty", bill_to_company_code)
        self._patch_party(session, token, po_object_id, "BuyerParty", company_code)

        return {"po_number": po_number, "po_uuid": po_object_id, "raw_xml": resp.text}
