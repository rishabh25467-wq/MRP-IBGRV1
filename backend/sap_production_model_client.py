"""SAP Business ByDesign "Source of Supply" (Production Model) lookup.

Backed by a custom OData service ("productionmodelemergent") the user's SAP
admin built via the key-user OData Modeler (Application and User Management
-> OData Services), exposing: ProductionModel (root, filterable by
MaterialUUID, with Description/languageCode) -> ReleasedPlanningProductionModel
-> SourceOfSupplyLogisticRelationship, plus ProductionModel ->
ProductionModelSupplyPlanningArea (which site(s) a given model is valid at)
and a standalone SupplyPlanningArea entity (ID -> UUID).

IMPORTANT design decision (per user): a Production Model determines its
site, not the other way around - Site is NOT an independent filter the user
sets first. So this returns one option per (model, site) combination for the
whole material, regardless of any site already typed in the form; the
frontend lets the user pick a combo and auto-fills Site from it. `site_id`
is still accepted as an optional narrowing filter for convenience.

The `SourceOfSupplyLogisticRelationship.UUID` returned here is the value
that must be passed to ProductionPlanningOrderCreate (see
sap_production_order_release_client.create_with_source_of_supply) - SAP
rejects it on PATCH of an existing Proposal, it is create-time-only."""
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

    def get_supply_planning_area_uuid(self, site_id: str) -> str:
        resp = requests.get(
            f"{self.base_url}/SupplyPlanningAreaCollection",
            auth=self.auth,
            headers={"Accept": "application/json"},
            params={"$filter": f"ID eq '{site_id}'", "$format": "json"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise SAPProductionModelError(f"SAP returned HTTP {resp.status_code}: {resp.text[:300]}")
        results = resp.json().get("d", {}).get("results", [])
        if not results:
            raise SAPProductionModelError(f"Supply Planning Area '{site_id}' not found in SAP")
        return results[0]["UUID"]

    def _all_supply_planning_areas(self) -> dict:
        """UUID (uppercased) -> Site ID, for the whole tenant. Small
        dataset (a handful of sites), fetched fresh each call - cheap."""
        resp = requests.get(
            f"{self.base_url}/SupplyPlanningAreaCollection",
            auth=self.auth,
            headers={"Accept": "application/json"},
            params={"$format": "json"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise SAPProductionModelError(f"SAP returned HTTP {resp.status_code}: {resp.text[:300]}")
        return {r["UUID"].upper(): r["ID"] for r in resp.json().get("d", {}).get("results", []) if r.get("UUID")}

    def get_source_of_supply_options(self, material_uuid: str, site_id: str = None) -> list[dict]:
        """Returns one entry per (Production Model, valid Site) combination
        for this material: {production_model_id, description,
        logistic_relationship_uuid, logistic_relationship_id, site_id,
        is_active, priority}. Only the Active released version of each
        Production Model is surfaced. If `site_id` is given, combinations
        for other sites are excluded - otherwise every valid site for
        every model is returned, since choosing a model also determines
        its site (per SAP's design, not an independent user choice)."""
        site_spa_uuid = self.get_supply_planning_area_uuid(site_id) if site_id else None
        spa_uuid_to_id = self._all_supply_planning_areas()

        resp = requests.get(
            f"{self.base_url}/ProductionModelCollection",
            auth=self.auth,
            headers={"Accept": "application/json"},
            params={
                "$filter": f"MaterialUUID eq guid'{material_uuid}'",
                "$expand": "ReleasedPlanningProductionModelReleasedPlanningProductionModel/SourceOfSupplySourceOfSupplyLogisticRelationship,ProductionModelSupplyPlanningArea",
                "$format": "json",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise SAPProductionModelError(f"SAP returned HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if "error" in data:
            raise SAPProductionModelError(data["error"].get("message", {}).get("value", "Unknown OData error"))

        options = []
        for pm in data.get("d", {}).get("results", []):
            model_id = pm.get("ID")
            model_uuid = pm.get("UUID")
            if not model_id or not model_uuid:
                continue

            site_ids = sorted({
                spa_uuid_to_id.get(s.get("SupplyPlanningAreaUUID", "").upper())
                for s in _as_list(pm.get("ProductionModelSupplyPlanningArea"))
                if s.get("SupplyPlanningAreaUUID") and spa_uuid_to_id.get(s.get("SupplyPlanningAreaUUID", "").upper())
            })
            if site_spa_uuid:
                model_spa_uuids = {s.get("SupplyPlanningAreaUUID", "").upper() for s in _as_list(pm.get("ProductionModelSupplyPlanningArea"))}
                if site_spa_uuid.upper() not in model_spa_uuids:
                    continue
                site_ids = [site_id]
            if not site_ids:
                continue

            candidates = []
            for rpm in _as_list(pm.get("ReleasedPlanningProductionModelReleasedPlanningProductionModel")):
                lr = rpm.get("SourceOfSupplySourceOfSupplyLogisticRelationship")
                if not lr or not lr.get("UUID"):
                    continue
                candidates.append({
                    "logistic_relationship_uuid": lr["UUID"],
                    "logistic_relationship_id": lr.get("ID"),
                    "is_active": lr.get("OverallLifeCycleStatusCode") == ACTIVE_STATUS_CODE,
                    "priority": lr.get("PriorityValue") or 0,
                })
            if not candidates:
                continue
            active = [c for c in candidates if c["is_active"]]
            chosen = max(active or candidates, key=lambda c: c["priority"])

            for sid in site_ids:
                options.append({
                    "production_model_id": model_id,
                    "production_model_uuid": model_uuid,
                    "description": pm.get("Description"),
                    "site_id": sid,
                    **chosen,
                })

        return sorted(options, key=lambda c: (c["site_id"], c["production_model_id"]))


