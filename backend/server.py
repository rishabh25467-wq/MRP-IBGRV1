import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException, Query
from pydantic import BaseModel
from pymongo import MongoClient
from starlette.middleware.cors import CORSMiddleware

from sap_soap_client import SAPSoapBOMClient, SAPSoapError
from sap_material_client import SAPMaterialClient, SAPMaterialError, SAPMaterialAuthError
from sap_valuation_client import SAPValuationClient, SAPValuationError
from sap_inventory_client import SAPInventoryClient, SAPInventoryError
from sap_planning_client import SAPPlanningClient, SAPPlanningError, bulk_push_to_sap
from inventory_service import get_cached_inventory, refresh_inventory_cache, deep_backfill_uuids
from bom_categorizer import categorize_items, _ai_categorize, BomCategorizerError, get_categories, add_category, delete_category, backfill_product_uuids
from oms_client import OMSClient, OMSError
from purchasing_plan import (
    build_purchasing_plan, retry_missing_boms, get_part_overrides, save_part_override,
    _default_month, _validate_month,
)
import bom_cache_service

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()
api_router = APIRouter(prefix="/api")

mongo_client = MongoClient(os.environ['MONGO_URL'], tz_aware=True)
db = mongo_client[os.environ['DB_NAME']]

sap_soap_client = SAPSoapBOMClient(
    endpoint=os.environ['SAP_SOAP_ENDPOINT'],
    username=os.environ['SAP_SOAP_USERNAME'],
    password=os.environ['SAP_SOAP_PASSWORD'],
)

# Direct Material ID -> UUID lookup (no BOM relationship required) - closes
# the value-coverage gap that bom_node_cache-based resolution structurally
# cannot (pure raw materials with no BOM anywhere). See sap_material_client
# module docstring: NOT YET AUTHORIZED on the tenant as of this writing.
sap_material_client = SAPMaterialClient(
    endpoint=os.environ['SAP_SOAP_MATERIAL_ENDPOINT'],
    username=os.environ['SAP_SOAP_USERNAME'],
    password=os.environ['SAP_SOAP_PASSWORD'],
)

sap_valuation_client = SAPValuationClient(
    base_url=os.environ['SAP_ODATA_BASE_URL'],
    username=os.environ['SAP_ODATA_USERNAME'],
    password=os.environ['SAP_ODATA_PASSWORD'],
)

sap_inventory_client = SAPInventoryClient(
    report_url=os.environ['SAP_INVENTORY_ODATA_URL'],
    username=os.environ['SAP_ODATA_USERNAME'],
    password=os.environ['SAP_ODATA_PASSWORD'],
)

sap_planning_client = SAPPlanningClient(
    base_url=os.environ['SAP_PLANNING_ODATA_BASE_URL'],
    username=os.environ['SAP_ODATA_USERNAME'],
    password=os.environ['SAP_ODATA_PASSWORD'],
)

oms_client = OMSClient(
    base_url=os.environ['OMS_BASE_URL'],
    username=os.environ['OMS_USERNAME'],
    password=os.environ['OMS_PASSWORD'],
)

# In-memory job store for the long-running (multi-minute, live OMS+SAP)
# Purchasing Plan generation - kept out-of-request so the client never has to
# hold a single HTTP connection open longer than the ingress/proxy timeout;
# the frontend polls /purchasing-plan/status/{job_id} instead.
purchasing_plan_jobs: dict = {}

# Same out-of-request pattern for the Admin page's bulk "Push All to SAP" -
# can touch hundreds of materials, each needing a CSRF handshake + several
# PATCHes, so it must run as a background job too.
push_all_to_sap_jobs: dict = {}

# Same pattern again for the Inventory page - the underlying SAP OData
# on-hand stock report can be slow/heavily paged under tenant load.
inventory_jobs: dict = {}

# Controlled, throttled one-time live-SAP UUID backfill for inventory items
# that have never appeared in any BOM Explorer/Purchasing Plan run - see
# inventory_service.deep_backfill_uuids docstring.
deep_backfill_jobs: dict = {}


class BomNode(BaseModel):
    level: int
    group_id: Optional[str] = None
    item_id: Optional[str] = None
    product_id: str
    product_uuid: Optional[str] = None
    description: Optional[str] = None
    quantity: Optional[float] = None
    unit_of_measure: Optional[str] = None
    eco_id: Optional[str] = None
    active: bool = True
    has_sub_bom: bool = False
    children: List["BomNode"] = []


