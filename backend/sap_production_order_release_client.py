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


ORDER_LIFECYCLE_LABELS = {
    "1": "In Preparation", "2": "Released", "3": "Started",
    "4": "Finished", "5": "Closed", "6": "Canceled",
}
# Codes from which "Released" is already true (a Started/Finished/Closed
# order was necessarily Released at some point) - used to verify the
# Release action's real effect instead of trusting a bare HTTP 200.
RELEASED_OR_LATER_CODES = {"2", "3", "4", "5"}


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

    def release_order(self, order_or_proposal_id: str, verify_status: bool = False) -> dict:
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
        released = True
        if verify_status:
            # A 200 here only means SAP accepted the call - it does NOT
            # guarantee the order's actual status flipped (confirmed by real
            # testing: a stale UI screenshot showed "In Preparation" right
            # after a "successful" call). Verify the real LifeCycleStatusCode
            # (only meaningful on the real ProductionOrder entity, i.e. the
            # final-order-release call site - not the earlier Proposal
            # "Request Production" trigger, which has no such field).
            try:
                status = self.get_life_cycle_status(order_or_proposal_id)
                released = status["code"] in RELEASED_OR_LATER_CODES
            except SAPProductionOrderReleaseError:
                pass  # status field not available - fall back to trusting the 200
        return {"success": released, "id": order_or_proposal_id, "object_id": object_id}

    def list_ids_by_status(self, life_cycle_status_code: str) -> set:
        """Global (not site-scoped - this entity doesn't expose Site) list
        of Order IDs currently at a given LifeCycleStatusCode. Used to
        detect brand-new "In Preparation" orders that our own
        create-and-release job must actively Release itself - confirmed
        live that SAP does NOT auto-assign a Production Lot (and therefore
        never shows up in the SOAP open-lots poll) until an order is
        actually released, so relying on Lot-polling alone can wait
        forever on an order stuck at "In Preparation"."""
        resp = requests.get(
            f"{self.base_url}/{self.entity_set}",
            params={"$filter": f"LifeCycleStatusCode eq '{life_cycle_status_code}'", "$format": "json", "$select": "ID"},
            auth=self.auth, headers={"Accept": "application/json"}, timeout=60,
        )
        if resp.status_code != 200:
            raise SAPProductionOrderReleaseError(self._error_message(resp))
        return {r["ID"] for r in resp.json().get("d", {}).get("results", [])}

    def get_life_cycle_status(self, order_id: str) -> dict:
        """Real, authoritative order status (verified live against SAP's
        source of truth - matches the SOAP Production Lot status exactly,
        unlike the separate, staler "list" status on
        ProductionOrderRequestSegmentReference). Returns {"code", "label"}."""
        resp = requests.get(
            f"{self.base_url}/{self.entity_set}",
            params={"$filter": f"ID eq '{order_id}'", "$format": "json"},
            auth=self.auth, headers={"Accept": "application/json"}, timeout=30,
        )
        if resp.status_code != 200:
            raise SAPProductionOrderReleaseError(self._error_message(resp))
        results = resp.json().get("d", {}).get("results", [])
        if not results or not results[0].get("LifeCycleStatusCode"):
            raise SAPProductionOrderReleaseError(f"LifeCycleStatusCode not available for '{order_id}'")
        code = results[0]["LifeCycleStatusCode"]
        return {"code": code, "label": ORDER_LIFECYCLE_LABELS.get(code, code)}

    def is_released(self, order_id: str) -> bool:
        """True if the order's real status is Released or any later stage
        (Started/Finished/Closed all imply it was Released at some point).
        Raises SAPProductionOrderReleaseError if the status field itself is
        unavailable - callers should treat that as "unknown", not False."""
        return self.get_life_cycle_status(order_id)["code"] in RELEASED_OR_LATER_CODES

    def get_requested_material(self, order_id: str) -> dict | None:
        """Reads the order's own ProductionOrderRequestSegmentReference -
        its ID field is "{material_id}_{n}" and RequestedQuantity is the
        originally-requested output qty, BOTH available immediately (even
        while the order is still LifeCycleStatusCode=1 "In Preparation",
        confirmed live) since they describe the conversion request itself,
        not the fulfilled Lot. Used to verify a self-detected "new In
        Preparation" order really belongs to THIS job's material before
        ever touching it - list_ids_by_status() is tenant-wide, not
        site/material-scoped, so any concurrent SAP activity (another
        user, another job, SAP's own MRP run) creating an unrelated order
        at the same moment would otherwise get falsely claimed as "ours".
        Real incident (Aug 2026): Proposal 224458 for BK-0021 (site P2,
        qty 2 EA) got wrongly tagged onto Order 70151, which turned out to
        actually be material 5989828 @ P9, qty 147 EA - a completely
        unrelated order that just happened to appear "In Preparation" in
        the same polling window. Returns None if the segment reference
        isn't available/parseable (caller should treat that as "cannot
        verify, don't risk it" - not as a match)."""
        resp = requests.get(
            f"{self.base_url}/{self.entity_set}",
            params={"$filter": f"ID eq '{order_id}'", "$format": "json", "$expand": "ProductionOrderRequestSegmentReference"},
            auth=self.auth, headers={"Accept": "application/json"}, timeout=30,
        )
        if resp.status_code != 200:
            raise SAPProductionOrderReleaseError(self._error_message(resp))
        results = resp.json().get("d", {}).get("results", [])
        if not results:
            return None
        segments = results[0].get("ProductionOrderRequestSegmentReference") or []
        if not segments:
            return None
        seg_id = segments[0].get("ID") or ""
        material_id = seg_id.rsplit("_", 1)[0] if "_" in seg_id else seg_id
        try:
            quantity = float(segments[0].get("RequestedQuantity"))
        except (TypeError, ValueError):
            quantity = None
        return {"material_id": material_id, "quantity": quantity}

    def tag_with_proposal_id(self, order_id: str, proposal_id: str) -> None:
        """Best-effort: writes the source Proposal ID onto the Order's own
        Z_ProductionProposalID custom field (added Aug 2026) so it's
        visible directly in SAP's native UI, and so future lookups of
        "which Order did Proposal X become" are instant instead of
        needing the polling fallback. Never raises - a failure here must
        never break the create-and-release job, this is pure traceability."""
        object_id = self.resolve_object_id(order_id)
        session = requests.Session()
        session.auth = self.auth
        token_resp = session.get(f"{self.base_url}/$metadata", headers={"X-CSRF-Token": "Fetch"}, timeout=30)
        csrf_token = token_resp.headers.get("X-CSRF-Token")
        session.patch(
            f"{self.base_url}/{self.entity_set}('{object_id}')",
            json={"Z_ProductionProposalIDcontent_SDK": str(proposal_id)},
            headers={"X-CSRF-Token": csrf_token, "Accept": "application/json", "Content-Type": "application/json"},
            timeout=30,
        )

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
