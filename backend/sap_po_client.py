"""SAP Business ByDesign Purchase Order query client - Supplier Portal
Phase 1 (Aug 2026). Lets an approved external vendor see their own open
Purchase Orders inside the portal.

BLOCKED as of Aug 2026: waiting on the user to activate + share the exact
SAP SOAP/OData endpoint (expected `QueryPurchaseOrderQueryIn` or an
equivalent OData analytics report, same pattern as sap_hsn_client.py /
sap_inventory_client.py) - `SAP_SOAP_PO_ENDPOINT` is left blank in .env
until then. `get_open_pos_for_vendor` raises SAPPurchaseOrderNotConfiguredError
so the portal can show a clear "not connected yet" message instead of a
generic crash. Swap in the real request body once the endpoint is
confirmed - the surrounding auth/error-handling shape already matches
every other SAP SOAP client in this codebase (see sap_supplier_client.py)."""
from requests.auth import HTTPBasicAuth


class SAPPurchaseOrderError(Exception):
    pass


class SAPPurchaseOrderNotConfiguredError(SAPPurchaseOrderError):
    """No SAP endpoint has been wired up yet - distinct from a live call
    that fails, so callers/UI can show "not connected yet" rather than a
    generic error."""
    pass


class SAPPurchaseOrderClient:
    def __init__(self, endpoint: str, username: str, password: str):
        self.endpoint = endpoint or None
        self.auth = HTTPBasicAuth(username, password)

    def get_open_pos_for_vendor(self, vendor_code: str) -> list:
        if not self.endpoint:
            raise SAPPurchaseOrderNotConfiguredError(
                "SAP Purchase Order lookup isn't wired up yet - waiting on the SAP SOAP/OData "
                "endpoint (SAP_SOAP_PO_ENDPOINT) to be activated and shared for this tenant."
            )
        # TODO: real QueryPurchaseOrderQueryIn (or equivalent) SOAP/OData
        # call once the endpoint is confirmed - filter by vendor_code,
        # return open line items (PO number, item code, description, PO
        # qty, already-shipped qty, due date).
        raise SAPPurchaseOrderNotConfiguredError("SAP Purchase Order query is not yet implemented")
