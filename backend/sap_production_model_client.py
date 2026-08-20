"""SAP Business ByDesign "Source of Supply" (Production Model) lookup.

Backed by a custom OData service ("productionmodelemergent") the user's SAP
admin built via the key-user OData Modeler (Application and User Management
-> OData Services), exposing: ProductionModel (root, filterable by
MaterialUUID) -> ReleasedPlanningProductionModel -> SourceOfSupplyLogisticRelationship.
The `SourceOfSupplyLogisticRelationship.UUID` returned here is exactly the
value that gets written back to a Production Proposal's own
`SourceOfSupplyLogisticRelationshipUUID` field (confirmed working via direct
OData PATCH - see /app/memory/sap_source_of_supply_dev_spec.md).

Empirically, a material with multiple Consistent BOM revisions has multiple
ProductionModel instances (e.g. HTBS-SPAIN_1, HTBS-SPAIN_2), each with its own
chain of released versions over time - only the one whose LogisticRelationship
OverallLifeCycleStatusCode == "2" (Active) is currently selectable in SAP;
older ones are "4" (Obsolete)."""
import requests
from requests.auth import HTTPBasicAuth

ACTIVE_STATUS_CODE = "2"


class SAPProductionModelError(Exception):
    pass


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and "results" in value:
        return value["results"]
    return [value]


class SAPProductionModelClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.auth = HTTPBasicAuth(username, password)

    def get_source_of_supply_options(self, material_uuid: str) -> list[dict]:
        """Returns one entry per distinct Production Model available for
        this material: {production_model_id, production_model_uuid,
        logistic_relationship_uuid, logistic_relationship_id, is_active,
        priority}. Only the Active released version of each Production
        Model is surfaced (falls back to showing all if none are Active,
        so the picker is never silently empty)."""
        resp = requests.get(
            f"{self.base_url}/ProductionModelCollection",
            auth=self.auth,
            headers={"Accept": "application/json"},
            params={
                "$filter": f"MaterialUUID eq guid'{material_uuid}'",
                "$expand": "ReleasedPlanningProductionModelReleasedPlanningProductionModel/SourceOfSupplySourceOfSupplyLogisticRelationship",
                "$format": "json",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise SAPProductionModelError(f"SAP returned HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if "error" in data:
            raise SAPProductionModelError(data["error"].get("message", {}).get("value", "Unknown OData error"))

        by_model = {}
        for pm in data.get("d", {}).get("results", []):
            model_id = pm.get("ID")
            model_uuid = pm.get("UUID")
            if not model_id or not model_uuid:
                continue
            candidates = []
            for rpm in _as_list(pm.get("ReleasedPlanningProductionModelReleasedPlanningProductionModel")):
                lr = rpm.get("SourceOfSupplySourceOfSupplyLogisticRelationship")
                if not lr or not lr.get("UUID"):
                    continue
                candidates.append({
                    "production_model_id": model_id,
                    "production_model_uuid": model_uuid,
                    "logistic_relationship_uuid": lr["UUID"],
                    "logistic_relationship_id": lr.get("ID"),
                    "is_active": lr.get("OverallLifeCycleStatusCode") == ACTIVE_STATUS_CODE,
                    "priority": lr.get("PriorityValue") or 0,
                })
            if not candidates:
                continue
            active = [c for c in candidates if c["is_active"]]
            chosen = max(active or candidates, key=lambda c: c["priority"])
            by_model[model_id] = chosen

        return sorted(by_model.values(), key=lambda c: c["production_model_id"])
