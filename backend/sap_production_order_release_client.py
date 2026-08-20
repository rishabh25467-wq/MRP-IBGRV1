"""Custom OData action client for releasing a SAP Production Order.

SAP has no standard SOAP/OData API to release a Production Order. This
calls a CUSTOM OData service ("productionorderemergent") that the user's
SAP admin built via the key-user OData Modeler (Application and User
Management -> OData Services), exposing the BO's own PSM-released
"Release" action. Custom OData services can only be called by a real
SAP Business User (not the SOAP technical user) - a dedicated business
user was created for this (see SAP_ODATA_BUSINESS_USER/PASSWORD)."""
import re

import requests
from requests.auth import HTTPBasicAuth


class SAPProductionOrderReleaseError(Exception):
    pass


class SAPProductionOrderReleaseClient:
    def __init__(self, base_url: str, username: str, password: str, entity_set: str = "ProductionOrderCollection"):
        self.base_url = base_url.rstrip("/")
        self.auth = HTTPBasicAuth(username, password)
        self.entity_set = entity_set

    def resolve_object_id(self, order_or_proposal_id: str) -> str:
        resp = requests.get(
            f"{self.base_url}/{self.entity_set}",
            params={"$filter": f"ID eq '{order_or_proposal_id}'"},
            auth=self.auth, headers={"Accept": "application/json"}, timeout=30,
        )
        if resp.status_code != 200:
            raise SAPProductionOrderReleaseError(self._error_message(resp))
        results = resp.json().get("d", {}).get("results", [])
        if not results:
            raise SAPProductionOrderReleaseError(f"'{order_or_proposal_id}' not found in {self.entity_set}")
        return results[0]["ObjectID"]

    def release_order(self, order_or_proposal_id: str) -> dict:
        object_id = self.resolve_object_id(order_or_proposal_id)
        session = requests.Session()
        session.auth = self.auth
        token_resp = session.get(f"{self.base_url}/$metadata", headers={"X-CSRF-Token": "Fetch"}, timeout=30)
        csrf_token = token_resp.headers.get("X-CSRF-Token")
        resp = session.post(
            f"{self.base_url}/Release",
            params={"ObjectID": f"'{object_id}'"},
            headers={"X-CSRF-Token": csrf_token, "Accept": "application/json"}, timeout=30,
        )
        if resp.status_code != 200:
            raise SAPProductionOrderReleaseError(self._error_message(resp))
        return {"success": True, "id": order_or_proposal_id, "object_id": object_id}

    def set_source_of_supply(self, proposal_id: str, logistic_relationship_uuid: str) -> dict:
        """Writes the chosen Source of Supply (Production Model) onto a
        Production Proposal before it gets converted into a Request/Order -
        PATCHes SourceOfSupplyLogisticRelationshipUUID + sets
        SourceOfSupplyFixedIndicator=true (a manually-chosen source of
        supply must be marked Fixed or SAP's planning run can recalculate
        and silently override it). Verified working via direct OData PATCH -
        see /app/memory/sap_source_of_supply_dev_spec.md."""
        object_id = self.resolve_object_id(proposal_id)
        session = requests.Session()
        session.auth = self.auth
        token_resp = session.get(f"{self.base_url}/$metadata", headers={"X-CSRF-Token": "Fetch"}, timeout=30)
        csrf_token = token_resp.headers.get("X-CSRF-Token")
        resp = session.patch(
            f"{self.base_url}/{self.entity_set}('{object_id}')",
            headers={"X-CSRF-Token": csrf_token, "Content-Type": "application/json", "Accept": "application/json"},
            json={
                "SourceOfSupplyLogisticRelationshipUUID": logistic_relationship_uuid,
                "SourceOfSupplyFixedIndicator": True,
            },
            timeout=30,
        )
        if resp.status_code not in (200, 204):
            raise SAPProductionOrderReleaseError(self._error_message(resp))
        return {"success": True, "object_id": object_id, "logistic_relationship_uuid": logistic_relationship_uuid}

    @staticmethod
    def _error_message(resp) -> str:
        try:
            return resp.json().get("error", {}).get("message", {}).get("value", resp.text[:300])
        except ValueError:
            return re.sub(r"<[^>]+>", " ", resp.text)[:300] or f"HTTP {resp.status_code}"