BomNode.model_rebuild()


class BomSearchResponse(BaseModel):
    bom_id: str
    total_components: int
    max_level: int
    tree: List[BomNode] = []


class ConnectionStatus(BaseModel):
    connected: bool
    message: str


class StandardCostsRequest(BaseModel):
    product_uuids: List[str]


class StandardCost(BaseModel):
    amount: float
    currency: Optional[str] = None


class StandardCostsResponse(BaseModel):
    costs: dict[str, Optional[StandardCost]]


class CategorizeRequest(BaseModel):
    items: List[dict]


class CategorizeResponse(BaseModel):
    categories: dict[str, str]


class PurchasingPlanComponent(BaseModel):
    product_id: str
    description: Optional[str] = None
    unit_of_measure: Optional[str] = None
    category: Optional[str] = None
    qty_by_month: dict[str, float]
    on_hand_qty: Optional[float] = None
    msl: Optional[float] = None
    net_qty_by_month: dict[str, float]
    unit_cost: Optional[float] = None
    currency: Optional[str] = None
    value_by_month: dict[str, Optional[float]]
    net_value_by_month: dict[str, Optional[float]]


class MissingBom(BaseModel):
    part_no: str
    sap_id: Optional[str] = None
    confidence: str = "not_found"  # "not_found" (SAP confirmed no BOM) | "fetch_error" (SAP unreachable, retryable)
    reason: str


class PurchasingPlanResponse(BaseModel):
    months: List[str]
    components: List[PurchasingPlanComponent]
    missing_boms: List[MissingBom]
    bom_data_as_of: Optional[str] = None
    inventory_as_of: Optional[str] = None


class PurchasingPlanJobStatus(BaseModel):
    job_id: str
    status: str  # "running" | "done" | "failed"
    result: Optional[PurchasingPlanResponse] = None
    error: Optional[str] = None


@api_router.get("/")
async def root():
    return {"message": "SAP BOM Lookup API"}


@api_router.get("/bom/connection-status", response_model=ConnectionStatus)
async def connection_status():
    try:
        await asyncio.to_thread(sap_soap_client.check_connection)
        return ConnectionStatus(connected=True, message="Connected to SAP Business ByDesign")
    except SAPSoapError as e:
        return ConnectionStatus(connected=False, message=str(e))


@api_router.get("/bom/search", response_model=BomSearchResponse)
async def search_bom(bom_id: str = Query(..., min_length=1)):
    try:
        result = await asyncio.to_thread(sap_soap_client.explode_bom, bom_id.strip())
    except SAPSoapError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if result is None:
        raise HTTPException(status_code=404, detail=f"BOM '{bom_id}' not found in SAP")

    return BomSearchResponse(
        bom_id=result["bom_id"],
        total_components=result["total_components"],
        max_level=result["max_level"] or 1,
        tree=result["tree"],
    )


@api_router.post("/bom/standard-costs", response_model=StandardCostsResponse)
async def standard_costs(payload: StandardCostsRequest):
    try:
        costs = await asyncio.to_thread(sap_valuation_client.get_standard_costs, payload.product_uuids)
    except SAPValuationError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return StandardCostsResponse(costs=costs)


@api_router.post("/bom/categorize", response_model=CategorizeResponse)
async def categorize(payload: CategorizeRequest):
    try:
        categories = await categorize_items(payload.items, db)
    except BomCategorizerError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return CategorizeResponse(categories=categories)


class PurchasingPlanGenerateRequest(BaseModel):
    target_month: Optional[str] = None


@api_router.post("/purchasing-plan/generate")
async def start_purchasing_plan_job(payload: Optional[PurchasingPlanGenerateRequest] = None):
    job_id = str(uuid.uuid4())
    purchasing_plan_jobs[job_id] = {"status": "running", "result": None, "error": None}
    target_month = payload.target_month if payload else None

    async def run():
        try:
            result = await asyncio.to_thread(
                build_purchasing_plan,
                oms_client, sap_soap_client, sap_valuation_client, sap_inventory_client, db, target_month
            )
            # Classify every leaf component by material/type category (same AI
            # categorizer the BOM Explorer uses) so the plan can be split/grouped
            # by category - best-effort, a categorizer hiccup shouldn't fail the plan.
            items = [{"product_id": c["product_id"], "description": c["description"], "product_uuid": c.get("product_uuid")} for c in result["components"]]
            if items:
                try:
                    categories = await categorize_items(items, db)
                except BomCategorizerError as e:
                    logger.warning(f"Purchasing plan categorization failed: {e}")
                    categories = {}
                for c in result["components"]:
                    c["category"] = categories.get(c["product_id"])
            purchasing_plan_jobs[job_id] = {"status": "done", "result": result, "error": None}
        except Exception as e:
            logger.error(f"Purchasing plan generation failed: {e}")
            purchasing_plan_jobs[job_id] = {"status": "failed", "result": None, "error": str(e)}

    asyncio.create_task(run())
    return {"job_id": job_id}


