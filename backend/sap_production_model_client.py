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


class SAPProductionModelBomClient:
    """Separate custom OData service ("productionmodelbomemergent", built by
    the same key-user OData Modeler as productionmodelemergent) that exposes
    the REAL BillOfMaterialID SAP actually locks to a given Production Model
    - distinct from bom_cache_service's own "highest revision wins" guess,
    which has no way to know which of several genuine alternate BOM
    revisions a specific Production Model was actually built against (Aug
    2026 bug: MAZ42117272-TA's cache defaulted to the 1.9mm revision while
    the material's real, released Production Model is locked to the 2.0mm
    one).

    `ReleasedExecutionProductionModelCollection` (root) -> expand
    `ReleasedExecutionProductionModelProductionSegment` (has the real
    `BillOfMaterialID`) turns out to be an APPEND-ONLY execution-history
    log, not a single current-state record - live-confirmed against
    MAZ42117272-TA's real Production Model: 19 rows for the exact same
    ProductionModelUUID, each with its own `VersionID` (1..19) and mostly
    (but not always - 2 of 19 are stale outliers) agreeing on the same
    BillOfMaterialID. The highest VersionID is the current/authoritative
    link (confirmed live: v19 -> `MAZ42117272-TA_1`, matching the real
    2.0mm revision) - so `get_bill_of_material_id_for_model` always takes
    the max-VersionID row's BillOfMaterialID, not just any/the first one.

    Deliberately scoped to ONLY the "new order, model already explicitly
    chosen via the Source of Supply picker" case (per user's Aug 2026
    decision) - NOT wired into BOM Explorer/Purchasing Plan/MRP's generic
    "highest revision" default guess, and NOT into existing-lot Production
    Confirmation's component check (no model selection happens there) -
    both of those intentionally keep their current behavior unchanged."""

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.auth = HTTPBasicAuth(username, password)

    def get_bill_of_material_id_for_model(self, production_model_uuid: str) -> str | None:
        resp = requests.get(
            f"{self.base_url}/ReleasedExecutionProductionModelCollection",
            auth=self.auth,
            headers={"Accept": "application/json"},
            params={
                "$filter": f"ProductionModelUUID eq guid'{production_model_uuid}'",
                "$expand": "ReleasedExecutionProductionModelProductionSegment",
                "$format": "json",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise SAPProductionModelError(f"SAP returned HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if "error" in data:
            raise SAPProductionModelError(data["error"].get("message", {}).get("value", "Unknown OData error"))

        best_version, best_bom_id = -1, None
        for rem in _as_list(data.get("d", {}).get("results")):
            for seg in _as_list(rem.get("ReleasedExecutionProductionModelProductionSegment")):
                bom_id = seg.get("BillOfMaterialID")
                if not bom_id:
                    continue
                try:
                    version = int(seg.get("VersionID") or -1)
                except (TypeError, ValueError):
                    version = -1
                if version > best_version:
                    best_version, best_bom_id = version, bom_id
        return best_bom_id

    def get_bill_of_operations_id_for_model_id(self, production_model_id: str) -> str | None:
        """Same APPEND-ONLY-log/max-VersionID caveat as
        get_bill_of_material_id_for_model above, but keyed by the
        human-readable Production Model ID (all we have on file for an
        existing order - see production_order_creation_history) rather
        than its UUID, and returning BillOfOperationsID instead (Aug 2026,
        user's ask - "I need Reporting Point Description")."""
        resp = requests.get(
            f"{self.base_url}/ReleasedExecutionProductionModelCollection",
            auth=self.auth,
            headers={"Accept": "application/json"},
            params={
                "$filter": f"ProductionModelID eq '{production_model_id}'",
                "$expand": "ReleasedExecutionProductionModelProductionSegment",
                "$format": "json",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise SAPProductionModelError(f"SAP returned HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if "error" in data:
            raise SAPProductionModelError(data["error"].get("message", {}).get("value", "Unknown OData error"))

        best_version, best_boo_id = -1, None
        for rem in _as_list(data.get("d", {}).get("results")):
            for seg in _as_list(rem.get("ReleasedExecutionProductionModelProductionSegment")):
                boo_id = seg.get("BillOfOperationsID")
                if not boo_id:
                    continue
                try:
                    version = int(seg.get("VersionID") or -1)
                except (TypeError, ValueError):
                    version = -1
                if version > best_version:
                    best_version, best_boo_id = version, boo_id
        return best_boo_id


