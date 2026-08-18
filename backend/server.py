import asyncio
import logging
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

import requests
from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ValidationError
from pymongo import MongoClient
from starlette.middleware.cors import CORSMiddleware

from sap_soap_client import SAPSoapBOMClient, SAPSoapError
from sap_material_client import SAPMaterialClient, SAPMaterialError, SAPMaterialAuthError
from sap_material_physical_client import (
    SAPMaterialPhysicalClient, SAPMaterialPhysicalError, PHYSICAL_FIELD_TO_SAP_PROPERTY,
)
from sap_supplier_client import SAPSupplierClient, SAPSupplierError, SAPSupplierAuthError, SAPSupplierNotConfiguredError
from sap_price_spec_client import SAPPriceSpecClient, SAPPriceSpecError, bulk_push_erp_prices_to_sap
from sap_supplier_invoice_client import SAPSupplierInvoiceClient, SAPSupplierInvoiceError
from sap_cost_estimate_client import SAPCostEstimateClient, SAPCostEstimateError
from sap_gsa_client import SAPGSAClient, SAPGSAError
from price_explorer_client import PriceExplorerClient, PriceExplorerError
import quota_arrangement_service
from sap_valuation_client import SAPValuationClient, SAPValuationError
from sap_inventory_client import SAPInventoryClient, SAPInventoryError
from sap_planning_client import SAPPlanningClient, SAPPlanningError, bulk_push_to_sap
from inventory_service import get_cached_inventory, refresh_inventory_cache, deep_backfill_uuids
import l1_l2_report_service
from bom_categorizer import categorize_items, _ai_categorize, BomCategorizerError, get_categories, add_category, delete_category, backfill_product_uuids, categorize_full_inventory, backfill_drawing_urls, refresh_attachments_now, REFRESH_ATTACHMENTS_MAX_IDS
from oms_client import OMSClient, OMSError
from open_po_client import OpenPODemandClient, OpenPODemandError
from forecast_demand_client import ForecastDemandClient
from purchasing_plan import (
    build_purchasing_plan, retry_missing_boms, get_part_overrides, save_part_override,
    _default_month, _validate_month,
)
import bom_cache_service
import production_plan_service
import mrp_service
import mps_service
import po_selection_service
import mrp_plan_store
import autosave_store
import job_store
import supplier_service

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Imported AFTER load_dotenv (not grouped with the other module imports
# above) because auth_service.py reads AZURE_AD_*/SUPER_ADMIN_EMAILS env
# vars at module level - importing it before load_dotenv() runs would
# crash with a KeyError, since none of those vars exist in the process
# environment until the .env file is actually loaded.
import auth_service

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()
api_router = APIRouter(prefix="/api")


@app.on_event("startup")
async def configure_default_executor():
    """Raise asyncio.to_thread's shared default executor size well above
    Python's default (min(32, cpu_count+4)). This app funnels essentially
    all blocking I/O (SAP SOAP/OData, OMS, Price Explorer, Mongo) through
    `asyncio.to_thread` on that one shared pool - during a SAP tenant
    outage (repeated 30s connect-timeout retries piling up across the
    BOM/inventory background refresh loops), that default pool can fill up
    and stall even unrelated, fast requests app-wide. Observed live (Feb
    2026) while debugging a Price Explorer report: the whole backend,
    including trivial endpoints, became unresponsive during a SAP outage
    window purely from thread-pool exhaustion, not from the reported
    endpoint itself being slow."""
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=128))

mongo_client = MongoClient(os.environ['MONGO_URL'], tz_aware=True)
db = mongo_client[os.environ['DB_NAME']]
job_store.ensure_indexes(db)
auth_service.ensure_indexes(db)

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

sap_material_physical_client = SAPMaterialPhysicalClient(
    base_url=os.environ['SAP_MATERIAL_GENERALINFO_ODATA_URL'],
    username=os.environ['SAP_SOAP_USERNAME'],
    password=os.environ['SAP_SOAP_PASSWORD'],
)