@api_router.get("/purchasing-plan/status/{job_id}", response_model=PurchasingPlanJobStatus)
async def purchasing_plan_status(job_id: str):
    job = purchasing_plan_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return PurchasingPlanJobStatus(
        job_id=job_id,
        status=job["status"],
        result=PurchasingPlanResponse(**job["result"]) if job["result"] else None,
        error=job["error"],
    )


class RetryMissingBomsRequest(BaseModel):
    part_nos: List[str]


class RetryMissingBomResult(BaseModel):
    part_no: str
    sap_id: Optional[str] = None
    resolved: bool
    confidence: Optional[str] = None
    reason: Optional[str] = None


class RetryMissingBomsResponse(BaseModel):
    results: List[RetryMissingBomResult]


@api_router.post("/purchasing-plan/retry-missing", response_model=RetryMissingBomsResponse)
async def retry_missing_boms_endpoint(payload: RetryMissingBomsRequest):
    """Instantly re-checks a specific subset of previously-missing OMS part
    numbers against live SAP (e.g. after a transient connection timeout, or
    after a manual ID correction was saved via /part-overrides), without
    re-running the whole multi-minute Purchasing Plan pipeline."""
    part_map = await asyncio.to_thread(oms_client.get_part_map)
    overrides = await asyncio.to_thread(get_part_overrides, db)
    results = await asyncio.to_thread(retry_missing_boms, payload.part_nos, part_map, sap_soap_client, db, overrides)
    return RetryMissingBomsResponse(results=results)


class SetPartOverrideRequest(BaseModel):
    part_no: str
    sap_id: str


class SetPartOverrideResponse(BaseModel):
    part_no: str
    sap_id: str
    resolved: bool
    confidence: Optional[str] = None
    reason: Optional[str] = None


@api_router.post("/purchasing-plan/part-overrides", response_model=SetPartOverrideResponse)
async def set_part_override(payload: SetPartOverrideRequest):
    """Saves a manual OMS-part-number -> SAP-Material-ID correction (e.g. OMS
    reports 'E410' but the real SAP Material ID is 'E410_IN') and immediately
    retries that one part against live SAP. The correction is persisted in
    Mongo (part_id_overrides) so every future Purchasing Plan regeneration -
    and the bulk "Retry Failed Lookups" button - also applies it automatically."""
    part_no = payload.part_no.strip()
    sap_id = payload.sap_id.strip()
    if not part_no or not sap_id:
        raise HTTPException(status_code=400, detail="part_no and sap_id are both required")

    await asyncio.to_thread(save_part_override, db, part_no, sap_id)
    part_map = await asyncio.to_thread(oms_client.get_part_map)
    result = (await asyncio.to_thread(retry_missing_boms, [part_no], part_map, sap_soap_client, db, {part_no: sap_id}))[0]
    return SetPartOverrideResponse(
        part_no=part_no, sap_id=sap_id, resolved=result["resolved"],
        confidence=result.get("confidence"), reason=result.get("reason"),
    )


class SalesPlanCustomerQty(BaseModel):
    customer_name: str
    qty: float
    price: Optional[float] = None
    sale_value_inr: Optional[float] = None
    lead_day: Optional[int] = None


class SalesPlanItem(BaseModel):
    part_no: str
    description: Optional[str] = None
    currency: Optional[str] = None
    price: Optional[float] = None
    lead_day: Optional[int] = None
    total_qty: float
    total_sale_value_inr: Optional[float] = None
    customers: List[SalesPlanCustomerQty]


class SalesPlanResponse(BaseModel):
    month: str
    items: List[SalesPlanItem]


