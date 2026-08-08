import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import requests
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
from bom_categorizer import categorize_items, _ai_categorize, BomCategorizerError, get_categories, add_category, delete_category, backfill_product_uuids, categorize_full_inventory
from oms_client import OMSClient, OMSError
from open_po_client import OpenPODemandClient, OpenPODemandError
from purchasing_plan import (
    build_purchasing_plan, retry_missing_boms, get_part_overrides, save_part_override,
    _default_month, _validate_month,
)
import bom_cache_service
import production_plan_service
import mrp_service
import po_selection_service
import mrp_plan_store
import job_store

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()
api_router = APIRouter(prefix="/api")

mongo_client = MongoClient(os.environ['MONGO_URL'], tz_aware=True)
db = mongo_client[os.environ['DB_NAME']]
job_store.ensure_indexes(db)

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

# Open-PO Demand feed - separate integration from oms_client (different base
# URL, X-API-Key auth instead of a login flow). Backs the Production Plan
# page's MRP computation - see mrp_service.py / open_po_client.py.
open_po_client = OpenPODemandClient(
    base_url=os.environ['OPEN_PO_DEMAND_BASE_URL'],
    api_key=os.environ['OPEN_PO_DEMAND_API_KEY'],
)




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


class ItemLookupInfo(BaseModel):
    product_id: str
    description: Optional[str] = None
    category: Optional[str] = None
    uom: Optional[str] = None
    on_hand_qty: Optional[float] = None
    unit_cost: Optional[float] = None
    currency: Optional[str] = None
    cost_source: str  # "live" | "unavailable"
    note: str


class BomSearchResponse(BaseModel):
    bom_id: str
    total_components: int
    max_level: int
    tree: List[BomNode] = []
    has_bom: bool = True
    item_info: Optional[ItemLookupInfo] = None


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
    product_id = bom_id.strip()
    try:
        result = await asyncio.to_thread(sap_soap_client.explode_bom, product_id)
    except SAPSoapError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if result is None:
        item_info = await asyncio.to_thread(_lookup_item_without_bom, product_id)
        if item_info is None:
            raise HTTPException(status_code=404, detail=f"BOM '{product_id}' not found in SAP")
        return BomSearchResponse(bom_id=product_id, total_components=0, max_level=0, tree=[], has_bom=False, item_info=item_info)

    return BomSearchResponse(
        bom_id=result["bom_id"],
        total_components=result["total_components"],
        max_level=result["max_level"] or 1,
        tree=result["tree"],
    )


