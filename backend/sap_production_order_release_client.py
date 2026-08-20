"""Custom OData action client for releasing a SAP Production Order.

SAP has no standard SOAP/OData API to release a Production Order. This
calls a CUSTOM OData service ("productionorderemergent") that the user's
SAP admin built via the key-user OData Modeler (Application and User
Management -> OData Services), exposing the BO's own PSM-released
"Release" action. Custom OData services can only be called by a real
SAP Business User (not the SOAP technical user) - a dedicated business
user was created for this (see SAP_ODATA_BUSINESS_USER/PASSWORD)."""
import re
import time
from datetime import datetime, timezone

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

    def create_with_source_of_supply(self, material_uuid: str, supply_planning_area_uuid: str, quantity: float,
                                      unit_code: str, availability_datetime, logistic_relationship_uuid: str,
                                      max_wait_seconds: int = 30, poll_interval: int = 3) -> str:
        """Creates a Production Proposal via the "ProductionPlanningOrderCreate"
        custom OData action (the BO's own native Create action, exposed as
        a Function Import) so a specific Source of Supply (Production
        Model) can be forced at creation time - SAP rejects setting
        SourceOfSupplyLogisticRelationshipUUID via PATCH on an existing
        Proposal (confirmed: this field is create-time-only). Verified
        live: two identical create calls for the same material+site with
        different logistic_relationship_uuid values produced Proposals
        using two different models, one of which SAP would NOT have
        auto-picked on its own.

        The action's own HTTP response is just a bare boolean (no created
        object reference) - the new Proposal is located by diffing the
        entity set filtered by SourceOfSupplyLogisticRelationshipUUID
        before/after the call. Returns the new Proposal's SAP ID (string)."""
        availability_datetime = availability_datetime or datetime.now(timezone.utc)
        avail_str = availability_datetime.strftime("%Y-%m-%dT%H:%M:%SZ")
        explosion_str = availability_datetime.strftime("%Y-%m-%dT%H:%M:%S")

        session = requests.Session()
        session.auth = self.auth

        def _matching_ids():
            resp = session.get(
                f"{self.base_url}/{self.entity_set}",
                params={
                    "$filter": f"SourceOfSupplyLogisticRelationshipUUID eq guid'{logistic_relationship_uuid}'",
                    "$format": "json",
                },
                timeout=30,
            )
            if resp.status_code != 200:
                raise SAPProductionOrderReleaseError(self._error_message(resp))
            return {r["ID"] for r in resp.json().get("d", {}).get("results", [])}

        before_ids = _matching_ids()

        token_resp = session.get(f"{self.base_url}/$metadata", headers={"X-CSRF-Token": "Fetch"}, timeout=30)
        csrf_token = token_resp.headers.get("X-CSRF-Token")
        params = {
            "SourceOfSupplyLogisticRelationshipUUID": f"guid'{logistic_relationship_uuid}'",
            "MainMaterialOutputSupplyPlanningAreaUUID": f"guid'{supply_planning_area_uuid}'",
            "MainMaterialOutputMaterialUUID": f"guid'{material_uuid}'",
            "MainMaterialOutputAvailabilityDateTime": f"datetimeoffset'{avail_str}'",
            "MainMaterialOutputQuantityTypeCode": f"'{unit_code}'",
            # SAP OData v2 rejects a bare decimal literal like "1.0" with
            # "Malformed URI literal syntax" (confirmed via live testing) -
            # Edm.Decimal literals require the 'm' suffix whenever a
            # decimal point is present. Always appending it is safe for
            # whole numbers too (e.g. "1m" is accepted, same as "1").
            "MainMaterialOutputQuantity": f"{quantity}m",
            "SourceOfSupplyExplosionDate": f"datetime'{explosion_str}'",
            "FixedIndicator": "true",
        }
        resp = session.post(
            f"{self.base_url}/ProductionPlanningOrderCreate",
            params=params,
            headers={"X-CSRF-Token": csrf_token, "Accept": "application/json"},
            timeout=45,
        )
        if resp.status_code != 200:
            raise SAPProductionOrderReleaseError(self._error_message(resp))
        if not resp.json().get("d", {}).get("results", {}).get("ProductionPlanningOrderCreate"):
            raise SAPProductionOrderReleaseError("SAP reported the Create action did not succeed")

        for _ in range(max(1, max_wait_seconds // poll_interval)):
            time.sleep(poll_interval)
            new_ids = _matching_ids() - before_ids
            if new_ids:
                return next(iter(new_ids))
        raise SAPProductionOrderReleaseError("Proposal was created but could not be located afterward (SAP indexing delay) - check SAP directly before retrying")

    @staticmethod
    def _error_message(resp) -> str:
        try:
            return resp.json().get("error", {}).get("message", {}).get("value", resp.text[:300])
        except ValueError:
            return re.sub(r"<[^>]+>", " ", resp.text)[:300] or f"HTTP {resp.status_code}"