@api_router.get("/sales-plan", response_model=SalesPlanResponse)
async def get_sales_plan(month: Optional[str] = Query(None)):
    """OMS sales forecast for a given 'YYYY-MM' month (defaults to next
    calendar month, same default the Purchasing Plan uses) - lets a user
    look up planned sales qty by item, with a per-customer breakdown,
    directly from this app instead of switching over to OMS. This is the
    raw sales-plan-level forecast (finished goods), distinct from the
    Purchasing Plan's leaf-component purchasing requirements."""
    try:
        validated_month = _validate_month(month) if month else _default_month()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        items = await asyncio.to_thread(oms_client.get_sales_plan, validated_month)
    except OMSError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return SalesPlanResponse(month=validated_month, items=items)


class InventoryLocation(BaseModel):
    site: Optional[str] = None
    logistics_area: Optional[str] = None
    stock_status: Optional[str] = None
    qty: float


class InventoryItem(BaseModel):
    product_id: str
    description: Optional[str] = None
    category: Optional[str] = None
    total_qty: float
    uom: Optional[str] = None
    unit_cost: Optional[float] = None
    currency: Optional[str] = None
    total_value: Optional[float] = None
    locations: List[InventoryLocation]


class InventoryResponse(BaseModel):
    items: List[InventoryItem]
    categories: List[str]
    updated_at: Optional[str] = None


class InventoryJobStatus(BaseModel):
    job_id: str
    status: str  # "running" | "done" | "failed"
    result: Optional[InventoryResponse] = None
    error: Optional[str] = None


@api_router.get("/inventory", response_model=InventoryResponse)
async def get_inventory():
    """Instant read of the last-refreshed inventory snapshot from the
    `inventory_cache` collection - what the Inventory page loads on every
    visit/mount. Empty items + updated_at=None if the background scheduler
    hasn't completed its first cycle yet (fresh deploy); the frontend falls
    back to triggering a live pull via POST /inventory in that case."""
    cached = await asyncio.to_thread(get_cached_inventory, db)
    return InventoryResponse(
        items=cached["items"], categories=cached["categories"],
        updated_at=cached["updated_at"].isoformat() if cached["updated_at"] else None,
    )


@api_router.post("/inventory")
async def start_inventory_job():
    """Forces a fresh live SAP On-Hand Inventory + Standard Costs pull (see
    inventory_service.refresh_inventory_cache) and overwrites the cache -
    the Inventory page's "Refresh" button, and also what the 2-hourly
    background scheduler calls. Runs as a background job (same pattern as
    Purchasing Plan generation) since the underlying SAP OData report can
    be slow/paged and would otherwise risk a proxy timeout on a plain
    synchronous request."""
    job_id = str(uuid.uuid4())
    inventory_jobs[job_id] = {"status": "running", "result": None, "error": None}

    async def run():
        try:
            cached = await asyncio.to_thread(refresh_inventory_cache, db, sap_inventory_client, sap_valuation_client)
            inventory_jobs[job_id] = {
                "status": "done",
                "result": {"items": cached["items"], "categories": cached["categories"], "updated_at": cached["updated_at"].isoformat()},
                "error": None,
            }
        except (SAPInventoryError, SAPValuationError) as e:
            inventory_jobs[job_id] = {"status": "failed", "result": None, "error": str(e)}
        except Exception as e:
            logger.error(f"Inventory job failed: {e}")
            inventory_jobs[job_id] = {"status": "failed", "result": None, "error": str(e)}

    asyncio.create_task(run())
    return {"job_id": job_id}


@api_router.get("/inventory/{job_id}", response_model=InventoryJobStatus)
async def get_inventory_job(job_id: str):
    job = inventory_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return InventoryJobStatus(job_id=job_id, **job)


class DeepBackfillProgress(BaseModel):
    processed: int
    total: int


class DeepBackfillResult(BaseModel):
    total: int
    resolved: int
    still_missing: int
    material_lookup_unauthorized: bool = False


class DeepBackfillJobStatus(BaseModel):
    job_id: str
    status: str  # "running" | "done" | "failed"
    phase: str = "resolving"  # "resolving" | "refreshing_cache" - which step is currently in progress
    progress: Optional[DeepBackfillProgress] = None
    result: Optional[DeepBackfillResult] = None
    error: Optional[str] = None


