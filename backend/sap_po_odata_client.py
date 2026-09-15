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

# Sep 14 2026, Job Work PO ask - (item_type_code, product_category) per
# po_type.
# Sep 15 2026, Capital PO ask - IMAT (Individual Material/Fixed Asset)
# account assignment confirmed live against this tenant's real $metadata
# (AccountAssignmentTypeCode "IMAT" is in
# ItemAccountAssignmentDetailsAccountAssignmentTypeCodeCollection, and
# IndividualMaterialID is a creatable field on ItemAccountAssignmentDetails).
# Product Category "CONSUMABLES" reused here TEMPORARILY (user's explicit
# fallback choice, Sep 15 2026) - pending the tenant's real Fixed-Asset-
# mapped category code.
PO_TYPE_ITEM_CONFIG = {
    "service": ("19", "CONSUMABLES"),
    "jobwork": ("18", "JOBWORK"),
    "capital": ("18", "CONSUMABLES"),
}


class SAPPurchaseOrderODataError(Exception):
    pass


class SAPPurchaseOrderODataNotConfiguredError(SAPPurchaseOrderODataError):
    pass


def _error_message_for(resp) -> str:
    """Sep 14 2026, same fix/reason as sap_outbound_delivery_client.py's
    helper - a 401 here means the itadmin SAP password was rotated,
    not a real business rejection of this Purchase Order."""
    if resp.status_code == 401:
        return "SAP login failed (401) - the SAP password for this account may have been changed in SAP. Please verify the SAP_USERNAME/SAP_PASSWORD credentials with your SAP admin."
    return f"SAP rejected the Purchase Order (HTTP {resp.status_code}): {resp.text[:500]}"


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

    def _create_order(self, session, token: str, order_data: dict, kind: str) -> dict:
        """Shared by create_purchase_order/create_service_purchase_order
        below. Sep 14 2026 fix (real incident: a Service PO creation
        crashed with an UNCAUGHT `requests.exceptions.ReadTimeout` after
        SAP took >60s to respond - Cloudflare then returned a raw
        "invalid/incomplete response" to the browser instead of a clean
        error, since the exception never reached FastAPI's own error
        handling). A write POST gets a longer timeout than reads (SAP's
        own processing of a deep-insert with nested Item/
        ItemAccountAssignment can genuinely take over 60s), and ANY
        network failure is now caught and converted into a normal
        SAPPurchaseOrderODataError - callers already handle that
        cleanly (HTTP 422 with a real message) instead of the whole
        request just dying."""
        headers = {"x-csrf-token": token, "Content-Type": "application/json", "Accept": "application/json"}
        try:
            resp = session.post(
                f"{self.base_url}/PurchaseOrderCollection", json=order_data, headers=headers, timeout=max(self.timeout, 120),
            )
        except requests.exceptions.RequestException as e:
            raise SAPPurchaseOrderODataError(
                f"SAP did not respond in time while creating the {kind} - it may or may not have actually been "
                f"created on SAP's side. Please check directly in SAP before retrying, to avoid creating a duplicate."
            ) from e
        if not resp.ok:
            raise SAPPurchaseOrderODataError(_error_message_for(resp))
        try:
            body = resp.json()["d"]
            results = body["results"] if "results" in body else body
            return {"po_number": results["ID"], "po_uuid": results["ObjectID"], "raw_xml": resp.text}
        except (ValueError, KeyError) as e:
            raise SAPPurchaseOrderODataError(
                f"SAP returned an unexpected response creating the {kind}: {resp.text[:500]}"
            ) from e

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

        result = self._create_order(session, token, order_data, "Purchase Order")
        self._patch_party(session, token, result["po_uuid"], "BillToParty", bill_to_company_code)
        self._patch_party(session, token, result["po_uuid"], "BuyerParty", company_code)
        return result

    def _create_service_item(self, session, token: str, po_object_id: str, it: dict, item_type_code: str, product_category: str, po_type: str):
        """Sep 14 2026 fix (real live SAP rejection hit on PR 125257):
        creating Service items INSIDE the same deep-insert as the header
        (like create_purchase_order does) fails with a real SAP error -
        "Intercompany posting not allowed; Cost Center belongs to
        different company" - because the Cost Center account assignment
        is validated the instant the deep-insert is processed, but
        BuyerParty (which carries the PO's real Company) is only
        PATCHable AFTER the PO already exists, so at that exact
        validation moment the PO is still sitting on SAP's own default
        company for this technical user, not the real one. Confirmed via
        live $metadata: ItemCollection is independently creatable with a
        settable ParentObjectID (the PO's own ObjectID) - so Service
        items are now created as a SEPARATE POST, after BuyerParty has
        already been patched to the correct company.

        item_type_code/product_category are parametrized (Sep 14 2026,
        Job Work PO ask) - "19"/CONSUMABLES for Service, "18"/JOBWORK for
        Job Work (same CC account assignment mechanism as Service, per
        user's explicit ask - "manual ledger selection... same as service
        PO"). Live-verified for JOBWORK the same way Service was.

        Sep 15 2026, Capital PO ask - "capital" lines use AccountAssignmentTypeCode
        "IMAT" (Individual Material/Fixed Asset) + IndividualMaterialID
        (the exact SAP Master Fixed Asset ID the user picked from the
        live list, via QueryObjectDescriptionIn - see sap_fixed_asset_client.py)
        instead of Cost Center + GL Account - per user's explicit ask,
        this posts cost against an EXISTING SAP-maintained asset, never
        creates a new one from here. UNVERIFIED for a real live write -
        first real Capital PO submission will confirm end to end."""
        description = str(it.get("description") or "Service")[:40]
        if po_type == "capital":
            account_assignment_details = {
                "AccountAssignmentTypeCode": "IMAT",
                "IndividualMaterialID": str(it["fixed_asset_id"]),
                "Percent": "100",
                "Quantity": str(it["quantity"]),
                "QuantityUnitCode": it["unit_of_measure"] or "EA",
            }
        else:
            account_assignment_details = {
                "AccountAssignmentTypeCode": "CC",
                "CostCentreID": it.get("cost_centre_id"),
                "GeneralLedgerAccountAliasCode": str(it["gl_account_code"]),
                "Percent": "100",
                "Quantity": str(it["quantity"]),
                "QuantityUnitCode": it["unit_of_measure"] or "EA",
            }
        item_data = {
            "ParentObjectID": po_object_id,
            "ProductCategoryInternalID": product_category,
            "Description": description,
            "ItemTypeCode": item_type_code,
            "DirectMaterialIndicator": False,
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
            "ItemAccountAssignment": {
                "ItemAccountAssignmentDetails": [account_assignment_details],
            },
        }
        headers = {"x-csrf-token": token, "Content-Type": "application/json", "Accept": "application/json"}
        try:
            resp = session.post(f"{self.base_url}/ItemCollection", json=item_data, headers=headers, timeout=max(self.timeout, 120))
        except requests.exceptions.RequestException as e:
            raise SAPPurchaseOrderODataError(
                f"SAP did not respond in time while adding line item '{description}' - the PO header may "
                f"already exist without this line. Please check directly in SAP before retrying."
            ) from e
        if not resp.ok:
            raise SAPPurchaseOrderODataError(_error_message_for(resp))

    def create_service_purchase_order(
        self, company_code: str, purchase_unit_site: str, supplier_code: str,
        bill_to_company_code: str, po_date: str, currency: str, items: list,
        pr_number: str = None, cash_discount_terms_code: str = None, po_type: str = "service",
    ) -> dict:
        """Sep 14 2026, user's explicit ask - a Service PO line has NO
        Product Master entry at all (just a free-text description, e.g.
        "SECURITY CHARGES"), needs Item Type=Service, and needs a manual
        GL Account + Cost Center (Account Assignment) instead of a
        product's own default account determination. Confirmed live
        against this tenant's real $metadata (Sep 14 2026):
          - ItemTypeCode "19" = Service (queried
            ItemItemTypeCodeCollection live: 18=Material, 19=Service,
            20=Limit, 84=Expense).
          - ItemAccountAssignment/ItemAccountAssignmentDetails IS
            creatable (Item 1:1 ItemAccountAssignment 1:*
            ItemAccountAssignmentDetails). AccountAssignmentTypeCode
            "CC" = Cost Center (queried
            ItemAccountAssignmentDetailsAccountAssignmentTypeCodeCollection
            live).
          - GeneralLedgerAccountAliasCode is the GL Account field -
            user's explicit choice: Cost Center always equals the PO's
            own Bill-To code.
        HSN/SAC has NO field anywhere on this custom OData service (
        checked the full $metadata - not on Item, not as an extension
        field) - see server.py's best-effort SOAP follow-up call
        (sap_po_write_client.set_item_hsn_code) for how that's handled
        instead, completely separately from this OData create.

        Sep 14 2026, SAME-DAY FIX (real live rejection on PR 125257,
        "Intercompany posting not allowed; Cost Center belongs to
        different company"): unlike create_purchase_order, header +
        items are now created in 3 SEPARATE steps (header -> patch
        BillToParty/BuyerParty -> then items), so the Cost Center's
        company check happens AFTER the real company is already set.
        See _create_service_item's own docstring for the full root
        cause. Header creation failing still raises normally (nothing
        written); an item failing after the header succeeded raises
        with the real po_number already visible in the error/logs so
        the caller (server.py) can decide how to proceed - the PO
        header exists in SAP at that point, just missing that line.

        Sep 14 2026, Job Work PO ask - `po_type` ("service"|"jobwork")
        picks ItemTypeCode + ProductCategoryInternalID per
        PO_TYPE_ITEM_CONFIG below; Job Work reuses the exact same CC
        (Cost Center) account assignment as Service, per user's explicit
        ask. "capital" is intentionally NOT included yet - the Fixed
        Asset (IMAT) account assignment it needs is a different SAP
        mechanism not yet built.

        items: [{"description", "quantity", "unit_of_measure",
        "unit_price", "delivery_date" (YYYY-MM-DD), "site_id",
        "gl_account_code"}, ...]. Returns the same shape as
        create_purchase_order."""
        if not self.base_url:
            raise SAPPurchaseOrderODataNotConfiguredError(
                "SAP Purchase Order creation isn't wired up yet - SAP_ODATA_PO_BASE_URL is not set."
            )
        item_type_code, product_category = PO_TYPE_ITEM_CONFIG.get(po_type, PO_TYPE_ITEM_CONFIG["service"])
        session = requests.Session()
        session.auth = self.auth
        token = self._csrf_token(session)

        order_data = {"CurrencyCode": currency, "PODate_KUT": f"{po_date}T00:00:00"}
        if pr_number:
            order_data["PortalPRNumber_KUT"] = pr_number
        order_data["BusinesResidence_SDK"] = purchase_unit_site
        if cash_discount_terms_code:
            order_data["PaymentTerms"] = {"PaymentTermsCode": cash_discount_terms_code}
        order_data["PurchasingUnit"] = {"PartyID": f"{purchase_unit_site}-PUR"}
        order_data["Supplier"] = {"PartyID": supplier_code}

        result = self._create_order(session, token, order_data, "Service Purchase Order")
        self._patch_party(session, token, result["po_uuid"], "BillToParty", bill_to_company_code)
        self._patch_party(session, token, result["po_uuid"], "BuyerParty", company_code)

        for it in items:
            it_with_cc = {**it, "cost_centre_id": bill_to_company_code}
            try:
                self._create_service_item(session, token, result["po_uuid"], it_with_cc, item_type_code, product_category, po_type)
            except SAPPurchaseOrderODataError as e:
                raise SAPPurchaseOrderODataError(
                    f"Service Purchase Order {result['po_number']} was created in SAP but adding line "
                    f"'{it.get('description')}' failed: {e}. The PO header exists in SAP - please check it directly "
                    f"(and add/fix this line manually) rather than retrying, to avoid a duplicate PO."
                ) from e
        return result
    def list_gl_accounts(self) -> list:
        """Sep 14 2026, user's explicit ask ("GL... manually selected...
        we then need... a GL list"). Queried live against this tenant -
        284 real GL accounts (confirmed via
        ItemAccountAssignmentDetailsGeneralLedgerAccountAliasCodeCollection).
        Small, rarely-changing reference list - server.py caches this in
        Mongo rather than calling SAP on every request."""
        if not self.base_url:
            raise SAPPurchaseOrderODataNotConfiguredError(
                "SAP Purchase Order creation isn't wired up yet - SAP_ODATA_PO_BASE_URL is not set."
            )
        session = requests.Session()
        session.auth = self.auth
        resp = session.get(
            f"{self.base_url}/ItemAccountAssignmentDetailsGeneralLedgerAccountAliasCodeCollection",
            params={"$format": "json", "$top": "5000"}, timeout=self.timeout,
        )
        if not resp.ok:
            raise SAPPurchaseOrderODataError(_error_message_for(resp))
        results = resp.json()["d"]["results"]
        return [{"code": r["Code"], "description": r.get("Description") or r["Code"]} for r in results]