def _lookup_item_without_bom(product_id: str) -> Optional[ItemLookupInfo]:
    """Fallback for /bom/search when SAP has no BOM for this ID: confirm the
    material genuinely EXISTS in SAP at all (live QueryMaterialIn lookup -
    this is the only way to tell "no BOM, but it's a real purchased/raw
    item" apart from "not found in SAP at all", since explode_bom returning
    None means the same thing for both). If it exists, try a live Standard
    Cost lookup, and fill in description/category/UOM/on-hand qty from
    whatever we already have cached (Inventory/Admin) - those aren't
    available from QueryMaterialIn itself. Returns None if the material
    doesn't exist in SAP either, so the caller can fall back to the
    original 404."""
    try:
        product_uuid = sap_material_client.resolve_uuid(product_id)
    except (SAPMaterialError,):
        # The material-lookup service itself hiccuped - we genuinely don't
        # know if the item exists, so don't claim it doesn't (fall back to
        # the plain "not found" 404 instead of asserting a wrong negative).
        return None
    if not product_uuid:
        return None

    unit_cost, currency, cost_source = None, None, "unavailable"
    try:
        costs = sap_valuation_client.get_standard_costs([product_uuid])
        cost = costs.get(product_uuid.upper())
        if cost:
            unit_cost, currency, cost_source = cost.get("amount"), cost.get("currency"), "live"
    except (SAPValuationError, requests.exceptions.RequestException):
        pass

    comp_doc = db["component_master"].find_one({"_id": product_id})
    on_hand_qty, uom = None, None
    cached_inv = db["inventory_cache"].find_one({"_id": "latest"}, {"items": 1})
    if cached_inv:
        for it in cached_inv.get("items", []):
            if it.get("product_id") == product_id:
                on_hand_qty, uom = it.get("total_qty"), it.get("uom")
                break

    return ItemLookupInfo(
        product_id=product_id,
        description=comp_doc.get("description") if comp_doc else None,
        category=comp_doc.get("category") if comp_doc else None,
        uom=uom,
        on_hand_qty=on_hand_qty,
        unit_cost=unit_cost,
        currency=currency,
        cost_source=cost_source,
        note="This item exists in SAP but has no Bill of Materials defined - it's a raw material or purchased part, not a manufactured assembly.",
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
    job_store.create_job(db, job_id, {"status": "running", "result": None, "error": None})
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
            job_store.update_job(db, job_id, {"status": "done", "result": result, "error": None})
        except Exception as e:
            logger.error(f"Purchasing plan generation failed: {e}")
            job_store.update_job(db, job_id, {"status": "failed", "result": None, "error": str(e)})

    asyncio.create_task(run())
    return {"job_id": job_id}


@api_router.get("/purchasing-plan/status/{job_id}", response_model=PurchasingPlanJobStatus)
async def purchasing_plan_status(job_id: str):
    job = job_store.get_job(db, job_id)
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
    job_store.create_job(db, job_id, {"status": "running", "result": None, "error": None})

    async def run():
        try:
            cached = await asyncio.to_thread(refresh_inventory_cache, db, sap_inventory_client, sap_valuation_client)
            job_store.update_job(db, job_id, {
                "status": "done",
                "result": {"items": cached["items"], "categories": cached["categories"], "updated_at": cached["updated_at"].isoformat()},
                "error": None,
            })
        except (SAPInventoryError, SAPValuationError) as e:
            job_store.update_job(db, job_id, {"status": "failed", "result": None, "error": str(e)})
        except Exception as e:
            logger.error(f"Inventory job failed: {e}")
            job_store.update_job(db, job_id, {"status": "failed", "result": None, "error": str(e)})

    asyncio.create_task(run())
    return {"job_id": job_id}


@api_router.get("/inventory/{job_id}", response_model=InventoryJobStatus)
async def get_inventory_job(job_id: str):
    job = job_store.get_job(db, job_id)
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
    stopped_early: bool = False


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
    Cost can be resolved. Runs as a background job, bounded to a few
    minutes per click on a tenant known to hit connection timeouts -
    resolved items persist immediately, so if the run stops early
    (result.stopped_early) the user just clicks it again to continue.
    Refreshes the inventory cache at the end so newly-resolved valuations
    show up immediately without a separate manual Refresh."""
    job_id = str(uuid.uuid4())
    job_store.create_job(db, job_id, {"status": "running", "phase": "resolving", "progress": {"processed": 0, "total": 0}, "result": None, "error": None})

    last_progress = {"processed": 0, "total": 0}

    def progress_callback(processed, total):
        nonlocal last_progress
        last_progress = {"processed": processed, "total": total}
        job_store.update_job(db, job_id, {"progress": last_progress})

    async def run():
        try:
            result = await asyncio.to_thread(deep_backfill_uuids, db, sap_soap_client, sap_material_client, progress_callback)
        except Exception as e:
            logger.error(f"Deep UUID backfill failed: {e}")
            job_store.update_job(db, job_id, {
                "status": "failed", "phase": "resolving", "progress": last_progress, "result": None, "error": str(e),
            })
            return

        # The backfill itself succeeded - report it as done regardless of
        # whether this best-effort follow-up refresh (a separate, unrelated
        # SAP OData call) succeeds. A failure here just means valuations
        # will show up on the next scheduled/manual Refresh instead of
        # immediately - it must never mask the backfill's own result.
        job_store.update_job(db, job_id, {"phase": "refreshing_cache"})
        try:
            await asyncio.to_thread(refresh_inventory_cache, db, sap_inventory_client, sap_valuation_client)
        except Exception as e:
            logger.warning(f"Deep UUID backfill: post-backfill inventory refresh failed, will show up on next Refresh instead: {e}")

        job_store.update_job(db, job_id, {
            "status": "done", "phase": "refreshing_cache", "progress": last_progress, "result": result, "error": None,
        })

    asyncio.create_task(run())
    return {"job_id": job_id}


@api_router.get("/inventory/deep-backfill-uuids/{job_id}", response_model=DeepBackfillJobStatus)
async def get_deep_backfill_status(job_id: str):
    job = job_store.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return DeepBackfillJobStatus(job_id=job_id, **job)


class CategorizeAllResult(BaseModel):
    total_items: int
    finished_goods: int
    ai_categorized: int


class CategorizeAllJobStatus(BaseModel):
    job_id: str
    status: str  # "running" | "done" | "failed"
    result: Optional[CategorizeAllResult] = None
    error: Optional[str] = None


@api_router.post("/inventory/categorize-all")
async def start_categorize_all_inventory():
    """One-click bulk categorization for EVERY item currently on the
    Inventory page - unlike the AI Categorize button on BOM Explorer /
    Purchasing Plan (which only ever sees BOM LEAF nodes), this is the only
    flow that also covers BOM ROOT/top-level finished products, which get
    deterministically classified as 'Finished Goods' (see
    bom_categorizer.categorize_full_inventory). Runs as a background job -
    the AI pass over any still-uncategorized leaf items is a handful of
    sequential LLM calls (BATCH_SIZE=80 each) that can take a couple of
    minutes for a large, freshly-seeded catalog."""
    job_id = str(uuid.uuid4())
    job_store.create_job(db, job_id, {"status": "running", "result": None, "error": None})

    async def run():
        try:
            cached = await asyncio.to_thread(get_cached_inventory, db)
            items = [{"product_id": it["product_id"], "description": it.get("description")} for it in cached["items"]]
            before = {
                doc["_id"]: doc.get("category")
                for doc in db["component_master"].find({"_id": {"$in": [i["product_id"] for i in items]}}, {"category": 1})
            }
            categories = await categorize_full_inventory(db, items)

            finished_goods = sum(1 for c in categories.values() if c == "Finished Goods")
            newly_ai = sum(1 for pid, c in categories.items() if c != "Finished Goods" and before.get(pid) != c)
            job_store.update_job(db, job_id, {
                "status": "done",
                "result": {"total_items": len(items), "finished_goods": finished_goods, "ai_categorized": newly_ai},
                "error": None,
            })
        except BomCategorizerError as e:
            job_store.update_job(db, job_id, {"status": "failed", "result": None, "error": str(e)})
        except Exception as e:
            logger.error(f"Categorize-all inventory failed: {e}")
            job_store.update_job(db, job_id, {"status": "failed", "result": None, "error": str(e)})

    asyncio.create_task(run())
    return {"job_id": job_id}


@api_router.get("/inventory/categorize-all/{job_id}", response_model=CategorizeAllJobStatus)
async def get_categorize_all_status(job_id: str):
    job = job_store.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return CategorizeAllJobStatus(job_id=job_id, **job)


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


class BomAlternateOption(BaseModel):
    bom_id: str
    description: Optional[str] = None
    sample_item_id: Optional[str] = None
    sample_item_description: Optional[str] = None


class BomAlternateEntry(BaseModel):
    product_id: str
    options: List[BomAlternateOption]
    default_bom_id: str
    resolved_bom_id: Optional[str] = None


class BomAlternatesResponse(BaseModel):
    items: List[BomAlternateEntry]


class ResolveAlternateRequest(BaseModel):
    chosen_bom_id: str


@api_router.get("/production-plan/alternates", response_model=BomAlternatesResponse)
async def get_bom_alternates():
    """Every component known to have genuine BOM alternates (same output,
    different raw materials - see sap_soap_client._parse) that production
    needs to pick between, plus whichever choice (if any) is already on
    file. A component only shows up here once it's been explored at least
    once via BOM Explorer/Purchasing Plan/the periodic cache refresh."""
    items = await asyncio.to_thread(production_plan_service.list_alternates, db)
    return BomAlternatesResponse(items=items)


@api_router.post("/production-plan/alternates/{product_id}", response_model=BomAlternateEntry)
async def resolve_bom_alternate(product_id: str, payload: ResolveAlternateRequest):
    """Production's standing choice for this component - applies globally,
    everywhere the component is used, until changed or cleared here."""
    await asyncio.to_thread(production_plan_service.resolve_alternate, db, product_id, payload.chosen_bom_id)
    items = await asyncio.to_thread(production_plan_service.list_alternates, db)
    match = next((i for i in items if i["product_id"] == product_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="Unknown product_id")
    return BomAlternateEntry(**match)


@api_router.delete("/production-plan/alternates/{product_id}", response_model=BomAlternateEntry)
async def clear_bom_alternate(product_id: str):
    """Reverts to the default (highest-revision) selection."""
    await asyncio.to_thread(production_plan_service.clear_alternate_resolution, db, product_id)
    items = await asyncio.to_thread(production_plan_service.list_alternates, db)
    match = next((i for i in items if i["product_id"] == product_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="Unknown product_id")
    return BomAlternateEntry(**match)


class OpenPoRow(BaseModel):
    customer: Optional[str] = None
    customer_pcode: Optional[str] = None
    customer_po: Optional[str] = None
    end_customer_po: Optional[str] = None
    internal_pono: Optional[float] = None
    item_code: str
    description: Optional[str] = None
    qty_ordered: Optional[float] = None
    qty_shipped: Optional[float] = None
    qty_open: Optional[float] = None
    due_date: Optional[str] = None
    target_ship_date: Optional[str] = None
    target_ship_basis: Optional[str] = None
    po_date: Optional[str] = None
    currency: Optional[str] = None
    lead_day: Optional[int] = None
    po_price: Optional[float] = None
    invoice_price: Optional[float] = None
    changed_at: Optional[str] = None


class OpenPoDemandResponse(BaseModel):
    count: int
    max_changed_at: Optional[str] = None
    truncated: bool = False
    rows: List[OpenPoRow]


@api_router.get("/production-plan/open-po-demand", response_model=OpenPoDemandResponse)
async def get_open_po_demand(customer: Optional[str] = Query(None), plant: Optional[str] = Query(None)):
    """Browsable view of the raw external Open-PO Demand feed (see
    open_po_client.py) - backs the Production Plan page's "Open PO Demand"
    tab. Live call every time (no caching) since this feed is the
    up-to-the-minute source of truth for target ship dates."""
    try:
        feed = await asyncio.to_thread(open_po_client.get_open_po_demand, customer, plant)
    except OpenPODemandError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return OpenPoDemandResponse(count=feed.get("count", 0), max_changed_at=feed.get("max_changed_at"), truncated=feed.get("truncated", False), rows=feed.get("rows", []))


class MrpDemandLine(BaseModel):
    internal_pono: Optional[float] = None
    customer_po: Optional[str] = None
    customer: Optional[str] = None
    item_code: str
    target_ship_date: Optional[str] = None
    order_by_date: Optional[str] = None
    lead_time_missing: bool = False
    gross_qty: float
    net_qty: float


class MrpComponent(BaseModel):
    product_id: str
    description: Optional[str] = None
    unit_of_measure: Optional[str] = None
    lead_time_days: Optional[float] = None
    msl: Optional[float] = None
    on_hand_qty: Optional[float] = None
    total_gross_qty: float
    total_net_qty: float
    demand_lines: List[MrpDemandLine]


class UnresolvedMrpItem(BaseModel):
    item_code: str
    sap_id: Optional[str] = None
    confidence: str
    reason: str


class MrpPlanResponse(BaseModel):
    generated_at: str
    po_data_as_of: Optional[str] = None
    total_po_lines: int
    total_open_po_lines: int = 0
    unresolved_items: List[UnresolvedMrpItem]
    components: List[MrpComponent]


class MrpPlanJobStatus(BaseModel):
    job_id: str
    status: str  # "running" | "done" | "failed"
    result: Optional[MrpPlanResponse] = None
    error: Optional[str] = None


@api_router.post("/production-plan/mrp/generate")
async def start_mrp_plan_job(customer: Optional[str] = None, actor: Optional[str] = None):
    """Kicks off the MRP computation (see mrp_service.build_mrp_plan) as a
    background job - same pattern as Purchasing Plan generation, since
    exploding every open PO line's BOM against live SAP can take a while
    for item_codes not already warm in the BOM cache. On success, also
    silently autosaves the result (mrp_plan_store) so a page refresh never
    loses the last-generated plan."""
    job_id = str(uuid.uuid4())
    job_store.create_job(db, job_id, {"status": "running", "result": None, "error": None})

    async def run():
        try:
            result = await asyncio.to_thread(mrp_service.build_mrp_plan, open_po_client, sap_soap_client, db, customer)
            job_store.update_job(db, job_id, {"status": "done", "result": result, "error": None})
            await asyncio.to_thread(mrp_plan_store.set_autosave, db, result, actor)
        except OpenPODemandError as e:
            job_store.update_job(db, job_id, {"status": "failed", "result": None, "error": str(e)})
        except Exception as e:
            logger.error(f"MRP plan generation failed: {e}")
            job_store.update_job(db, job_id, {"status": "failed", "result": None, "error": str(e)})

    asyncio.create_task(run())
    return {"job_id": job_id}


@api_router.get("/production-plan/mrp/status/{job_id}", response_model=MrpPlanJobStatus)
async def mrp_plan_status(job_id: str):
    job = job_store.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return MrpPlanJobStatus(
        job_id=job_id, status=job["status"],
        result=MrpPlanResponse(**job["result"]) if job["result"] else None,
        error=job["error"],
    )


class MrpAutosaveResponse(BaseModel):
    found: bool
    plan: Optional[MrpPlanResponse] = None
    created_at: Optional[str] = None
    created_by: Optional[str] = None


@api_router.get("/production-plan/mrp/autosave", response_model=MrpAutosaveResponse)
async def get_mrp_autosave():
    """The last plan that finished generating, silently autosaved server-side
    - lets the MRP Plan tab restore exactly what you last saw after a page
    refresh, without needing to click Generate again."""
    doc = await asyncio.to_thread(mrp_plan_store.get_autosave, db)
    if not doc:
        return MrpAutosaveResponse(found=False)
    return MrpAutosaveResponse(
        found=True, plan=MrpPlanResponse(**doc["plan"]),
        created_at=doc["created_at"].isoformat(), created_by=doc.get("created_by"),
    )


class SaveMrpPlanRequest(BaseModel):
    name: str
    actor: str
    plan: MrpPlanResponse


class SavedMrpPlanMeta(BaseModel):
    id: str
    name: str
    created_by: Optional[str] = None
    created_at: str
    total_po_lines: int
    total_open_po_lines: int = 0
    components_count: int
    total_net_qty: float


class SavedMrpPlansListResponse(BaseModel):
    plans: List[SavedMrpPlanMeta]


def _saved_plan_meta(doc) -> SavedMrpPlanMeta:
    plan = doc["plan"]
    return SavedMrpPlanMeta(
        id=doc["_id"], name=doc["name"], created_by=doc.get("created_by"),
        created_at=doc["created_at"].isoformat(),
        total_po_lines=plan.get("total_po_lines", 0), total_open_po_lines=plan.get("total_open_po_lines", 0),
        components_count=len(plan.get("components", [])),
        total_net_qty=round(sum(c.get("total_net_qty", 0) for c in plan.get("components", [])), 2),
    )


@api_router.post("/production-plan/mrp/saved-plans", response_model=SavedMrpPlanMeta)
async def save_mrp_plan(payload: SaveMrpPlanRequest):
    """Explicit 'Save As' - snapshots the currently-displayed MRP plan
    (exactly as generated, including which PO lines/demand lines drove it)
    under a name, kept forever until deleted. Independent from the silent
    autosave above."""
    name = payload.name.strip()
    actor = payload.actor.strip()
    if not name or not actor:
        raise HTTPException(status_code=400, detail="name and actor (your name) are both required")
    doc = await asyncio.to_thread(mrp_plan_store.save_named_plan, db, name, actor, payload.plan.dict())
    return _saved_plan_meta(doc)


@api_router.get("/production-plan/mrp/saved-plans", response_model=SavedMrpPlansListResponse)
async def list_saved_mrp_plans():
    docs = await asyncio.to_thread(mrp_plan_store.list_named_plans, db)
    return SavedMrpPlansListResponse(plans=[_saved_plan_meta(d) for d in docs])


@api_router.get("/production-plan/mrp/saved-plans/{plan_id}", response_model=MrpPlanResponse)
async def get_saved_mrp_plan(plan_id: str):
    doc = await asyncio.to_thread(mrp_plan_store.get_plan, db, plan_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Saved plan not found")
    return MrpPlanResponse(**doc["plan"])


@api_router.delete("/production-plan/mrp/saved-plans/{plan_id}")
async def delete_saved_mrp_plan(plan_id: str):
    deleted = await asyncio.to_thread(mrp_plan_store.delete_plan, db, plan_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Saved plan not found")
    return {"deleted": True}


class RetryUnresolvedMrpRequest(BaseModel):
    item_codes: List[str]


class RetryUnresolvedMrpResult(BaseModel):
    item_code: str
    sap_id: Optional[str] = None
    resolved: bool
    confidence: Optional[str] = None
    reason: Optional[str] = None


class RetryUnresolvedMrpResponse(BaseModel):
    results: List[RetryUnresolvedMrpResult]


@api_router.post("/production-plan/mrp/retry-unresolved", response_model=RetryUnresolvedMrpResponse)
async def retry_unresolved_mrp_items(payload: RetryUnresolvedMrpRequest):
    """Instantly re-checks a specific subset of previously-unresolved
    open-PO item codes against live SAP - same pattern (and same
    part_id_overrides collection) as the Purchasing Plan's "Retry Failed
    Lookups"/"Fix Mapping" feature, so a correction saved on either page
    benefits both."""
    overrides = await asyncio.to_thread(get_part_overrides, db)
    results = await asyncio.to_thread(mrp_service.retry_unresolved_items, payload.item_codes, sap_soap_client, db, overrides)
    return RetryUnresolvedMrpResponse(results=results)


class SetMrpPartOverrideRequest(BaseModel):
    item_code: str
    sap_id: str


class SetMrpPartOverrideResponse(BaseModel):
    item_code: str
    sap_id: str
    resolved: bool
    confidence: Optional[str] = None
    reason: Optional[str] = None


@api_router.post("/production-plan/mrp/part-overrides", response_model=SetMrpPartOverrideResponse)
async def set_mrp_part_override(payload: SetMrpPartOverrideRequest):
    """Saves a manual open-PO-item-code -> SAP-Material-ID correction and
    immediately retries that one item against live SAP. Shares the same
    part_id_overrides collection as the Purchasing Plan page - a correction
    saved here also fixes that item_code on the Purchasing Plan, and
    vice-versa."""
    item_code = payload.item_code.strip()
    sap_id = payload.sap_id.strip()
    if not item_code or not sap_id:
        raise HTTPException(status_code=400, detail="item_code and sap_id are both required")

    await asyncio.to_thread(save_part_override, db, item_code, sap_id)
    result = (await asyncio.to_thread(mrp_service.retry_unresolved_items, [item_code], sap_soap_client, db, {item_code: sap_id}))[0]
    return SetMrpPartOverrideResponse(
        item_code=item_code, sap_id=sap_id, resolved=result["resolved"],
        confidence=result.get("confidence"), reason=result.get("reason"),
    )


class PoSelectionEntry(BaseModel):
    key: str
    internal_pono: Optional[float] = None
    item_code: Optional[str] = None
    customer_po: Optional[str] = None
    customer: Optional[str] = None
    selected: bool
    selected_by: Optional[str] = None
    selected_at: Optional[str] = None


class PoSelectionsResponse(BaseModel):
    selections: List[PoSelectionEntry]


@api_router.get("/production-plan/po-selections", response_model=PoSelectionsResponse)
async def list_po_selections():
    """Every open-PO line production has ever marked selected/deselected -
    the Open PO Demand and MRP Plan tabs merge this onto their own rows (by
    `internal_pono::item_code`) to render checkboxes + the 'who/when'
    caption without a separate round-trip per row."""
    docs = await asyncio.to_thread(po_selection_service.list_selections, db)
    selections = [
        PoSelectionEntry(
            key=d["_id"], internal_pono=d.get("internal_pono"), item_code=d.get("item_code"),
            customer_po=d.get("customer_po"), customer=d.get("customer"), selected=d["selected"],
            selected_by=d.get("selected_by"), selected_at=d["selected_at"].isoformat() if d.get("selected_at") else None,
        )
        for d in docs
    ]
    return PoSelectionsResponse(selections=selections)


class TogglePoSelectionRequest(BaseModel):
    internal_pono: float
    item_code: str
    customer_po: Optional[str] = None
    customer: Optional[str] = None
    selected: bool
    actor: str


class TogglePoSelectionResponse(BaseModel):
    key: str
    selected: bool
    selected_by: str
    selected_at: str


@api_router.post("/production-plan/po-selections/toggle", response_model=TogglePoSelectionResponse)
async def toggle_po_selection(payload: TogglePoSelectionRequest):
    """Marks (or unmarks) one open-PO line as selected-for-production. The
    MRP Plan computation only ever nets demand for currently-selected
    lines - see mrp_service.build_mrp_plan. No login system exists, so
    `actor` is a free-text name the user typed once in their browser
    (persisted client-side); every action is still appended to a full
    audit history (po_selection_history), never overwritten."""
    actor = payload.actor.strip()
    if not actor:
        raise HTTPException(status_code=400, detail="actor (your name) is required")
    result = await asyncio.to_thread(
        po_selection_service.toggle_selection, db, payload.internal_pono, payload.item_code,
        payload.customer_po, payload.customer, payload.selected, actor,
    )
    return TogglePoSelectionResponse(
        key=result["key"], selected=result["selected"], selected_by=result["selected_by"],
        selected_at=result["selected_at"].isoformat(),
    )


class PoSelectionHistoryEntry(BaseModel):
    internal_pono: Optional[float] = None
    item_code: Optional[str] = None
    customer_po: Optional[str] = None
    customer: Optional[str] = None
    action: str
    by: str
    at: str


class PoSelectionHistoryResponse(BaseModel):
    entries: List[PoSelectionHistoryEntry]


@api_router.get("/production-plan/po-selections/history", response_model=PoSelectionHistoryResponse)
async def po_selection_history(internal_pono: Optional[float] = Query(None), item_code: Optional[str] = Query(None)):
    """Full select/deselect audit trail (who, when), optionally scoped to
    one PO line (pass both internal_pono and item_code) for the per-row
    History dialog - otherwise the most recent 200 actions across every
    line, newest first."""
    docs = await asyncio.to_thread(po_selection_service.get_history, db, internal_pono, item_code)
    entries = [
        PoSelectionHistoryEntry(
            internal_pono=d.get("internal_pono"), item_code=d.get("item_code"), customer_po=d.get("customer_po"),
            customer=d.get("customer"), action=d["action"], by=d["by"], at=d["at"].isoformat(),
        )
        for d in docs
    ]
    return PoSelectionHistoryResponse(entries=entries)


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
    Runs as a background job (see the Purchasing Plan endpoint for the same
    pattern) since it can take a while against the live SAP tenant."""
    job_id = str(uuid.uuid4())
    job_store.create_job(db, job_id, {"status": "running", "progress": {"processed": 0, "total": 0}, "result": None, "error": None})

    last_progress = {"processed": 0, "total": 0}

    def progress_callback(processed, total):
        nonlocal last_progress
        last_progress = {"processed": processed, "total": total}
        job_store.update_job(db, job_id, {"progress": last_progress})

    async def run():
        try:
            result = await asyncio.to_thread(bulk_push_to_sap, db, sap_planning_client, progress_callback)
            job_store.update_job(db, job_id, {
                "status": "done", "progress": last_progress, "result": result, "error": None,
            })
        except Exception as e:
            logger.error(f"Bulk push-to-SAP failed: {e}")
            job_store.update_job(db, job_id, {
                "status": "failed", "progress": last_progress, "result": None, "error": str(e),
            })

    asyncio.create_task(run())
    return {"job_id": job_id}


@api_router.get("/admin/components/push-all-to-sap/{job_id}", response_model=PushAllToSapJobStatus)
async def get_push_all_to_sap_status(job_id: str):
    job = job_store.get_job(db, job_id)
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