@api_router.post("/inventory/deep-backfill-uuids")
async def start_deep_backfill_uuids():
    """User-requested one-time controlled backfill: live SAP lookup (low
    concurrency, see inventory_service.deep_backfill_uuids) for every
    current inventory item still missing a product_uuid, so its Standard
    Cost can be resolved. Runs as a background job - against 1000+ items
    on a tenant known to hit connection timeouts, this can take a while.
    Refreshes the inventory cache at the end so newly-resolved valuations
    show up immediately without a separate manual Refresh."""
    job_id = str(uuid.uuid4())
    deep_backfill_jobs[job_id] = {"status": "running", "phase": "resolving", "progress": {"processed": 0, "total": 0}, "result": None, "error": None}

    def progress_callback(processed, total):
        deep_backfill_jobs[job_id]["progress"] = {"processed": processed, "total": total}

    async def run():
        try:
            result = await asyncio.to_thread(deep_backfill_uuids, db, sap_soap_client, sap_material_client, progress_callback)
        except Exception as e:
            logger.error(f"Deep UUID backfill failed: {e}")
            deep_backfill_jobs[job_id] = {
                "status": "failed", "phase": "resolving", "progress": deep_backfill_jobs[job_id]["progress"], "result": None, "error": str(e),
            }
            return

        # The backfill itself succeeded - report it as done regardless of
        # whether this best-effort follow-up refresh (a separate, unrelated
        # SAP OData call) succeeds. A failure here just means valuations
        # will show up on the next scheduled/manual Refresh instead of
        # immediately - it must never mask the backfill's own result.
        deep_backfill_jobs[job_id]["phase"] = "refreshing_cache"
        try:
            await asyncio.to_thread(refresh_inventory_cache, db, sap_inventory_client, sap_valuation_client)
        except Exception as e:
            logger.warning(f"Deep UUID backfill: post-backfill inventory refresh failed, will show up on next Refresh instead: {e}")

        deep_backfill_jobs[job_id] = {
            "status": "done", "phase": "refreshing_cache", "progress": deep_backfill_jobs[job_id]["progress"], "result": result, "error": None,
        }

    asyncio.create_task(run())
    return {"job_id": job_id}


@api_router.get("/inventory/deep-backfill-uuids/{job_id}", response_model=DeepBackfillJobStatus)
async def get_deep_backfill_status(job_id: str):
    job = deep_backfill_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return DeepBackfillJobStatus(job_id=job_id, **job)


class BomCacheStats(BaseModel):
    cached_nodes: int
    oldest_checked_at: Optional[str] = None
    newest_checked_at: Optional[str] = None


@api_router.get("/bom-cache/stats", response_model=BomCacheStats)
async def bom_cache_stats():
    def query():
        collection = db[bom_cache_service.COLLECTION_NAME]
        count = collection.count_documents({})
        oldest = collection.find_one({}, sort=[("last_checked_at", 1)])
        newest = collection.find_one({}, sort=[("last_checked_at", -1)])
        return count, oldest, newest

    count, oldest, newest = await asyncio.to_thread(query)
    return BomCacheStats(
        cached_nodes=count,
        oldest_checked_at=oldest["last_checked_at"].isoformat() if oldest and oldest.get("last_checked_at") else None,
        newest_checked_at=newest["last_checked_at"].isoformat() if newest and newest.get("last_checked_at") else None,
    )


@api_router.post("/bom-cache/refresh")
async def trigger_bom_cache_refresh():
    """Manually trigger the same change-detection sweep the background
    scheduler runs periodically - fires it off in the background (can take
    a while against a live SAP tenant) and returns immediately; check
    /bom-cache/stats or the backend logs for progress/completion."""
    async def run():
        stats = await asyncio.to_thread(bom_cache_service.refresh_stale_nodes, sap_soap_client, db)
        logger.info(f"Manually-triggered BOM cache refresh complete: {stats}")

    asyncio.create_task(run())
    return {"triggered": True}


class ComponentMasterItem(BaseModel):
    product_id: str
    description: Optional[str] = None
    category: Optional[str] = None
    category_source: Optional[str] = None
    msl: Optional[float] = None
    lead_time_days: Optional[float] = None
    has_sap_link: bool = False
    sap_pushed_at: Optional[str] = None
    updated_at: Optional[str] = None


def _to_component_master_item(doc: dict) -> ComponentMasterItem:
    updated_at = doc.get("categorized_at") or doc.get("created_at")
    sap_pushed_at = doc.get("sap_pushed_at")
    return ComponentMasterItem(
        product_id=doc["_id"],
        description=doc.get("description"),
        category=doc.get("category"),
        category_source=doc.get("category_source"),
        msl=doc.get("msl"),
        lead_time_days=doc.get("lead_time_days"),
        has_sap_link=bool(doc.get("product_uuid")),
        sap_pushed_at=sap_pushed_at.isoformat() if sap_pushed_at else None,
        updated_at=updated_at.isoformat() if updated_at else None,
    )