sap_supplier_client = SAPSupplierClient(
    endpoint=os.environ['SAP_SOAP_SUPPLIER_ENDPOINT'],
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

sap_price_spec_client = SAPPriceSpecClient(
    base_url=os.environ['SAP_PRICE_SPEC_ODATA_BASE_URL'],
    username=os.environ['SAP_ODATA_USERNAME'],
    password=os.environ['SAP_ODATA_PASSWORD'],
    soap_endpoint=os.environ['SAP_SOAP_PRICE_SPEC_ENDPOINT'],
    soap_username=os.environ['SAP_SOAP_USERNAME'],
    soap_password=os.environ['SAP_SOAP_PASSWORD'],
)

price_explorer_client = PriceExplorerClient(
    base_url=os.environ['PRICE_EXPLORER_BASE_URL'],
    username=os.environ['PRICE_EXPLORER_USERNAME'],
    password=os.environ['PRICE_EXPLORER_PASSWORD'],
)

sap_supplier_invoice_client = SAPSupplierInvoiceClient(
    endpoint=os.environ['SAP_SOAP_SUPPLIER_INVOICE_ENDPOINT'],
    username=os.environ['SAP_SOAP_USERNAME'],
    password=os.environ['SAP_SOAP_PASSWORD'],
)

sap_cost_estimate_client = SAPCostEstimateClient(
    endpoint=os.environ['SAP_SOAP_COST_ESTIMATE_ENDPOINT'],
    username=os.environ['SAP_SOAP_USERNAME'],
    password=os.environ['SAP_SOAP_PASSWORD'],
    company_id=os.environ['SAP_COMPANY_ID'],
    set_of_books_id=os.environ['SAP_SET_OF_BOOKS_ID'],
)

sap_gsa_client = SAPGSAClient(
    endpoint=os.environ['SAP_SOAP_GSA_ENDPOINT'],
    username=os.environ['SAP_SOAP_USERNAME'],
    password=os.environ['SAP_SOAP_PASSWORD'],
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

# Forecast Demand feed - same host/family as open_po_client, X-Api-Key auth.
# Supersedes AMS per-item in demand_planning_service.get_demand_signal()
# wherever a real forecast exists; AMS remains the fallback for everything
# else (per user's Session 17 instruction).
forecast_demand_client = ForecastDemandClient(
    base_url=os.environ['FORECAST_DEMAND_BASE_URL'],
    api_key=os.environ['FORECAST_DEMAND_API_KEY'],
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


@api_router.get("/auth/login")
async def auth_login(request: Request):
    return await asyncio.to_thread(auth_service.start_login, request, db)


@api_router.get("/auth/callback")
async def auth_callback(request: Request):
    return await asyncio.to_thread(auth_service.handle_callback, request, db)


@api_router.post("/auth/logout")
async def auth_logout(request: Request):
    return await asyncio.to_thread(auth_service.logout, request, db)


@api_router.get("/auth/me")
async def auth_me(request: Request):
    user = await asyncio.to_thread(auth_service.get_current_user, request, db)
    if not user:
        return {"authenticated": False}
    return auth_service.user_public_view(user)


@api_router.get("/admin/pages")
async def admin_list_pages(request: Request):
    user = await asyncio.to_thread(auth_service.get_current_user, request, db)
    if not user or user.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin access required")
    return {"pages": auth_service.PAGE_CATALOG}


@api_router.get("/admin/users")
async def admin_list_users(request: Request):
    user = await asyncio.to_thread(auth_service.get_current_user, request, db)
    if not user or user.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin access required")
    docs = await asyncio.to_thread(
        lambda: list(db[auth_service.USERS_COLLECTION].find({}).sort("last_login_at", -1))
    )
    return {"users": docs}


class UpdateUserAccessRequest(BaseModel):
    role: str
    allowed_pages: List[str] = []


@api_router.put("/admin/users/{user_id}/access")
async def admin_update_user_access(user_id: str, body: UpdateUserAccessRequest, request: Request):
    requester = await asyncio.to_thread(auth_service.get_current_user, request, db)
    if not requester or requester.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin access required")
    if body.role not in ("user", "super_admin"):
        raise HTTPException(status_code=400, detail="Invalid role")
    invalid_pages = set(body.allowed_pages) - auth_service.PAGE_KEYS
    if invalid_pages:
        raise HTTPException(status_code=400, detail=f"Unknown page keys: {sorted(invalid_pages)}")
    result = await asyncio.to_thread(
        db[auth_service.USERS_COLLECTION].update_one,
        {"_id": user_id},
        {"$set": {"role": body.role, "allowed_pages": body.allowed_pages}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True}


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
        # NOTE: 400, not 502/503/504 - the platform's Cloudflare ingress
        # swallows those gateway-error status codes and substitutes its own
        # generic HTML error page, discarding our JSON detail before it
        # reaches the frontend (see the Cost Estimate Run endpoint below for
        # the original discovery/verification of this). A plain 400 passes
        # through intact - applied consistently to every external
        # SAP/OMS/Price-Explorer error response in this file.
        raise HTTPException(status_code=400, detail=str(e))

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
        raise HTTPException(status_code=400, detail=str(e))
    return StandardCostsResponse(costs=costs)


@api_router.post("/bom/categorize", response_model=CategorizeResponse)
async def categorize(payload: CategorizeRequest):
    try:
        categories = await categorize_items(payload.items, db)
    except BomCategorizerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return CategorizeResponse(categories=categories)


@api_router.get("/bom/drawing-urls")
async def get_drawing_urls(product_ids: str = Query(..., description="Comma-separated product IDs")):
    """Read-only lookup of drawing/documentation links for BOM Explorer -
    ALWAYS served from the `component_master` cache (never a live SAP call
    here), populated in the background by the periodic drawing-URL backfill
    scheduler (see server.py's start_drawing_url_backfill_loop). Returns
    only the product_ids that actually have a URL on file - anything
    absent from the response simply has no drawing (or hasn't been checked
    by the background job yet)."""
    ids = [p.strip() for p in product_ids.split(",") if p.strip()]
    if not ids:
        return {}
    docs = db["component_master"].find({"_id": {"$in": ids}, "drawing_url": {"$ne": None}}, {"_id": 1, "drawing_url": 1})
    return {doc["_id"]: doc["drawing_url"] for doc in docs}


@api_router.get("/bom/comments")
async def get_bom_comments(product_ids: str = Query(..., description="Comma-separated product IDs")):
    """Read-only lookup of SAP Material Attachment comments (e.g. ECR
    notes) for BOM Explorer - ALWAYS served from the `component_master`
    cache (never a live SAP call here), populated by the same background
    drawing-URL backfill scheduler that fetches drawing_url (both come
    from the same AttachmentFolder.Document SAP node, see
    bom_categorizer.backfill_drawing_urls). Returns only the product_ids
    that actually have at least one non-empty comment on file - anything
    absent simply has none (or hasn't been checked by the background job
    yet). Each value is a list of {"title", "type_label", "comment"}."""
    ids = [p.strip() for p in product_ids.split(",") if p.strip()]
    if not ids:
        return {}
    docs = db["component_master"].find(
        {"_id": {"$in": ids}, "comments": {"$exists": True, "$ne": []}}, {"_id": 1, "comments": 1}
    )
    return {doc["_id"]: doc["comments"] for doc in docs}


@api_router.get("/bom/net-weight")
async def get_bom_net_weight(product_ids: str = Query(..., description="Comma-separated product IDs")):
    """Read-only lookup of each component's Net Weight (kg) for BOM
    Explorer - served from the local `component_master` cache only (set
    either manually via the Admin page's "Weight & Surface Area" dialog,
    or pulled from SAP's custom "Item Net Weight" field once the SAP admin
    links it to QueryMaterialIn - see SAP_MATERIAL_FIELD_SETUP_REQUEST.md).
    Returns only product_ids that have a value set."""
    ids = [p.strip() for p in product_ids.split(",") if p.strip()]
    if not ids:
        return {}
    docs = db["component_master"].find(
        {"_id": {"$in": ids}, "net_weight_kg": {"$ne": None}}, {"_id": 1, "net_weight_kg": 1}
    )
    return {doc["_id"]: doc["net_weight_kg"] for doc in docs}


class RefreshAttachmentsRequest(BaseModel):
    product_ids: List[str]


@api_router.post("/bom/refresh-attachments")
async def refresh_bom_attachments(payload: RefreshAttachmentsRequest):
    """On-demand refresh of drawing_url + attachment comments (ECNs) for a
    bounded set of product_ids - triggered by BOM Explorer's "Refresh
    Attachments" toolbar button on whichever BOM is currently open.
    Unlike the passive background backfill (which only ever checks
    components never seen before, on its own slow schedule), this ALWAYS
    re-fetches live from SAP for every id given - lets a user see a
    brand-new SAP comment/ECR immediately instead of waiting for the
    background job's turn. Capped at REFRESH_ATTACHMENTS_MAX_IDS ids per
    call to protect this SAP tenant from a single click hammering it.
    Returns {"checked", "found", "failed"}."""
    ids = list(dict.fromkeys(p.strip() for p in payload.product_ids if p.strip()))[:REFRESH_ATTACHMENTS_MAX_IDS]
    if not ids:
        return {"checked": 0, "found": 0, "failed": 0}
    stats = await asyncio.to_thread(refresh_attachments_now, db, sap_material_client, ids)
    return stats


class CostEstimateRunRequest(BaseModel):
    product_id: str
    product_uuid: Optional[str] = None


class CostEstimateRunResponse(BaseModel):
    run_id: Optional[str] = None
    run_uuid: Optional[str] = None
    transaction_id: Optional[str] = None


@api_router.post("/sap/cost-estimate-run", response_model=CostEstimateRunResponse)
async def run_cost_estimate(payload: CostEstimateRunRequest):
    """Manually triggers a real SAP Cost Estimate Run for a single material,
    for use next to BOM Explorer's rolled-up (not-yet-costed) Std Cost
    warning. Resolves the material UUID via QueryMaterialIn if the caller
    didn't already have it cached on the BOM node."""
    material_uuid = payload.product_uuid
    if not material_uuid:
        try:
            material_uuid = await asyncio.to_thread(sap_material_client.resolve_uuid, payload.product_id)
        except SAPMaterialError as e:
            raise HTTPException(status_code=400, detail=f"Could not resolve {payload.product_id} in SAP: {e}")
        if not material_uuid:
            raise HTTPException(status_code=404, detail=f"Material '{payload.product_id}' not found in SAP")

    try:
        result = await asyncio.to_thread(
            sap_cost_estimate_client.run_cost_estimate, material_uuid, f"Emergent App Cost Estimate {payload.product_id}"
        )
    except SAPCostEstimateError as e:
        detail = str(e)
        if e.transaction_id:
            detail += f" — give this Transaction ID to your SAP Basis/Admin team so they can pull the exact cause from the provider-side web service error log: {e.transaction_id}"
        # NOTE: intentionally 400, not 502/503/504 - the platform's Cloudflare
        # ingress swallows those specific gateway-error status codes and
        # substitutes its own generic HTML error page, silently discarding
        # our JSON detail before it ever reaches the frontend (confirmed by
        # comparing a direct localhost:8001 call, which DOES return our JSON,
        # against the same call through the public ingress, which returned a
        # Cloudflare HTML page instead). A plain 400 passes through intact.
        raise HTTPException(status_code=400, detail=detail)

    return CostEstimateRunResponse(run_id=result.get("run_id"), run_uuid=result.get("uuid"))


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
        raise HTTPException(status_code=400, detail=str(e))
    return SalesPlanResponse(month=validated_month, items=items)


class InventoryLocation(BaseModel):
    site: Optional[str] = None
    logistics_area: Optional[str] = None
    stock_status: Optional[str] = None
    qty: float
    company_code: Optional[str] = None
    company_name: Optional[str] = None


class InventoryItem(BaseModel):
    product_id: str
    description: Optional[str] = None
    category: Optional[str] = None
    total_qty: float
    uom: Optional[str] = None
    unit_cost: Optional[float] = None
    currency: Optional[str] = None
    total_value: Optional[float] = None
    no_bom: bool = False
    historical_bom: bool = False
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


class L1L2ReportItem(BaseModel):
    root_product_id: str
    parent_product_id: str
    level: int
    product_id: str
    description: Optional[str] = None
    quantity: Optional[float] = None
    unit_of_measure: Optional[str] = None
    line_item_group_id: Optional[str] = None
    line_item_id: Optional[str] = None


class L1L2Report(BaseModel):
    items: List[L1L2ReportItem]
    roots_scanned: int
    roots_with_bom: int
    updated_at: Optional[datetime] = None


class L1L2ReportJobStatus(BaseModel):
    job_id: str
    status: str  # "running" | "done" | "failed"
    result: Optional[L1L2Report] = None
    error: Optional[str] = None


@api_router.get("/admin/l1-l2-report", response_model=L1L2Report)
async def get_l1_l2_report():
    """Instant read of the last-generated report - empty items/None
    updated_at if it's never been generated yet."""
    return l1_l2_report_service.get_cached_report(db)


@api_router.post("/admin/l1-l2-report/generate")
async def start_l1_l2_report_generation():
    """Full fresh sweep: every material in SAP's own On-Hand Inventory feed
    plus every already-known BOM root, expanding Level 1 and Level 2 line
    items (cache-first, live SAP fallback only for anything never explored
    before - see l1_l2_report_service.build_l1_l2_report). Runs as a
    background job since a full live sweep of the catalog can take
    several minutes."""
    job_id = str(uuid.uuid4())
    job_store.create_job(db, job_id, {"status": "running", "result": None, "error": None})

    async def run():
        try:
            result = await asyncio.to_thread(l1_l2_report_service.build_l1_l2_report, sap_soap_client, sap_inventory_client, db)
            job_store.update_job(db, job_id, {
                "status": "done",
                "result": {**result, "updated_at": result["updated_at"].isoformat()},
                "error": None,
            })
        except SAPInventoryError as e:
            job_store.update_job(db, job_id, {"status": "failed", "result": None, "error": str(e)})
        except Exception as e:
            logger.error(f"L1/L2 report generation failed: {e}")
            job_store.update_job(db, job_id, {"status": "failed", "result": None, "error": str(e)})

    asyncio.create_task(run())
    return {"job_id": job_id}


@api_router.get("/admin/l1-l2-report/generate/{job_id}", response_model=L1L2ReportJobStatus)
async def get_l1_l2_report_job(job_id: str):
    job = job_store.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return L1L2ReportJobStatus(job_id=job_id, **job)


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
    up-to-the-minute source of truth for target ship dates. On success,
    also silently autosaves the result (autosave_store) so navigating away
    and back never loses what was last fetched - see the /autosave route
    below."""
    try:
        feed = await asyncio.to_thread(open_po_client.get_open_po_demand, customer, plant)
    except OpenPODemandError as e:
        raise HTTPException(status_code=400, detail=str(e))
    response = OpenPoDemandResponse(count=feed.get("count", 0), max_changed_at=feed.get("max_changed_at"), truncated=feed.get("truncated", False), rows=feed.get("rows", []))
    try:
        await asyncio.to_thread(
            autosave_store.set_autosave, db, "open_po_demand",
            {"customer": customer, "plant": plant, **response.dict()},
        )
    except Exception as e:
        logger.warning(f"Open PO Demand autosave write failed (non-fatal, live data still returned): {e}")
    return response


class OpenPoDemandAutosaveResponse(BaseModel):
    found: bool
    customer: Optional[str] = None
    plant: Optional[str] = None
    count: int = 0
    max_changed_at: Optional[str] = None
    truncated: bool = False
    rows: List[OpenPoRow] = []
    created_at: Optional[str] = None


@api_router.get("/production-plan/open-po-demand/autosave", response_model=OpenPoDemandAutosaveResponse)
async def get_open_po_demand_autosave():
    """The last Open PO Demand fetch, silently autosaved server-side - lets
    the tab restore exactly what was last seen after navigating away and
    back, without needing to search again. Always labeled in the UI as a
    past snapshot; a fresh search still hits the live feed as usual."""
    doc = await asyncio.to_thread(autosave_store.get_autosave, db, "open_po_demand")
    if not doc:
        return OpenPoDemandAutosaveResponse(found=False)
    return OpenPoDemandAutosaveResponse(found=True, created_at=doc["created_at"].isoformat(), **doc["data"])


class MpsDemandLine(BaseModel):
    internal_pono: Optional[float] = None
    customer_po: Optional[str] = None
    customer: Optional[str] = None
    target_ship_date: Optional[str] = None
    lead_day: Optional[float] = None
    production_start_date: Optional[str] = None
    qty_open: float
    net_qty: float


class MpsFinishedGood(BaseModel):
    item_code: str
    description: Optional[str] = None
    ams: float = 0.0
    safety_stock_qty: float = 0.0
    on_hand_qty: Optional[float] = None
    total_gross_qty: float
    total_net_qty: float
    demand_lines: List[MpsDemandLine]


class MpsPlanResponse(BaseModel):
    generated_at: str
    po_data_as_of: Optional[str] = None
    total_open_po_lines: int = 0
    total_selected_po_lines: int = 0
    fgs: List[MpsFinishedGood]


class MpsPlanJobStatus(BaseModel):
    job_id: str
    status: str
    result: Optional[MpsPlanResponse] = None
    error: Optional[str] = None


@api_router.post("/production-plan/mps/generate")
async def start_mps_plan_job(customer: Optional[str] = None, actor: Optional[str] = None):
    """Kicks off the Production Plan (Tier 1 of the MRP II cascade - see
    mps_service.py) computation as a background job - same pattern as
    Purchasing Plan/MRP generation. This is a DRAFT only; nothing is
    committed until it's explicitly locked (see /mps/lock below). On
    success, also silently autosaves the draft (autosave_store, including
    this job_id so a restored draft can still be locked) so navigating
    away and back never loses the last-generated draft."""
    job_id = str(uuid.uuid4())
    job_store.create_job(db, job_id, {"status": "running", "result": None, "error": None})

    async def run():
        try:
            result = await asyncio.to_thread(mps_service.build_production_plan, open_po_client, oms_client, db, customer, forecast_demand_client)
            job_store.update_job(db, job_id, {"status": "done", "result": result, "error": None})
            await asyncio.to_thread(autosave_store.set_autosave, db, "mps_draft", {"job_id": job_id, "customer": customer, "result": result}, actor)
        except Exception as e:
            logger.error(f"Production Plan (MPS) generation failed: {e}")
            job_store.update_job(db, job_id, {"status": "failed", "result": None, "error": str(e)})

    asyncio.create_task(run())
    return {"job_id": job_id}


@api_router.get("/production-plan/mps/status/{job_id}", response_model=MpsPlanJobStatus)
async def mps_plan_status(job_id: str):
    job = job_store.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return MpsPlanJobStatus(
        job_id=job_id, status=job["status"],
        result=MpsPlanResponse(**job["result"]) if job["result"] else None,
        error=job["error"],
    )


class MpsAutosaveResponse(BaseModel):
    found: bool
    job_id: Optional[str] = None
    customer: Optional[str] = None
    result: Optional[MpsPlanResponse] = None
    created_at: Optional[str] = None
    created_by: Optional[str] = None


@api_router.get("/production-plan/mps/autosave", response_model=MpsAutosaveResponse)
async def get_mps_autosave():
    """The last Production Plan draft that finished generating, silently
    autosaved server-side - lets the Sales & Production Plan tab restore
    exactly what was last seen (including its job_id, so the restored
    draft can still be Locked) after navigating away and back, without
    needing to click Generate Draft again."""
    doc = await asyncio.to_thread(autosave_store.get_autosave, db, "mps_draft")
    if not doc:
        return MpsAutosaveResponse(found=False)
    data = doc["data"]
    try:
        result = MpsPlanResponse(**data["result"])
    except ValidationError:
        # Stale autosave from before a schema change - treat as "nothing to
        # restore" instead of crashing; a fresh Generate Draft overwrites it.
        return MpsAutosaveResponse(found=False)
    return MpsAutosaveResponse(
        found=True, job_id=data.get("job_id"), customer=data.get("customer"), result=result,
        created_at=doc["created_at"].isoformat(), created_by=doc.get("created_by"),
    )


class LockMpsPlanRequest(BaseModel):
    job_id: str
    locked_by: Optional[str] = None


class LockMpsPlanResponse(BaseModel):
    locked_plan_id: str
    locked_at: str
    fg_count: int


@api_router.post("/production-plan/mps/lock", response_model=LockMpsPlanResponse)
async def lock_mps_plan(payload: LockMpsPlanRequest):
    """Commits a just-generated draft (by job_id, so exactly the snapshot
    the user reviewed gets locked - not a fresh, possibly-different
    recompute) as the new stable Production Plan every downstream MRP run
    reads from until the next lock."""
    job = job_store.get_job(db, payload.job_id)
    if job is None or job["status"] != "done" or not job["result"]:
        raise HTTPException(status_code=400, detail="job_id must refer to a completed Production Plan draft")
    doc = await asyncio.to_thread(mps_service.lock_production_plan, db, job["result"], payload.locked_by)
    return LockMpsPlanResponse(locked_plan_id=str(doc["_id"]), locked_at=doc["locked_at"].isoformat(), fg_count=len(doc["fgs"]))


class MpsLockMeta(BaseModel):
    id: str
    locked_at: str
    locked_by: Optional[str] = None
    po_data_as_of: Optional[str] = None
    fg_count: int = 0


@api_router.get("/production-plan/mps/locks", response_model=List[MpsLockMeta])
async def list_mps_locks():
    docs = await asyncio.to_thread(mps_service.list_locked_plans, db)
    return [
        MpsLockMeta(id=str(d["_id"]), locked_at=d["locked_at"].isoformat(), locked_by=d.get("locked_by"),
                    po_data_as_of=d.get("po_data_as_of"), fg_count=d.get("fg_count", 0))
        for d in docs
    ]


@api_router.get("/production-plan/mps/locks/latest", response_model=MpsPlanResponse)
async def get_latest_mps_lock():
    doc = await asyncio.to_thread(mps_service.get_latest_locked_plan, db)
    if not doc:
        raise HTTPException(status_code=404, detail="No Production Plan has been locked yet")
    return MpsPlanResponse(
        generated_at=doc["generated_at"], po_data_as_of=doc.get("po_data_as_of"),
        total_open_po_lines=doc.get("total_open_po_lines", 0), total_selected_po_lines=doc.get("total_selected_po_lines", 0),
        fgs=doc["fgs"],
    )


class MrpDemandLine(BaseModel):
    internal_pono: Optional[float] = None
    customer_po: Optional[str] = None
    customer: Optional[str] = None
    item_code: str
    target_ship_date: Optional[str] = None
    need_by_date: Optional[str] = None
    order_by_date: Optional[str] = None
    gross_qty: float
    net_qty: float


class MrpComponent(BaseModel):
    product_id: str
    description: Optional[str] = None
    unit_of_measure: Optional[str] = None
    lead_time_days: Optional[float] = None
    lead_time_is_default: bool = False
    dynamic_msl: Optional[float] = None
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
    locked_plan_id: str
    locked_at: str
    unresolved_items: List[UnresolvedMrpItem]
    components: List[MrpComponent]


class MrpPlanJobStatus(BaseModel):
    job_id: str
    status: str  # "running" | "done" | "failed"
    result: Optional[MrpPlanResponse] = None
    error: Optional[str] = None


@api_router.post("/production-plan/mrp/generate")
async def start_mrp_plan_job(actor: Optional[str] = None):
    """Kicks off the MRP computation (see mrp_service.build_mrp_plan) as a
    background job - same pattern as Purchasing Plan generation, since
    exploding every locked FG's BOM against live SAP can take a while for
    item_codes not already warm in the BOM cache. Always explodes the
    LATEST LOCKED Production Plan snapshot (see mps_service.py) - fails
    clearly if nothing has been locked yet. On success, also silently
    autosaves the result (mrp_plan_store) so a page refresh never loses
    the last-generated plan."""
    job_id = str(uuid.uuid4())
    job_store.create_job(db, job_id, {"status": "running", "result": None, "error": None})

    async def run():
        try:
            result = await asyncio.to_thread(mrp_service.build_mrp_plan, sap_soap_client, db)
            job_store.update_job(db, job_id, {"status": "done", "result": result, "error": None})
            await asyncio.to_thread(mrp_plan_store.set_autosave, db, result, actor)
        except mrp_service.NoLockedPlanError as e:
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
    try:
        plan = MrpPlanResponse(**doc["plan"])
    except ValidationError:
        # Stale autosave from before a schema change (e.g. the Tier-2-from-lock
        # migration added required fields) - treat as "nothing to restore"
        # instead of crashing; a fresh Generate will overwrite it.
        return MrpAutosaveResponse(found=False)
    return MrpAutosaveResponse(
        found=True, plan=plan,
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
    locked_plan_id: Optional[str] = None
    components_count: int
    total_net_qty: float


class SavedMrpPlansListResponse(BaseModel):
    plans: List[SavedMrpPlanMeta]


def _saved_plan_meta(doc) -> SavedMrpPlanMeta:
    plan = doc["plan"]
    return SavedMrpPlanMeta(
        id=doc["_id"], name=doc["name"], created_by=doc.get("created_by"),
        created_at=doc["created_at"].isoformat(),
        locked_plan_id=plan.get("locked_plan_id"),
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
    net_weight_kg: Optional[float] = None
    surface_area_sqin: Optional[float] = None
    sap_physical_pushed_at: Optional[str] = None


def _to_component_master_item(doc: dict) -> ComponentMasterItem:
    updated_at = doc.get("categorized_at") or doc.get("created_at")
    sap_pushed_at = doc.get("sap_pushed_at")
    sap_physical_pushed_at = doc.get("sap_physical_pushed_at")
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
        net_weight_kg=doc.get("net_weight_kg"),
        surface_area_sqin=doc.get("surface_area_sqin"),
        sap_physical_pushed_at=sap_physical_pushed_at.isoformat() if sap_physical_pushed_at else None,
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
    net_weight_kg: Optional[float] = None
    surface_area_sqin: Optional[float] = None


@api_router.patch("/admin/components/{product_id}", response_model=ComponentMasterItem)
async def update_admin_component(product_id: str, payload: UpdateComponentMasterRequest):
    """Manually correct a component's category (marks it category_source
    "manual" so it's never overwritten by a bulk Re-Categorise) and/or set
    its Minimum Stock Level, which the Purchasing Plan's netting math reads
    directly (see purchasing_plan.get_component_msl), and/or its Procurement
    Lead Time (used only by the SAP Push-to-SAP write-back), and/or its
    Net Weight/Surface Area (local values used by the separate "Push
    Weight & Surface Area to SAP" write-back)."""
    update = {}
    if payload.category is not None:
        update["category"] = payload.category
        update["category_source"] = "manual"
        update["categorized_at"] = datetime.now(timezone.utc)
    if payload.msl is not None:
        update["msl"] = payload.msl
    if payload.lead_time_days is not None:
        update["lead_time_days"] = payload.lead_time_days
    for field in PHYSICAL_FIELD_TO_SAP_PROPERTY:
        value = getattr(payload, field)
        if value is not None:
            update[field] = value
    if not update:
        raise HTTPException(status_code=400, detail="Provide at least one field to update")

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
        raise HTTPException(status_code=400, detail=str(e))

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
        raise HTTPException(status_code=400, detail=str(e))

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
        raise HTTPException(status_code=400, detail=str(e))

    await asyncio.to_thread(
        db["component_master"].update_one,
        {"_id": product_id},
        {"$set": {"sap_pushed_at": datetime.now(timezone.utc)}},
    )
    return PushToSapResponse(planning_areas_updated=updated, safety_stock=msl, lead_time_days=lead_time_days)


class SapPhysicalAttributesResponse(BaseModel):
    attributes: dict = {}


@api_router.get("/admin/components/{product_id}/sap-physical-attributes", response_model=SapPhysicalAttributesResponse)
async def get_sap_physical_attributes(product_id: str):
    """Live-reads SAP's current Net Weight/Surface Area for this component
    via the custom "materialgeneralinfo" OData service - used by the
    Admin page's "Weight & Surface Area" dialog to show Current SAP vs
    Pushing before an operator decides to push. An empty `attributes`
    dict just means neither value is set on this material yet - not an
    error."""
    try:
        result = await asyncio.to_thread(sap_material_physical_client.get_physical_attributes, product_id)
    except SAPMaterialPhysicalError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not result:
        raise HTTPException(status_code=404, detail="SAP has no material with this ID")
    return SapPhysicalAttributesResponse(attributes=result["attributes"])


class PushPhysicalAttributesResponse(BaseModel):
    pushed_fields: List[str]


@api_router.post("/admin/components/{product_id}/push-physical-attributes-to-sap", response_model=PushPhysicalAttributesResponse)
async def push_physical_attributes_to_sap(product_id: str):
    """Pushes this component's LOCAL Net Weight/Surface Area -> SAP via
    the custom "materialgeneralinfo" OData service (PATCH, live-confirmed
    working end-to-end 18 Aug 2026)."""
    doc = await asyncio.to_thread(db["component_master"].find_one, {"_id": product_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Unknown component")
    values = {field: doc.get(field) for field in PHYSICAL_FIELD_TO_SAP_PROPERTY}
    if not any(v is not None for v in values.values()):
        raise HTTPException(status_code=400, detail="Set Net Weight and/or Surface Area for this component before pushing")

    try:
        await asyncio.to_thread(sap_material_physical_client.push_physical_attributes, product_id, values)
    except SAPMaterialPhysicalError as e:
        raise HTTPException(status_code=400, detail=str(e))

    pushed_fields = [f for f, v in values.items() if v is not None]
    await asyncio.to_thread(
        db["component_master"].update_one,
        {"_id": product_id},
        {"$set": {"sap_physical_pushed_at": datetime.now(timezone.utc)}},
    )
    return PushPhysicalAttributesResponse(pushed_fields=pushed_fields)


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


# ---------------------------------------------------------------------------
# Suppliers - fully self-managed (not synced with SAP; the tenant has no
# Source List / Approved Supplier List). Lets the user record which
# supplier(s) can provide a part, at what quota split, lead time, and price,
# to inform how a purchase requisition's quantity should be divided when
# generating one from the Purchasing Plan.
# ---------------------------------------------------------------------------

class SupplierCreate(BaseModel):
    name: str
    contact_person: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None


class SupplierUpdate(BaseModel):
    name: Optional[str] = None
    contact_person: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None


class Supplier(BaseModel):
    id: str
    name: str
    contact_person: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    sap_internal_id: Optional[str] = None
    source: str = "local"
    created_at: str
    updated_at: str


def _supplier_to_response(doc: dict) -> Supplier:
    return Supplier(
        id=doc["_id"], name=doc["name"], contact_person=doc.get("contact_person"),
        email=doc.get("email"), phone=doc.get("phone"),
        sap_internal_id=doc.get("sap_internal_id"), source=doc.get("source", "local"),
        created_at=doc["created_at"].isoformat(), updated_at=doc["updated_at"].isoformat(),
    )


class SyncSuppliersFromSapResult(BaseModel):
    created: int
    updated: int


class SyncSuppliersFromSapJobStatus(BaseModel):
    job_id: str
    status: str  # "running" | "done" | "failed"
    result: Optional[SyncSuppliersFromSapResult] = None
    error: Optional[str] = None


def _run_sap_supplier_sync(db):
    """Runs in a worker thread - raises SAPSupplierAuthError/
    SAPSupplierNotConfiguredError/SAPSupplierError on failure, letting the
    async wrapper turn that into a clear job error message."""
    sap_suppliers = sap_supplier_client.list_suppliers()
    return supplier_service.sync_suppliers_from_sap(db, sap_suppliers)


@api_router.post("/suppliers/sync-from-sap")
async def sync_suppliers_from_sap():
    """Pulls the full Supplier master list live from SAP (QuerySupplierIn -
    ~3000 suppliers in this tenant, takes ~60-90s) and upserts into the
    local suppliers collection, matched by sap_internal_id. Purely-local
    suppliers (no SAP link) are left untouched. Runs as a background job
    (same pattern as Purchasing Plan/MRP/Push-All-to-SAP) since a single
    HTTP request would exceed the platform's ingress timeout. See
    /app/SAP_SUPPLIER_SYNC_AUTHORIZATION_REQUEST.md if this fails - it means
    the SAP admin needs to activate/authorize the Query Supplier service."""
    job_id = str(uuid.uuid4())
    job_store.create_job(db, job_id, {"status": "running", "result": None, "error": None})

    async def run():
        try:
            result = await asyncio.to_thread(_run_sap_supplier_sync, db)
            job_store.update_job(db, job_id, {"status": "done", "result": result, "error": None})
        except SAPSupplierAuthError:
            job_store.update_job(db, job_id, {
                "status": "failed", "result": None,
                "error": "SAP rejected this call due to a missing authorization role for Query Supplier. "
                         "Ask your SAP admin to authorize the _EMERGENTBOM technical user for QuerySupplierIn "
                         "on its existing Communication Arrangement.",
            })
        except SAPSupplierNotConfiguredError:
            job_store.update_job(db, job_id, {
                "status": "failed", "result": None,
                "error": "SAP has no active Communication Arrangement for Query Supplier yet. "
                         "See /app/SAP_SUPPLIER_SYNC_AUTHORIZATION_REQUEST.md for exact setup steps.",
            })
        except SAPSupplierError as e:
            job_store.update_job(db, job_id, {"status": "failed", "result": None, "error": f"SAP error: {e}"})
        except Exception as e:
            logger.error(f"Supplier sync from SAP failed: {e}")
            job_store.update_job(db, job_id, {"status": "failed", "result": None, "error": str(e)})

    asyncio.create_task(run())
    return {"job_id": job_id}


@api_router.get("/suppliers/sync-from-sap/status/{job_id}", response_model=SyncSuppliersFromSapJobStatus)
async def get_sync_suppliers_from_sap_status(job_id: str):
    job = job_store.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return SyncSuppliersFromSapJobStatus(job_id=job_id, **job)


@api_router.get("/suppliers", response_model=List[Supplier])
async def get_suppliers():
    docs = await asyncio.to_thread(supplier_service.list_suppliers, db)
    return [_supplier_to_response(d) for d in docs]


@api_router.post("/suppliers", response_model=Supplier)
async def add_supplier(payload: SupplierCreate):
    doc = await asyncio.to_thread(
        supplier_service.create_supplier, db, payload.name, payload.contact_person, payload.email, payload.phone
    )
    return _supplier_to_response(doc)


@api_router.patch("/suppliers/{supplier_id}", response_model=Supplier)
async def patch_supplier(supplier_id: str, payload: SupplierUpdate):
    if not supplier_service.get_supplier(db, supplier_id):
        raise HTTPException(status_code=404, detail="Supplier not found")
    updates = {k: v for k, v in payload.dict().items() if v is not None}
    doc = await asyncio.to_thread(supplier_service.update_supplier, db, supplier_id, updates)
    return _supplier_to_response(doc)


@api_router.delete("/suppliers/{supplier_id}")
async def remove_supplier(supplier_id: str):
    ok = await asyncio.to_thread(supplier_service.delete_supplier, db, supplier_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Supplier not found")
    return {"deleted": True}


class PartSupplierCreate(BaseModel):
    product_id: str
    supplier_id: str
    quota_percent: Optional[float] = None
    lead_time_days: Optional[float] = None
    unit_price: Optional[float] = None
    currency: Optional[str] = "INR"
    preference: str = "Preferred"  # "Preferred" | "Backup"
    notes: Optional[str] = None


class PartSupplierUpdate(BaseModel):
    quota_percent: Optional[float] = None
    lead_time_days: Optional[float] = None
    unit_price: Optional[float] = None
    currency: Optional[str] = None
    preference: Optional[str] = None
    notes: Optional[str] = None


class PartSupplierAssignment(BaseModel):
    id: str
    product_id: str
    supplier_id: str
    supplier_name: Optional[str] = None
    quota_percent: Optional[float] = None
    lead_time_days: Optional[float] = None
    unit_price: Optional[float] = None
    currency: Optional[str] = None
    preference: str
    notes: Optional[str] = None
    created_at: str
    updated_at: str


def _assignment_to_response(doc: dict, supplier_name: Optional[str] = None) -> PartSupplierAssignment:
    return PartSupplierAssignment(
        id=doc["_id"], product_id=doc["product_id"], supplier_id=doc["supplier_id"],
        supplier_name=supplier_name, quota_percent=doc.get("quota_percent"),
        lead_time_days=doc.get("lead_time_days"), unit_price=doc.get("unit_price"),
        currency=doc.get("currency"), preference=doc.get("preference", "Preferred"),
        notes=doc.get("notes"), created_at=doc["created_at"].isoformat(), updated_at=doc["updated_at"].isoformat(),
    )


@api_router.get("/part-suppliers", response_model=List[PartSupplierAssignment])
async def get_part_suppliers(product_id: str = Query(...)):
    docs = await asyncio.to_thread(supplier_service.list_suppliers_for_part, db, product_id)
    suppliers = {s["_id"]: s["name"] for s in db[supplier_service.SUPPLIERS_COLLECTION].find({}, {"name": 1})}
    return [_assignment_to_response(d, suppliers.get(d["supplier_id"])) for d in docs]


@api_router.get("/part-suppliers/bulk")
async def get_part_suppliers_bulk(product_ids: str = Query(..., description="Comma-separated product IDs")):
    ids = [p.strip() for p in product_ids.split(",") if p.strip()]
    grouped = await asyncio.to_thread(supplier_service.list_suppliers_for_parts, db, ids)
    suppliers = {s["_id"]: s["name"] for s in db[supplier_service.SUPPLIERS_COLLECTION].find({}, {"name": 1})}
    return {
        pid: [_assignment_to_response(d, suppliers.get(d["supplier_id"])) for d in assignments]
        for pid, assignments in grouped.items()
    }


class ProductSuggestion(BaseModel):
    product_id: str
    description: Optional[str] = None


@api_router.get("/products/search", response_model=List[ProductSuggestion])
async def search_products(q: str = Query(..., min_length=1), limit: int = 10):
    """Live autosuggest for Product ID fields (Suppliers page) - backed by
    the `component_master` Mongo cache (3200+ known parts across all BOMs,
    already synced with description text) rather than a live SAP call per
    keystroke. Matches on Product ID prefix OR a description substring."""
    q_escaped = re.escape(q.strip())
    cursor = db["component_master"].find(
        {"$or": [
            {"_id": {"$regex": f"^{q_escaped}", "$options": "i"}},
            {"description": {"$regex": q_escaped, "$options": "i"}},
        ]},
        {"_id": 1, "description": 1},
    ).limit(limit)
    return [ProductSuggestion(product_id=d["_id"], description=d.get("description")) for d in cursor]


@api_router.post("/part-suppliers", response_model=PartSupplierAssignment)
async def add_part_supplier(payload: PartSupplierCreate):
    supplier = supplier_service.get_supplier(db, payload.supplier_id)
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")
    doc = await asyncio.to_thread(
        supplier_service.assign_supplier_to_part, db, payload.product_id, payload.supplier_id,
        payload.quota_percent, payload.lead_time_days, payload.unit_price, payload.currency,
        payload.preference, payload.notes,
    )
    return _assignment_to_response(doc, supplier["name"])


@api_router.patch("/part-suppliers/{assignment_id}", response_model=PartSupplierAssignment)
async def patch_part_supplier(assignment_id: str, payload: PartSupplierUpdate):
    existing = supplier_service.get_part_supplier(db, assignment_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Assignment not found")
    updates = {k: v for k, v in payload.dict().items() if v is not None}
    doc = await asyncio.to_thread(supplier_service.update_part_supplier, db, assignment_id, updates)
    supplier = supplier_service.get_supplier(db, doc["supplier_id"])
    return _assignment_to_response(doc, supplier["name"] if supplier else None)


@api_router.delete("/part-suppliers/{assignment_id}")
async def remove_part_supplier(assignment_id: str):
    ok = await asyncio.to_thread(supplier_service.delete_part_supplier, db, assignment_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Quota Arrangements - AI-assisted, revision-tracked replacement for the old
# simple part_suppliers table. See quota_arrangement_service.py.
# ---------------------------------------------------------------------------

class QuotaAllocation(BaseModel):
    supplier_id: Optional[str] = None
    supplier_name: Optional[str] = None
    sap_internal_id: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = None
    price_source: Optional[str] = None
    lead_time_days: Optional[float] = None
    quota_percent: float
    rationale: Optional[str] = None


class SuggestQuotaResponse(BaseModel):
    allocations: List[QuotaAllocation]
    overall_rationale: Optional[str] = None


@api_router.post("/quota-arrangements/{product_id}/suggest", response_model=SuggestQuotaResponse)
async def suggest_quota(product_id: str):
    """Gathers every known candidate supplier for this part (local
    assignments, ERP billing history, SAP Supplier Invoices, SAP Released
    Price Specs) and asks an LLM to suggest a fair quota % split weighted
    by price + lead time (quality/OTIF are None placeholders today - see
    module docstring). Does NOT save anything - the buyer reviews/edits
    the suggestion, then calls /confirm."""
    candidates = await asyncio.to_thread(
        quota_arrangement_service.gather_candidate_suppliers, db, product_id,
        price_explorer_client, sap_price_spec_client, sap_supplier_invoice_client,
    )
    result = await asyncio.to_thread(
        quota_arrangement_service.suggest_allocation, product_id, candidates, os.environ['EMERGENT_LLM_KEY'],
    )
    return SuggestQuotaResponse(**result)


class QuotaRevision(BaseModel):
    id: str
    arrangement_id: str
    revision_no: int
    source: str
    remarks: Optional[str] = None
    created_by: Optional[str] = None
    created_at: str
    allocations: List[QuotaAllocation]


class QuotaArrangementResponse(BaseModel):
    arrangement_id: Optional[str] = None
    product_id: str
    current_revision_no: int = 0
    latest_revision: Optional[QuotaRevision] = None
    revisions: List[QuotaRevision] = []


def _revision_to_model(r: dict) -> QuotaRevision:
    return QuotaRevision(
        id=r["_id"], arrangement_id=r["arrangement_id"], revision_no=r["revision_no"], source=r["source"],
        remarks=r.get("remarks"), created_by=r.get("created_by"), created_at=r["created_at"].isoformat(),
        allocations=[QuotaAllocation(**a) for a in r["allocations"]],
    )


@api_router.get("/quota-arrangements/{product_id}", response_model=QuotaArrangementResponse)
async def get_quota_arrangement(product_id: str):
    result = await asyncio.to_thread(quota_arrangement_service.get_arrangement, db, product_id)
    arrangement = result["arrangement"]
    return QuotaArrangementResponse(
        arrangement_id=arrangement["_id"] if arrangement else None,
        product_id=product_id,
        current_revision_no=arrangement["current_revision_no"] if arrangement else 0,
        latest_revision=_revision_to_model(result["latest_revision"]) if result["latest_revision"] else None,
        revisions=[_revision_to_model(r) for r in result["revisions"]],
    )


class ConfirmQuotaRequest(BaseModel):
    allocations: List[QuotaAllocation]
    remarks: Optional[str] = None
    source: str = "user"  # "ai" (confirmed as-is) | "user" (buyer edited)
    created_by: Optional[str] = None


@api_router.post("/quota-arrangements/{product_id}/confirm", response_model=QuotaArrangementResponse)
async def confirm_quota(product_id: str, payload: ConfirmQuotaRequest):
    total = sum(a.quota_percent for a in payload.allocations)
    if payload.allocations and abs(total - 100.0) > 0.5:
        raise HTTPException(status_code=400, detail=f"Quota percentages must sum to 100 (got {total})")
    result = await asyncio.to_thread(
        quota_arrangement_service.confirm_arrangement, db, product_id,
        [a.dict() for a in payload.allocations], payload.remarks, payload.source, payload.created_by,
    )
    arrangement = result["arrangement"]
    return QuotaArrangementResponse(
        arrangement_id=arrangement["_id"], product_id=product_id, current_revision_no=arrangement["current_revision_no"],
        latest_revision=_revision_to_model(result["latest_revision"]) if result["latest_revision"] else None,
        revisions=[_revision_to_model(r) for r in result["revisions"]],
    )


class SapPriceSpec(BaseModel):
    sap_id: str
    supplier_name: Optional[str] = None
    supplier_internal_id: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = None
    unit: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    release_status_code: Optional[str] = None


@api_router.get("/suppliers/sap-price-specs/{product_id}", response_model=List[SapPriceSpec])
async def get_sap_price_specs(product_id: str):
    """Reads real purchasing prices already maintained in SAP for this
    Product ID (via the custom `pricespecificationemergent` OData service -
    SAP's own condition-technique price records, distinct from our local
    part_suppliers assignments). Read-only - see sap_price_spec_client.py."""
    try:
        specs = await asyncio.to_thread(sap_price_spec_client.get_price_specs_for_product, product_id)
    except SAPPriceSpecError as e:
        raise HTTPException(status_code=400, detail=f"SAP error: {e}")
    return [SapPriceSpec(**s) for s in specs]


class SapPriceSpecCreate(BaseModel):
    product_id: str
    supplier_internal_id: str
    price: float
    currency: str = "INR"


@api_router.post("/suppliers/sap-price-specs", response_model=List[SapPriceSpec])
async def create_sap_price_spec(payload: SapPriceSpecCreate):
    """Manually writes a single Procurement Price Specification into SAP
    for one Product ID + one Supplier (SOAP write, see
    sap_price_spec_client.create_price_spec) - the manual counterpart to
    the automated bulk ERP push, for one-off corrections/testing. Returns
    the product's full up-to-date list of SAP price specs after the write
    (re-reads from SAP) so the UI can immediately show the new record."""
    try:
        await asyncio.to_thread(
            sap_price_spec_client.create_price_spec,
            payload.product_id, payload.supplier_internal_id, payload.price, payload.currency,
        )
        specs = await asyncio.to_thread(sap_price_spec_client.get_price_specs_for_product, payload.product_id)
    except SAPPriceSpecError as e:
        raise HTTPException(status_code=400, detail=f"SAP error: {e}")
    return [SapPriceSpec(**s) for s in specs]


class ErpPriceQuote(BaseModel):
    supplier: str
    pcode: str
    rate: float
    bill_date: str


class ErpPriceAverage(BaseModel):
    rate: float
    bill_count: int


class ErpPriceItem(BaseModel):
    icode: str
    iname: Optional[str] = None
    lowest: Optional[ErpPriceQuote] = None
    last: Optional[ErpPriceQuote] = None
    average: Optional[ErpPriceAverage] = None


@api_router.get("/suppliers/erp-prices/{product_id}", response_model=List[ErpPriceItem])
async def get_erp_prices(product_id: str, lookback_days: int = 180):
    """Reads real billed-purchase history (lowest/last/6-month average price
    per supplier) from the company's MS SQL ERP, via the existing separate
    Price Explorer service (see price_explorer_client.py). This reflects
    ACTUAL past purchases, unlike SAP's mostly-unpopulated List Prices."""
    try:
        items = await asyncio.to_thread(price_explorer_client.search, product_id, lookback_days, 5)
    except PriceExplorerError as e:
        raise HTTPException(status_code=400, detail=f"Price Explorer error: {e}")
    return [ErpPriceItem(**i) for i in items]


class SapSupplierInvoiceLine(BaseModel):
    invoice_id: Optional[str] = None
    supplier_invoice_number: Optional[str] = None
    date: Optional[str] = None
    supplier_name: Optional[str] = None
    supplier_internal_id: Optional[str] = None
    quantity: Optional[float] = None
    unit_of_measure: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = None
    document_type: Optional[str] = None
    reverses_invoice_id: Optional[str] = None
    reverses_supplier_invoice_number: Optional[str] = None


@api_router.get("/suppliers/sap-purchase-history/{product_id}", response_model=List[SapSupplierInvoiceLine])
async def get_sap_purchase_history(product_id: str, limit: int = 20):
    """Reads REAL, posted Supplier Invoice line items for this Product ID
    straight from SAP itself (QuerySupplierInvoiceQueryIn) - supplier name,
    price, quantity, invoice date. Authoritative SAP-native proof of
    purchase, independent of the external ERP Price Explorer and of SAP's
    (often unpopulated) Price Specification records - see
    sap_supplier_invoice_client.py."""
    try:
        rows = await asyncio.to_thread(sap_supplier_invoice_client.get_invoices_for_product, product_id, limit)
    except SAPSupplierInvoiceError as e:
        raise HTTPException(status_code=400, detail=f"SAP error: {e}")
    return [SapSupplierInvoiceLine(**r) for r in rows]


class SapGSALine(BaseModel):
    gsa_id: Optional[str] = None
    posting_date: Optional[str] = None
    po_id: Optional[str] = None
    supplier_internal_id: Optional[str] = None
    quantity: Optional[float] = None
    unit_of_measure: Optional[str] = None


@api_router.get("/suppliers/sap-receipt-dates/{product_id}", response_model=List[SapGSALine])
async def get_sap_receipt_dates(product_id: str, limit: int = 20):
    """Reads REAL, posted Goods & Service Acknowledgement (physical goods
    receipt) line items for this Product ID straight from SAP itself
    (QueryGoodsAndServiceAcknowledgementInbound) - a genuinely separate
    document from the Supplier Invoice above (billing date vs. actual
    delivery date can differ) - see sap_gsa_client.py."""
    try:
        rows = await asyncio.to_thread(sap_gsa_client.get_receipt_dates_for_product, product_id, limit)
    except SAPGSAError as e:
        raise HTTPException(status_code=400, detail=f"SAP error: {e}")
    return [SapGSALine(**r) for r in rows]


class BulkPushErpProgress(BaseModel):
    processed: int
    total: int


class BulkPushErpPushedItem(BaseModel):
    product_id: str
    supplier: str
    price: float
    currency: str
    bill_date: Optional[str] = None


class BulkPushErpResult(BaseModel):
    total: int
    pushed: int
    skipped_already_released: int
    skipped_no_erp_data: int
    skipped_unknown_supplier: int
    failed: List[dict]
    pushed_items: List[BulkPushErpPushedItem] = []
    cancelled: bool = False


class BulkPushErpJobStatus(BaseModel):
    job_id: str
    status: str  # "running" | "done" | "failed"
    progress: Optional[BulkPushErpProgress] = None
    result: Optional[BulkPushErpResult] = None
    error: Optional[str] = None


@api_router.post("/suppliers/bulk-push-erp-to-sap")
async def start_bulk_push_erp_to_sap():
    """For every known component, pushes the ERP's most recent real billed
    price+supplier into SAP as a new Procurement Price Specification -
    ONLY for parts that don't already have a Released price in SAP (see
    bulk_push_erp_prices_to_sap docstring). Runs as a background job since
    it iterates ~3000+ parts against two live external services."""
    job_id = str(uuid.uuid4())
    job_store.create_job(db, job_id, {"status": "running", "progress": {"processed": 0, "total": 0}, "result": None, "error": None, "cancel_requested": False})

    last_progress = {"processed": 0, "total": 0}

    def progress_callback(processed, total):
        nonlocal last_progress
        last_progress = {"processed": processed, "total": total}
        job_store.update_job(db, job_id, {"progress": last_progress})

    def is_cancelled():
        job = job_store.get_job(db, job_id)
        return bool(job and job.get("cancel_requested"))

    async def run():
        try:
            result = await asyncio.to_thread(
                bulk_push_erp_prices_to_sap, db, sap_price_spec_client, price_explorer_client, progress_callback, is_cancelled
            )
            job_store.update_job(db, job_id, {"status": "done", "progress": last_progress, "result": result, "error": None})
        except Exception as e:
            logger.error(f"Bulk push ERP prices to SAP failed: {e}")
            job_store.update_job(db, job_id, {"status": "failed", "progress": last_progress, "result": None, "error": str(e)})

    asyncio.create_task(run())
    return {"job_id": job_id}


@api_router.post("/suppliers/bulk-push-erp-to-sap/{job_id}/stop")
async def stop_bulk_push_erp_to_sap(job_id: str):
    """Requests a cooperative stop of a running bulk-push job - already
    in-flight part pushes finish, but no new ones start. The job still
    completes normally (status='done') with a partial result and
    `cancelled: true`, rather than the request itself blocking on it."""
    job = job_store.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    job_store.update_job(db, job_id, {"cancel_requested": True})
    return {"stopping": True}


@api_router.get("/suppliers/bulk-push-erp-to-sap/{job_id}", response_model=BulkPushErpJobStatus)
async def get_bulk_push_erp_to_sap_status(job_id: str):
    job = job_store.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return BulkPushErpJobStatus(job_id=job_id, **job)


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Microsoft Entra ID SSO + page-level access control (Feb 2026) - see
# auth_service.py module docstring. Registered as HTTP middleware (not
# Depends() on each route) so a route added later can't accidentally end
# up unprotected by omission.
app.middleware("http")(auth_service.create_auth_middleware(db))

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


# Background maintenance: a consolidated "Nightly Sync" scheduled for
# 1 AM IST (a genuinely quiet window for this tenant, per user request) -
# runs the heavier full-catalog jobs back-to-back, off business hours,
# instead of at unpredictable/immediate-after-restart times:
#   1. deep_expand_all_known_roots - full recursive re-walk of every known
#      BOM root (see bom_cache_service.py) - closes deeply-nested "No BOM"
#      false positives across the whole catalog over time.
#   2. deep_backfill_uuids - resolves any inventory item still missing a
#      product_uuid (needed for Standard Cost valuation).
#   3. refresh_inventory_cache - live On-Hand Inventory + Standard Costs
#      pull (also already runs every 2h during the day; redundant but
#      harmless here, just guarantees a fresh number right after the
#      nightly BOM/UUID work).
# None of these raise concurrency beyond the existing sap_semaphore cap -
# running them at 1 AM doesn't make any single call heavier, it just moves
# the WHEN so nobody notices if it takes a while.
IST_OFFSET = timedelta(hours=5, minutes=30)
NIGHTLY_SYNC_HOUR_IST = 1


def _seconds_until_next_nightly_sync() -> float:
    now_ist = datetime.now(timezone.utc) + IST_OFFSET
    next_run_ist = now_ist.replace(hour=NIGHTLY_SYNC_HOUR_IST, minute=0, second=0, microsecond=0)
    if next_run_ist <= now_ist:
        next_run_ist += timedelta(days=1)
    return (next_run_ist - now_ist).total_seconds()


@app.on_event("startup")
async def start_nightly_sync_loop():
    async def loop():
        while True:
            await asyncio.sleep(_seconds_until_next_nightly_sync())
            started_at = datetime.now(timezone.utc)
            logger.info("Nightly sync (1 AM IST) starting: deep BOM expansion -> UUID backfill -> inventory/cost refresh")
            try:
                bom_result = await asyncio.to_thread(bom_cache_service.deep_expand_all_known_roots, sap_soap_client, db)
                logger.info(f"Nightly sync: deep BOM expansion complete: {bom_result}")
            except Exception as e:
                logger.error(f"Nightly sync: deep BOM expansion failed: {e}")
            try:
                uuid_result = await asyncio.to_thread(deep_backfill_uuids, db, sap_soap_client, sap_material_client)
                logger.info(f"Nightly sync: UUID backfill complete: {uuid_result}")
            except Exception as e:
                logger.error(f"Nightly sync: UUID backfill failed: {e}")
            try:
                cached = await asyncio.to_thread(refresh_inventory_cache, db, sap_inventory_client, sap_valuation_client)
                logger.info(f"Nightly sync: inventory/cost refresh complete: {len(cached['items'])} item(s)")
            except Exception as e:
                logger.error(f"Nightly sync: inventory/cost refresh failed: {e}")
            logger.info(f"Nightly sync finished in {(datetime.now(timezone.utc) - started_at).total_seconds():.0f}s")

    asyncio.create_task(loop())


# Background maintenance: throttled per-component drawing/documentation
# URL backfill (see bom_categorizer.backfill_drawing_urls) - grows coverage
# automatically over time, no manual button needed. Runs more frequently
# than the other two loops since each cycle only processes a small,
# gentle batch (DRAWING_URL_BACKFILL_BATCH_SIZE) rather than a full sweep.
DRAWING_URL_BACKFILL_INTERVAL_SECONDS = 15 * 60


@app.on_event("startup")
async def start_drawing_url_backfill_loop():
    async def loop():
        await asyncio.sleep(60)
        while True:
            try:
                stats = await asyncio.to_thread(backfill_drawing_urls, db, sap_material_client)
                if stats["checked"] > 0:
                    logger.info(f"Drawing URL background backfill: checked {stats['checked']}, found {stats['found']}")
            except Exception as e:
                logger.error(f"Drawing URL background backfill failed: {e}")
            await asyncio.sleep(DRAWING_URL_BACKFILL_INTERVAL_SECONDS)

    asyncio.create_task(loop())