class ComponentMasterListResponse(BaseModel):
    items: List[ComponentMasterItem]
    categories: List[str]


@api_router.get("/admin/components", response_model=ComponentMasterListResponse)
async def list_admin_components():
    """Every leaf component ever discovered by a BOM Explorer search or a
    Purchasing Plan generation, with its current AI/manual category and MSL -
    backs the Admin > Component Master page."""
    def query():
        return list(db["component_master"].find({}))

    docs = await asyncio.to_thread(query)
    items = [_to_component_master_item(d) for d in docs]
    categories = await asyncio.to_thread(get_categories, db)
    return ComponentMasterListResponse(items=items, categories=categories)


class UpdateComponentMasterRequest(BaseModel):
    category: Optional[str] = None
    msl: Optional[float] = None
    lead_time_days: Optional[float] = None


@api_router.patch("/admin/components/{product_id}", response_model=ComponentMasterItem)
async def update_admin_component(product_id: str, payload: UpdateComponentMasterRequest):
    """Manually correct a component's category (marks it category_source
    "manual" so it's never overwritten by a bulk Re-Categorise) and/or set
    its Minimum Stock Level, which the Purchasing Plan's netting math reads
    directly (see purchasing_plan.get_component_msl), and/or its Procurement
    Lead Time (used only by the SAP Push-to-SAP write-back)."""
    update = {}
    if payload.category is not None:
        update["category"] = payload.category
        update["category_source"] = "manual"
        update["categorized_at"] = datetime.now(timezone.utc)
    if payload.msl is not None:
        update["msl"] = payload.msl
    if payload.lead_time_days is not None:
        update["lead_time_days"] = payload.lead_time_days
    if not update:
        raise HTTPException(status_code=400, detail="Provide category, msl and/or lead_time_days to update")

    def apply():
        db["component_master"].update_one({"_id": product_id}, {"$set": update}, upsert=True)
        return db["component_master"].find_one({"_id": product_id})

    doc = await asyncio.to_thread(apply)
    return _to_component_master_item(doc)


class RecategorizeRequest(BaseModel):
    product_ids: List[str]


class RecategorizeResponse(BaseModel):
    categories: dict[str, str]
    skipped_manual: List[str]


@api_router.post("/admin/components/recategorize", response_model=RecategorizeResponse)
async def recategorize_components(payload: RecategorizeRequest):
    """Forces a fresh AI categorization pass for the given product_ids,
    bypassing the normal "skip if already categorized" check - the Admin >
    Component Master page's "Re-Categorise" action. Any item a human has
    manually corrected (category_source="manual") is skipped so a bulk
    re-run never clobbers a deliberate correction."""
    def get_targets():
        docs = list(db["component_master"].find({"_id": {"$in": payload.product_ids}}))
        targets = [{"product_id": d["_id"], "description": d.get("description")}
                   for d in docs if d.get("category_source") != "manual"]
        skipped = [d["_id"] for d in docs if d.get("category_source") == "manual"]
        return targets, skipped

    targets, skipped = await asyncio.to_thread(get_targets)
    if not targets:
        return RecategorizeResponse(categories={}, skipped_manual=skipped)

    try:
        categories = await asyncio.to_thread(get_categories, db)
        ai_results = await _ai_categorize(targets, categories)
    except BomCategorizerError as e:
        raise HTTPException(status_code=502, detail=str(e))

    def persist():
        now = datetime.now(timezone.utc)
        for product_id, category in ai_results.items():
            db["component_master"].update_one(
                {"_id": product_id},
                {"$set": {"category": category, "category_source": "ai", "categorized_at": now}},
            )

    await asyncio.to_thread(persist)
    return RecategorizeResponse(categories=ai_results, skipped_manual=skipped)


class SapPlanningDataResponse(BaseModel):
    safety_stock: Optional[float] = None
    lead_time_days: Optional[float] = None
    unit_code: Optional[str] = None
    planning_area_count: int = 0


@api_router.get("/admin/components/{product_id}/sap-planning", response_model=SapPlanningDataResponse)
async def get_sap_planning_data(product_id: str):
    """Pulls SAP's current Safety Stock / Procurement Lead Time for this
    component (representative value across its Supply Planning Areas) so
    the Admin page can show it side-by-side with the app's MSL/Lead Time
    before an operator decides to push. Requires the component to have been
    seen in a BOM Explorer search or Purchasing Plan run at least once
    (that's what captures its SAP product_uuid)."""
    doc = await asyncio.to_thread(db["component_master"].find_one, {"_id": product_id})
    product_uuid = (doc or {}).get("product_uuid")
    if not product_uuid:
        raise HTTPException(status_code=404, detail="This component has no known SAP link yet - open it in BOM Explorer or a Purchasing Plan run first")

    try:
        planning = await asyncio.to_thread(sap_planning_client.get_planning_data, [product_uuid])
    except SAPPlanningError as e:
        raise HTTPException(status_code=502, detail=str(e))

    data = planning.get(product_uuid.upper())
    if not data:
        raise HTTPException(status_code=404, detail="SAP has no Supply Planning record for this material")
    return SapPlanningDataResponse(
        safety_stock=data["safety_stock"], lead_time_days=data["lead_time_days"],
        unit_code=data["unit_code"], planning_area_count=data["planning_area_count"],
    )


class PushToSapResponse(BaseModel):
    planning_areas_updated: int
    safety_stock: Optional[float] = None
    lead_time_days: Optional[float] = None


@api_router.post("/admin/components/{product_id}/push-to-sap", response_model=PushToSapResponse)
async def push_component_to_sap(product_id: str):
    """Pushes this component's app-side MSL -> SAP Safety Stock and
    Lead Time (Days) -> SAP Procurement Lead Time, applied identically to
    EVERY Supply Planning Area row for this material (the tenant has no
    single "canonical" site, so all are kept in sync). Requires at least
    one of msl/lead_time_days to already be set via the PATCH endpoint."""
    doc = await asyncio.to_thread(db["component_master"].find_one, {"_id": product_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Unknown component")
    product_uuid = doc.get("product_uuid")
    if not product_uuid:
        raise HTTPException(status_code=404, detail="This component has no known SAP link yet - open it in BOM Explorer or a Purchasing Plan run first")
    msl = doc.get("msl")
    lead_time_days = doc.get("lead_time_days")
    if msl is None and lead_time_days is None:
        raise HTTPException(status_code=400, detail="Set an MSL and/or Lead Time (Days) for this component before pushing")

    try:
        planning = await asyncio.to_thread(sap_planning_client.get_planning_data, [product_uuid])
        data = planning.get(product_uuid.upper())
        if not data:
            raise HTTPException(status_code=404, detail="SAP has no Supply Planning record for this material")
        updated = await asyncio.to_thread(
            sap_planning_client.push_planning_data, product_uuid, data["rows"], msl, lead_time_days
        )
    except SAPPlanningError as e:
        raise HTTPException(status_code=502, detail=str(e))

    await asyncio.to_thread(
        db["component_master"].update_one,
        {"_id": product_id},
        {"$set": {"sap_pushed_at": datetime.now(timezone.utc)}},
    )
    return PushToSapResponse(planning_areas_updated=updated, safety_stock=msl, lead_time_days=lead_time_days)


class PushAllToSapProgress(BaseModel):
    processed: int
    total: int


class PushAllToSapResult(BaseModel):
    total: int
    pushed: int
    failed: List[dict]


class PushAllToSapJobStatus(BaseModel):
    job_id: str
    status: str  # "running" | "done" | "failed"
    progress: Optional[PushAllToSapProgress] = None
    result: Optional[PushAllToSapResult] = None
    error: Optional[str] = None


@api_router.post("/admin/components/push-all-to-sap")
async def start_push_all_to_sap():
    """Bulk counterpart to the per-row Push to SAP - pushes MSL/Lead Time
    for EVERY component that has a SAP link and at least one value set.
    Runs as a background job (see purchasing_plan_jobs for the same
    pattern) since it can take a while against the live SAP tenant."""
    job_id = str(uuid.uuid4())
    push_all_to_sap_jobs[job_id] = {"status": "running", "progress": {"processed": 0, "total": 0}, "result": None, "error": None}

    def progress_callback(processed, total):
        push_all_to_sap_jobs[job_id]["progress"] = {"processed": processed, "total": total}

    async def run():
        try:
            result = await asyncio.to_thread(bulk_push_to_sap, db, sap_planning_client, progress_callback)
            push_all_to_sap_jobs[job_id] = {
                "status": "done", "progress": push_all_to_sap_jobs[job_id]["progress"], "result": result, "error": None,
            }
        except Exception as e:
            logger.error(f"Bulk push-to-SAP failed: {e}")
            push_all_to_sap_jobs[job_id] = {
                "status": "failed", "progress": push_all_to_sap_jobs[job_id]["progress"], "result": None, "error": str(e),
            }

    asyncio.create_task(run())
    return {"job_id": job_id}


@api_router.get("/admin/components/push-all-to-sap/{job_id}", response_model=PushAllToSapJobStatus)
async def get_push_all_to_sap_status(job_id: str):
    job = push_all_to_sap_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return PushAllToSapJobStatus(job_id=job_id, **job)


class AddCategoryRequest(BaseModel):
    name: str


class AddCategoryResponse(BaseModel):
    categories: List[str]


@api_router.post("/admin/categories", response_model=AddCategoryResponse)
async def add_admin_category(payload: AddCategoryRequest):
    """Adds a new category to the shared taxonomy (category_master) - shows
    up immediately in every category dropdown on the Admin page AND is
    included in the allowed-categories list for all future AI
    categorization calls (see bom_categorizer.get_categories)."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    categories = await asyncio.to_thread(add_category, db, name)
    return AddCategoryResponse(categories=categories)


@api_router.delete("/admin/categories/{name}", response_model=AddCategoryResponse)
async def delete_admin_category(name: str):
    """Removes a category from the shared taxonomy. Components already
    assigned to it are left untouched (see bom_categorizer.delete_category)."""
    categories = await asyncio.to_thread(delete_category, db, name)
    return AddCategoryResponse(categories=categories)


class BackfillSapLinksResponse(BaseModel):
    updated: int


@api_router.post("/admin/components/backfill-sap-links", response_model=BackfillSapLinksResponse)
async def backfill_sap_links():
    """Fills in product_uuid (needed for Push to SAP) for any component
    that predates this feature but already has its UUID sitting in the BOM
    cache from a past explosion - see bom_categorizer.backfill_product_uuids."""
    updated = await asyncio.to_thread(backfill_product_uuids, db)
    return BackfillSapLinksResponse(updated=updated)


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Background maintenance: keep the persistent BOM cache within the ~12h
# freshness requirement by re-checking every already-cached node's revision
# on a fixed interval (well under 12h, with margin for a slow SAP tenant).
# Cheap no-op for any node whose BOM hasn't actually changed - see
# bom_cache_service.refresh_stale_nodes() for the change-detection logic.
BOM_CACHE_REFRESH_INTERVAL_SECONDS = 6 * 60 * 60

# Same background-scheduler pattern for the Inventory page's cache (see
# inventory_service.refresh_inventory_cache) - every 2h, well inside a
# workday, so "Last Updated" on the Inventory page never gets too stale
# even if nobody clicks "Refresh" manually.
INVENTORY_CACHE_REFRESH_INTERVAL_SECONDS = 2 * 60 * 60


@app.on_event("startup")
async def start_bom_cache_refresh_loop():
    async def loop():
        # Small delay before the first sweep so a server restart doesn't
        # immediately hammer SAP with a full refresh cycle.
        await asyncio.sleep(30)
        while True:
            try:
                stats = await asyncio.to_thread(bom_cache_service.refresh_stale_nodes, sap_soap_client, db)
                logger.info(f"BOM cache background refresh complete: {stats}")
            except Exception as e:
                logger.error(f"BOM cache background refresh failed: {e}")
            await asyncio.sleep(BOM_CACHE_REFRESH_INTERVAL_SECONDS)

    asyncio.create_task(loop())


@app.on_event("startup")
async def start_inventory_cache_refresh_loop():
    async def loop():
        await asyncio.sleep(45)
        while True:
            try:
                cached = await asyncio.to_thread(refresh_inventory_cache, db, sap_inventory_client, sap_valuation_client)
                logger.info(f"Inventory cache background refresh complete: {len(cached['items'])} item(s) as of {cached['updated_at'].isoformat()}")
            except Exception as e:
                logger.error(f"Inventory cache background refresh failed: {e}")
            await asyncio.sleep(INVENTORY_CACHE_REFRESH_INTERVAL_SECONDS)

    asyncio.create_task(loop())

