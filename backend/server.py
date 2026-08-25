import asyncio
import json
import logging
import os
import re
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

import requests
from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ValidationError
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from starlette.middleware.cors import CORSMiddleware

from sap_soap_client import SAPSoapBOMClient, SAPSoapError
from sap_material_client import SAPMaterialClient, SAPMaterialError, SAPMaterialAuthError
from sap_material_create_client import SAPMaterialCreateClient, SAPMaterialCreateError
from sap_production_lot_client import SAPProductionLotClient, SAPProductionLotError, SAPProductionLotAuthError
from sap_wip_clearing_client import SAPWipClearingClient, SAPWipClearingError, company_and_set_of_books_for_site
from sap_production_proposal_client import SAPProductionProposalClient, SAPProductionProposalError
from sap_sto_client import SAPSTOClient
from sap_outbound_delivery_client import SAPOutboundDeliveryClient, SAPOutboundDeliveryError
from erp_portal_client import ERPPortalClient
from sap_production_model_client import SAPProductionModelClient, SAPProductionModelError, SAPProductionModelBomClient
from sap_goods_movement_client import SAPGoodsMovementClient, SAPGoodsMovementError
from sap_production_order_release_client import SAPProductionOrderReleaseClient, SAPProductionOrderReleaseError
from sap_material_physical_client import (
    SAPMaterialPhysicalClient, SAPMaterialPhysicalError, PHYSICAL_FIELD_TO_SAP_PROPERTY, bulk_push_physical_to_sap,
)
from sap_supplier_client import SAPSupplierClient, SAPSupplierError, SAPSupplierAuthError, SAPSupplierNotConfiguredError
from sap_price_spec_client import SAPPriceSpecClient, SAPPriceSpecError, bulk_push_erp_prices_to_sap
from sap_supplier_invoice_client import SAPSupplierInvoiceClient, SAPSupplierInvoiceError
from sap_cost_estimate_client import SAPCostEstimateClient, SAPCostEstimateError
from sap_gsa_client import SAPGSAClient, SAPGSAError
from price_explorer_client import PriceExplorerClient, PriceExplorerError
import quota_arrangement_service
from sap_valuation_client import SAPValuationClient, SAPValuationError
from sap_hsn_client import SAPHSNClient
from sap_inventory_client import SAPInventoryClient, SAPInventoryError
from sap_planning_client import SAPPlanningClient, SAPPlanningError, bulk_push_to_sap
from inventory_service import get_cached_inventory, refresh_inventory_cache, refresh_stock_quantities_for_warehouses, refresh_stock_quantities_for_site, deep_backfill_uuids, list_known_sites
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
import production_confirmation_service
import store_approval_service
import mrp_plan_store
import autosave_store
import job_store
import supplier_service
import stock_transfer_service
import company_cache_service

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


def _get_build_version() -> dict:
    """Short git commit hash + date, computed once at startup (Aug 27
    2026, user's explicit ask: "add build version on footer") - this
    repo has no separate semantic-version bump step, so the current git
    commit is the most accurate, zero-maintenance stand-in for "what
    build is actually running right now"."""
    try:
        commit = subprocess.check_output(
            ["git", "log", "-1", "--format=%h|%cd", "--date=format:%d %b %Y"],
            cwd=Path(__file__).parent.parent, timeout=5,
        ).decode().strip()
        short_hash, commit_date = commit.split("|", 1)
        return {"commit": short_hash, "commit_date": commit_date}
    except Exception:
        return {"commit": None, "commit_date": None}


BUILD_VERSION = _get_build_version()


@api_router.get("/version")
async def get_build_version():
    return BUILD_VERSION


@api_router.get("/docs/sap-integrations")
async def get_sap_integrations_doc():
    """Serves the SAP integration reference doc (read-only, plain text)."""
    doc_path = Path(__file__).parent.parent / "memory" / "SAP_INTEGRATIONS.md"
    if not doc_path.exists():
        raise HTTPException(status_code=404, detail="Documentation not found")
    return PlainTextResponse(doc_path.read_text(), media_type="text/plain; charset=utf-8")


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
store_approval_service.ensure_indexes(db)

_recovered_jobs = job_store.recover_orphaned_jobs(
    db, "Interrupted by a backend restart/deploy while this step was running - please retry this action."
)
if _recovered_jobs:
    logger.warning(f"Startup: recovered {_recovered_jobs} orphaned background job(s) stuck in an in-process-only status from before this restart.")

_recovered_issues = store_approval_service.recover_orphaned_issues(
    db, "Reverted to pending after a backend restart interrupted the stock issue before any SAP movement fired."
)
if _recovered_issues:
    logger.warning(f"Startup: recovered {_recovered_issues} orphaned store request(s) stuck in 'issuing' from before this restart.")

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

sap_material_create_client = SAPMaterialCreateClient(
    endpoint=os.environ['SAP_SOAP_MATERIAL_MANAGE_ENDPOINT'],
    username=os.environ['SAP_SOAP_USERNAME'],
    password=os.environ['SAP_SOAP_PASSWORD'],
)

sap_production_lot_client = SAPProductionLotClient(
    query_endpoint=os.environ['SAP_SOAP_PRODUCTION_LOT_QUERY_ENDPOINT'],
    manage_endpoint=os.environ['SAP_SOAP_PRODUCTION_LOT_MANAGE_ENDPOINT'],
    username=os.environ['SAP_SOAP_USERNAME'],
    password=os.environ['SAP_SOAP_PASSWORD'],
)

sap_wip_clearing_client = SAPWipClearingClient(
    endpoint=os.environ['SAP_SOAP_WIP_CLEARING_ENDPOINT'],
    username=os.environ['SAP_SOAP_USERNAME'],
    password=os.environ['SAP_SOAP_PASSWORD'],
)

sap_production_proposal_client = SAPProductionProposalClient(
    endpoint=os.environ['SAP_SOAP_PRODUCTION_PROPOSAL_ENDPOINT'],
    username=os.environ['SAP_SOAP_USERNAME'],
    password=os.environ['SAP_SOAP_PASSWORD'],
)

sap_sto_client = SAPSTOClient(
    endpoint=os.environ['SAP_SOAP_STO_ENDPOINT'],
    username=os.environ['SAP_SOAP_USERNAME'],
    password=os.environ['SAP_SOAP_PASSWORD'],
)

erp_portal_client = ERPPortalClient(
    primary_host=os.environ['ERP_MSSQL_PRIMARY_HOST'],
    fallback_host=os.environ['ERP_MSSQL_FALLBACK_HOST'],
    port=int(os.environ['ERP_MSSQL_PORT']),
    database=os.environ['ERP_MSSQL_DATABASE'],
    username=os.environ['ERP_MSSQL_USERNAME'],
    password=os.environ['ERP_MSSQL_PASSWORD'],
)

sap_outbound_delivery_client = SAPOutboundDeliveryClient(
    endpoint=os.environ['BYD_ODATA_BASE'],
    username=os.environ['SAP_USERNAME'],
    password=os.environ['SAP_PASSWORD'],
    vhost=os.environ['BYD_ODATA_VHOST'],
)

sap_production_order_release_client = SAPProductionOrderReleaseClient(
    base_url=os.environ['SAP_ODATA_PRODUCTION_ORDER_RELEASE_BASE_URL'],
    username=os.environ['SAP_ODATA_BUSINESS_USER'],
    password=os.environ['SAP_ODATA_BUSINESS_PASSWORD'],
    entity_set="ProductionOrderCollection",
)

sap_production_proposal_release_client = SAPProductionOrderReleaseClient(
    base_url=os.environ['SAP_ODATA_PRODUCTION_PROPOSAL_RELEASE_BASE_URL'],
    username=os.environ['SAP_ODATA_BUSINESS_USER'],
    password=os.environ['SAP_ODATA_BUSINESS_PASSWORD'],
    entity_set="ProductionPlanningOrderCollection",
)

sap_production_model_client = SAPProductionModelClient(
    base_url=os.environ['SAP_ODATA_PRODUCTION_MODEL_BASE_URL'],
    username=os.environ['SAP_ODATA_USERNAME'],
    password=os.environ['SAP_ODATA_PASSWORD'],
)

sap_production_model_bom_client = SAPProductionModelBomClient(
    base_url=os.environ['SAP_ODATA_PRODUCTION_MODEL_BOM_BASE_URL'],
    username=os.environ['SAP_ODATA_USERNAME'],
    password=os.environ['SAP_ODATA_PASSWORD'],
)

sap_goods_movement_client = SAPGoodsMovementClient(
    endpoint=os.environ['SAP_SOAP_GOODS_MOVEMENT_ENDPOINT'],
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

sap_hsn_client = SAPHSNClient(
    report_url=os.environ['SAP_HSN_ODATA_URL'],
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
    if not user or user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return {"pages": auth_service.PAGE_CATALOG}


@api_router.get("/admin/users")
async def admin_list_users(request: Request):
    user = await asyncio.to_thread(auth_service.get_current_user, request, db)
    if not user or user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    docs = await asyncio.to_thread(
        lambda: list(db[auth_service.USERS_COLLECTION].find({}).sort("last_login_at", -1))
    )
    return {"users": docs}


class UpdateUserAccessRequest(BaseModel):
    role: str
    allowed_pages: List[str] = []


class UpdateUserStoreSitesRequest(BaseModel):
    bound_sites: List[str] = []


@api_router.get("/admin/known-sites")
async def admin_known_sites(request: Request):
    """Feeds the Store Assignment tab's per-site checkboxes - same full
    SAP site list as /store-requests/known-sites, just gated by admin
    role instead of the store_approval page permission."""
    user = await asyncio.to_thread(auth_service.get_current_user, request, db)
    if not user or user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return {"sites": await asyncio.to_thread(list_known_sites, db)}


@api_router.put("/admin/users/{user_id}/store-sites")
async def admin_update_user_store_sites(user_id: str, body: UpdateUserStoreSitesRequest, request: Request):
    """Store Binding (Aug 2026, user's explicit ask): restricts a "store
    user"'s Store Approval queue/dropdown/actions to only the site(s)
    bound here - enforced in the store-requests endpoints below via
    _has_site_access/_filter_by_site_access. admin/super_admin always see
    every site regardless of this field (checked there, not here)."""
    requester = await asyncio.to_thread(auth_service.get_current_user, request, db)
    if not requester or requester.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    valid_sites = set(await asyncio.to_thread(list_known_sites, db))
    unknown = [s for s in body.bound_sites if s not in valid_sites]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown site code(s): {', '.join(unknown)}")
    result = await asyncio.to_thread(
        db[auth_service.USERS_COLLECTION].update_one,
        {"_id": user_id},
        {"$set": {"bound_sites": body.bound_sites}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True}


@api_router.put("/admin/users/{user_id}/access")
async def admin_update_user_access(user_id: str, body: UpdateUserAccessRequest, request: Request):
    requester = await asyncio.to_thread(auth_service.get_current_user, request, db)
    if not requester or requester.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    if body.role not in ("user", "admin", "super_admin"):
        raise HTTPException(status_code=400, detail="Invalid role")
    # Aug 2026, user's explicit ask: "admin" can grant/revoke pages and set
    # someone's role to "user", but can NEVER hand out "admin" or
    # "super_admin" - only an existing super_admin may create either.
    # Enforced here (not just hidden in the UI) since that's the actual
    # security boundary.
    if requester.get("role") == "admin" and body.role != "user":
        raise HTTPException(status_code=403, detail="Only a super admin can assign the admin/super admin role")
    target = await asyncio.to_thread(db[auth_service.USERS_COLLECTION].find_one, {"_id": user_id})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    # An admin also can't touch a super_admin's access at all (demote,
    # revoke pages, etc.) - only another super_admin can. Not explicitly
    # requested but a natural extension of "admin can't act at the
    # super_admin tier" - prevents an admin from ever locking out the
    # only super_admin(s).
    if requester.get("role") == "admin" and target.get("role") == "super_admin":
        raise HTTPException(status_code=403, detail="Only a super admin can modify another super admin's access")
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
    # Aug 2026 bug fix: this polls a live SAP SOAP call every 60s from every
    # open browser tab, with no retry - SAP's own connect stage is known to
    # stall intermittently every few minutes (see sap_soap_client._query
    # comment), so a single failed attempt was flashing "SAP Disconnected"
    # for whoever's poll happened to land in that window, even though SAP
    # recovered a second later. Retry once, briefly, before reporting down.
    last_error = None
    for attempt in range(2):
        try:
            await asyncio.to_thread(sap_soap_client.check_connection)
            return ConnectionStatus(connected=True, message="Connected to SAP Business ByDesign")
        except SAPSoapError as e:
            last_error = e
            if attempt == 0:
                await asyncio.sleep(1.5)
    return ConnectionStatus(connected=False, message=str(last_error))


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


SCRAP_FAMILY_RULES = [
    # (family label, expected SAP byproduct code, keywords to match in RM product_id/description)
    # Codes + categories confirmed against the actual scrap materials already in SAP - order matters,
    # more specific keywords are checked before generic ones (e.g. Stainless before generic Steel,
    # CR before generic HR/Steel).
    ("Brass Scrap", "BRASS-SCR", ["CDA", "BRASS"]),
    ("Copper Scrap", "COPPSC", ["CU", "COPPER"]),
    ("Aluminium Scrap", "ALU-SCRAP", ["ALU", "ALUMINIUM", "ALUMINUM"]),
    ("SS Scrap", "SSSCRAP", ["SS", "STAINLESS"]),
    ("CR Iron Scrap", "CR-SCRAP", ["CR"]),
    ("Iron Scrap", "IRON-SCR", ["HR", "STEEL", "COIL", "SHEET", "FLAT"]),
]
ZINC_KEYWORDS = ["ZN", "ZINC"]  # never a structural raw material - galvanization/coating only, always excluded


def _tokenize(text: str) -> list:
    return [t for t in re.split(r"[^A-Z0-9]+", text.upper()) if t]


def _classify_scrap_family(product_id: str, description: str):
    tokens = _tokenize(f"{product_id} {description or ''}")
    for label, code, keywords in SCRAP_FAMILY_RULES:
        if any(tok.startswith(k) for tok in tokens for k in keywords):
            return {"family": label, "expected_byproduct_code": code}
    return None


def _pick_rm_item(mass_items: list) -> dict:
    """Picks the real structural raw material among a BOM's mass-based
    components. Zinc is NEVER the RM (galvanization/coating only, per
    user's explicit rule) - always excluded. Among the rest, prefers an
    item that matches a known scrap family (Iron/Brass/Copper keywords);
    falls back to the largest-quantity remaining item if none match a
    known family (e.g. paint/coating consumables like "MAT" are usually
    much smaller by weight than the actual structural material anyway).

    Keyword matching is token-prefix based (not raw substring) - e.g.
    "SCREW"/"MICRO" no longer false-match "CR", "CUT"/"SECURE" no longer
    false-match "CU" - a token must START WITH the keyword to count."""
    candidates = [
        i for i in mass_items
        if not any(tok.startswith(k) for tok in _tokenize(f"{i['product_id']} {i.get('description') or ''}") for k in ZINC_KEYWORDS)
    ]
    if not candidates:
        return None
    classified = [(i, _classify_scrap_family(i["product_id"], i.get("description"))) for i in candidates]
    matched = [i for i, cls in classified if cls]
    pool = matched or candidates
    return max(pool, key=lambda i: i["quantity"])


class MaterialInputItem(BaseModel):
    product_id: str
    unit_code: Optional[str] = None
    qty_per_unit: Optional[float] = None


class ScrapCalcRequest(BaseModel):
    material_inputs: Optional[List[MaterialInputItem]] = None


async def _compute_scrap_calc(product_id: str, material_inputs: Optional[List[MaterialInputItem]]) -> dict:
    """Shared calc used by both the scrap-calc endpoint (frontend preview)
    and confirm_production's backend enforcement (Aug 27 2026) - kept as a
    single function so the two can never drift apart. See get_scrap_calc
    below for the full field-by-field explanation."""
    if material_inputs:
        inv_doc = db["inventory_cache"].find_one({"_id": "latest"})
        desc_by_product = {it["product_id"]: it.get("description") for it in (inv_doc or {}).get("items", [])}
        mass_items = [
            {"product_id": mi.product_id, "description": desc_by_product.get(mi.product_id), "quantity": mi.qty_per_unit}
            for mi in material_inputs
            if mi.qty_per_unit is not None and (mi.unit_code or "").upper() == "KGM"
        ]
    else:
        bom_doc = db["bom_node_cache"].find_one({"_id": product_id})
        mass_items = [
            item for group in (bom_doc or {}).get("groups", []) for item in group.get("items", [])
            if item.get("unit_of_measure") == "MASS" and item.get("quantity") is not None
        ]
    rm_item = _pick_rm_item(mass_items)
    if not rm_item:
        return {"available": False, "reason": "No raw-material (mass-based) BOM component found for this item"}
    scrap_family = _classify_scrap_family(rm_item["product_id"], rm_item.get("description"))

    component_doc = db["component_master"].find_one({"_id": product_id})
    net_weight_kg = (component_doc or {}).get("net_weight_kg")
    if net_weight_kg is None:
        # Local cache never got this value - before giving up, check if SAP
        # already has it set (common case: entered directly in SAP, or from
        # an earlier Push for a different field) and adopt it automatically
        # instead of forcing a manual Admin-page visit for every material.
        try:
            sap_result = await asyncio.to_thread(sap_material_physical_client.get_physical_attributes, product_id)
        except SAPMaterialPhysicalError:
            sap_result = None
        sap_net_weight = (sap_result or {}).get("attributes", {}).get("net_weight_kg")
        if sap_net_weight is not None:
            net_weight_kg = sap_net_weight
            db["component_master"].update_one({"_id": product_id}, {"$set": {"net_weight_kg": net_weight_kg}}, upsert=True)
    if net_weight_kg is None:
        return {
            "available": False, "reason": "Net Weight not set for this item yet - set it on the Admin page first",
            "rm_product_id": rm_item["product_id"], "rm_description": rm_item.get("description"),
            "gross_weight_kg": rm_item["quantity"], "scrap_family": scrap_family,
        }

    gross_weight_kg = rm_item["quantity"]
    # Aug 25 2026, user's explicit ask (real bug found live: PL-0037A on
    # Lot 70524 - Net Weight 1.344 kg > FLAT-PL37 Gross Weight 1.315 kg,
    # a physical impossibility since stamping/forming only ever REMOVES
    # material). Previously this silently clamped to "Scrap/unit: 0 kg"
    # and let the user post a meaningless 0kg by-product to SAP, masking
    # a real master-data error. Now treated exactly like a missing Net
    # Weight - "available": False - which blocks confirmation entirely
    # (both the frontend guard and the backend enforcement in
    # confirm_production reuse this same "available" flag).
    if net_weight_kg > gross_weight_kg:
        return {
            "available": False,
            "reason": (
                f"Data error: Net Weight ({net_weight_kg} kg) is greater than Gross Weight ({gross_weight_kg} kg) - "
                f"a finished part can never weigh more than the raw material it was made from. Fix the Net Weight "
                f"on the Admin page or the BOM consumption qty for {rm_item['product_id']} before confirming."
            ),
            "rm_product_id": rm_item["product_id"], "rm_description": rm_item.get("description"),
            "gross_weight_kg": gross_weight_kg, "net_weight_kg": net_weight_kg, "scrap_family": scrap_family,
        }
    scrap_per_unit_kg = max(0, round(gross_weight_kg - net_weight_kg, 6))
    return {
        "available": True,
        "rm_product_id": rm_item["product_id"],
        "rm_description": rm_item.get("description"),
        "gross_weight_kg": gross_weight_kg,
        "net_weight_kg": net_weight_kg,
        "scrap_per_unit_kg": scrap_per_unit_kg,
        "scrap_family": scrap_family,
    }


@api_router.post("/production-confirmation/scrap-calc/{product_id}")
async def get_scrap_calc(product_id: str, payload: ScrapCalcRequest = ScrapCalcRequest()):
    """Auto-calculates the expected scrap-per-unit for an output product:
    Gross Weight (the raw-material component's own consumption quantity)
    minus Net Weight (the finished item's own weight, already captured
    via the Admin page's Net Weight tool). The RM child is picked via
    _pick_rm_item (excludes Zn, prefers Iron/Brass/Copper family keyword
    matches). Returns {"available": False, "reason": ...} if either side
    is missing, and omits "rm_product_id" entirely when no mass-based RM
    component exists at all (e.g. an assembly-only item like a hardware
    kit in a bag - no by-product is ever expected for these, and
    confirm_production's backend check below treats the two cases
    differently for exactly this reason).

    Aug 27 2026 fix (same root cause/fix as
    check_component_availability_from_material_inputs): when the caller
    already has this specific lot's exact SAP MaterialInput list
    (`payload.material_inputs`, from SAPProductionLotClient), it's used
    directly instead of guessing via the cached "highest revision" BOM -
    real bug: lot 70422's real MaterialInput is FLAT-BK21, but this
    endpoint kept guessing SH4.5HR (from a DIFFERENT Production Model,
    BK-0021_2) because it only ever looked at `bom_node_cache` keyed by
    product_id alone. Falls back to the old cached-BOM guess only when
    no material_inputs are given (e.g. very old cached rows)."""
    return await _compute_scrap_calc(product_id, payload.material_inputs)


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


# ---------------------------------------------------------------------
# Production Task Confirmation - new top-level page (Aug 2026). Reads open
# Production Lots from SAP (QueryProductionLotISIIn) and posts confirmations
# (ManageProductionLotsIn) at the Reporting Point level ONLY - Confirmed
# Quantity, Confirmed Scrap, Deviation Reason, Finished indicator. Never
# sends MaterialInput/component quantities - SAP's backflush auto-consumes
# BOM components from the confirmed output, per the user's explicit
# requirement.
# ---------------------------------------------------------------------
def _attach_order_creators(rows: list, db) -> list:
    """Aug 2026, user's ask: "Show mine"/"Sort by latest" on the Production
    Confirmation table need to know who created/released each row's
    production order, and when - joins against
    production_order_creation_history (see
    production_confirmation_service.get_order_creators). Aug 27 2026: also
    joins the Production Model ID used to create that order, so a planner
    can identify which SAP Production Model/BOM this lot was built against."""
    order_ids = [r.get("production_order_id") for r in rows if r.get("production_order_id")]
    creators = production_confirmation_service.get_order_creators(db, order_ids)
    for r in rows:
        info = creators.get(r.get("production_order_id")) or {}
        r["created_by"] = info.get("name")
        at = info.get("at")
        r["order_created_at"] = at.isoformat() if at else None
        r["production_model_id"] = info.get("production_model_id")
    return rows


@api_router.get("/production-confirmation/open-lots")
async def get_open_production_lots(request: Request, status: str = Query("open", description="'open' (Released+Started), 'all', or comma-separated status codes"), site_id: Optional[str] = None, limit: int = 100):
    if status == "open":
        status_codes = None
    elif status == "all":
        status_codes = [str(c) for c in range(1, 7)]
    else:
        status_codes = [c.strip() for c in status.split(",") if c.strip()]
    try:
        rows = await asyncio.to_thread(sap_production_lot_client.find_open_lots, status_codes, site_id, limit)
    except SAPProductionLotAuthError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except SAPProductionLotError as e:
        raise HTTPException(status_code=502, detail=f"SAP error: {e}")
    # Aug 25 2026, user's explicit ask: Production Confirmation should only
    # show a "user"-role account what belongs to them (their bound sites,
    # same Site Binding mechanism as Store Approval) - admin/super_admin
    # keep seeing everything, unchanged.
    rows = _filter_by_site_access(rows, request.state.user)
    return {"rows": await asyncio.to_thread(_attach_order_creators, rows, db)}


@api_router.get("/production-confirmation/lot/{production_lot_id}")
async def get_production_lot_by_id(production_lot_id: str):
    try:
        rows = await asyncio.to_thread(sap_production_lot_client.find_lot_by_id, production_lot_id)
    except SAPProductionLotAuthError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except SAPProductionLotError as e:
        raise HTTPException(status_code=502, detail=f"SAP error: {e}")
    if not rows:
        raise HTTPException(status_code=404, detail=f"No production lot found for ID '{production_lot_id}'")
    return {"rows": await asyncio.to_thread(_attach_order_creators, rows, db)}


class ConfirmProductionRequest(BaseModel):
    production_lot_id: str
    production_lot_uuid: str
    confirmation_group_uuid: str
    reporting_point_uuid: str
    reporting_point_id: Optional[str] = None
    main_output_product: Optional[str] = None
    site_id: Optional[str] = None
    unit_code: Optional[str] = None
    production_task_id: Optional[str] = None
    production_task_uuid: Optional[str] = None
    confirmed_quantity: Optional[float] = None
    confirmed_scrap: Optional[float] = None
    deviation_reason_code: Optional[str] = None
    confirmation_finished: Optional[bool] = None
    byproduct_material_output_uuid: Optional[str] = None
    byproduct_confirmed_quantity: Optional[float] = None
    byproduct_unit_code: Optional[str] = None
    new_byproduct_product_id: Optional[str] = None
    new_byproduct_target_logistics_area_id: Optional[str] = None
    new_byproduct_confirmed_quantity: Optional[float] = None
    new_byproduct_unit_code: Optional[str] = None
    material_inputs: Optional[List[MaterialInputItem]] = None
    actor: str


def _clarify_confirm_error(raw_error: str, production_lot_id: str) -> str:
    """Aug 2026 - a real Cloudflare-level "invalid/incomplete response"
    error reached the user posting Lot 70222's confirmation while stock
    was short: `confirm_production` used to run entirely inline in the
    HTTP request (up to 4 sequential SOAP calls - by-product, main
    ReportingPoint, Finish Task, WIP Clearing - none of which retry, but
    a genuinely slow/hanging one is enough to blow past the platform's
    ingress timeout, and NOTHING was ever logged to history for this lot,
    confirming the call itself hung rather than SAP cleanly rejecting it).
    Now run as a background job (see confirm_production/poll below, same
    pattern as Store Approval's issue flow and Create-and-Release) so
    this can never happen again regardless of how slow SAP is - this
    clarifier just makes whatever SAP eventually does say easier to read."""
    text = raw_error or ""
    if re.search(r"negative stock not permitted|insufficient|not enough|shortage", text, re.IGNORECASE):
        return f"SAP rejected this confirmation for Lot {production_lot_id} - insufficient component stock for backflush. Check the Component Stock Check panel or raise a Store Approval request, then retry."
    if re.search(r"authentication failed|authorization role missing", text, re.IGNORECASE):
        return f"SAP login/authorization failed while posting Lot {production_lot_id}'s confirmation. Contact IT."
    if re.search(r"unreachable|timeout|connection", text, re.IGNORECASE):
        return f"Could not reach SAP to post Lot {production_lot_id}'s confirmation. Please retry in a moment."
    return f"SAP rejected Lot {production_lot_id}'s confirmation: {text}"


@api_router.post("/production-confirmation/confirm")
async def confirm_production(payload: ConfirmProductionRequest):
    if not payload.actor.strip():
        raise HTTPException(status_code=400, detail="actor (your name) is required")
    # Aug 27 2026 fix (user's explicit ask, part 2): the frontend already
    # blocks the Confirm button when a by-product is expected but not
    # posted - this mirrors that SAME check server-side, using the exact
    # same _compute_scrap_calc used to render the frontend's panel, so a
    # confirmation can never slip through no matter what state the UI is
    # in (stale tab, direct API call, etc). An item with NO mass-based RM
    # component at all (rm_product_id absent - e.g. a hardware
    # sub-assembly kit in a bag) never expected a by-product in the
    # first place and is correctly skipped below, same as the frontend.
    if payload.main_output_product:
        scrap_calc = await _compute_scrap_calc(payload.main_output_product, payload.material_inputs)
        if scrap_calc.get("rm_product_id") and not scrap_calc.get("available"):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot confirm Lot {payload.production_lot_id}: by-product is expected but its weight can't be calculated yet ({scrap_calc.get('reason')}). Fix this first so the by-product is never skipped.",
            )
        if scrap_calc.get("available"):
            has_existing_target = bool(payload.byproduct_material_output_uuid) and payload.byproduct_confirmed_quantity is not None
            has_new_target = (
                bool(payload.new_byproduct_product_id)
                and bool(payload.new_byproduct_target_logistics_area_id)
                and payload.new_byproduct_confirmed_quantity is not None
            )
            if not has_existing_target and not has_new_target:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot confirm Lot {payload.production_lot_id}: a by-product is expected but there's no Output Products line to post it to and no target storage area to create one - contact support before confirming this lot.",
                )
    job_id = str(uuid.uuid4())
    job_store.create_job(db, job_id, {"status": "running"})
    asyncio.create_task(_run_confirm_production_job(job_id, payload))
    return {"job_id": job_id}


@api_router.get("/production-confirmation/confirm/status/{job_id}")
async def get_confirm_production_status(job_id: str):
    job = job_store.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return job


async def _run_confirm_production_job(job_id: str, payload: ConfirmProductionRequest):
    try:
        await _confirm_production_inner(job_id, payload)
    except Exception as e:
        logger.error(f"Confirm production job {job_id} (Lot {payload.production_lot_id}) crashed unexpectedly: {e}")
        job_store.update_job(db, job_id, {"status": "failed", "error": f"Unexpected error posting Lot {payload.production_lot_id}'s confirmation - please retry."})


async def _confirm_production_inner(job_id: str, payload: ConfirmProductionRequest):
    # The by-product's Output Products line (e.g. IRON-SCR) MUST be
    # confirmed BEFORE the main ReportingPoint call when
    # confirmation_finished=True - confirm_reporting_point internally also
    # calls finish_task() in that case, which fully closes the lot/task, and
    # SAP then rejects ANY further MaterialOutput update with "Processing
    # not possible due to status Finished" (reproduced live on Lot 69962).
    byproduct_confirmation = None
    if payload.byproduct_material_output_uuid and payload.byproduct_confirmed_quantity is not None:
        try:
            byproduct_confirmation = await asyncio.to_thread(
                sap_production_lot_client.confirm_material_output,
                production_lot_id=payload.production_lot_id, production_lot_uuid=payload.production_lot_uuid,
                confirmation_group_uuid=payload.confirmation_group_uuid,
                material_output_uuid=payload.byproduct_material_output_uuid,
                confirmed_quantity=payload.byproduct_confirmed_quantity, unit_code=payload.byproduct_unit_code,
            )
        except SAPProductionLotError as e:
            byproduct_confirmation = {"success": False, "logs": [{"note": str(e)}]}
    elif (payload.new_byproduct_product_id and payload.new_byproduct_target_logistics_area_id
          and payload.new_byproduct_confirmed_quantity is not None):
        # No output line was ever planned for this by-product on this lot
        # (Production Model gap) - create the line from scratch instead of
        # updating an existing one, per the same before-finish ordering.
        try:
            byproduct_confirmation = await asyncio.to_thread(
                sap_production_lot_client.create_material_output,
                production_lot_id=payload.production_lot_id, production_lot_uuid=payload.production_lot_uuid,
                confirmation_group_uuid=payload.confirmation_group_uuid,
                product_id=payload.new_byproduct_product_id,
                target_logistics_area_id=payload.new_byproduct_target_logistics_area_id,
                confirmed_quantity=payload.new_byproduct_confirmed_quantity, unit_code=payload.new_byproduct_unit_code,
            )
        except SAPProductionLotError as e:
            byproduct_confirmation = {"success": False, "logs": [{"note": str(e)}]}

    # Aug 27 2026 fix (user's explicit ask - a real production confirmation
    # went through with NO by-product ever posted): previously, if the
    # by-product SAP call above failed (or an exception was caught), the
    # code still went ahead and posted the main ReportingPoint confirmation
    # below anyway - and per the comment above, if confirmation_finished
    # was also true, the lot got permanently closed with SAP then
    # rejecting ANY further attempt to add the missing by-product. Now: a
    # by-product that was actually ATTEMPTED (has a real target - existing
    # line or a new one) but failed hard-stops here, BEFORE the main
    # confirmation is ever sent, so the user can fix the issue and retry
    # with nothing yet posted to SAP.
    if byproduct_confirmation is not None and not byproduct_confirmation.get("success"):
        note = (byproduct_confirmation.get("logs") or [{}])[-1].get("note") or "Unknown error"
        job_store.update_job(db, job_id, {
            "status": "failed",
            "error": f"By-product posting failed, so the main confirmation was NOT submitted (nothing was posted to SAP) - fix the issue below and retry: {note}",
            "byproduct_confirmation": byproduct_confirmation,
        })
        return

    try:
        result = await asyncio.to_thread(
            sap_production_lot_client.confirm_reporting_point,
            production_lot_id=payload.production_lot_id, production_lot_uuid=payload.production_lot_uuid,
            confirmation_group_uuid=payload.confirmation_group_uuid, reporting_point_uuid=payload.reporting_point_uuid,
            unit_code=payload.unit_code, production_task_id=payload.production_task_id,
            production_task_uuid=payload.production_task_uuid, confirmed_quantity=payload.confirmed_quantity,
            # confirmed_scrap here is a manually-entered REJECTED QUANTITY
            # (defective units), unrelated to the physical by-product
            # material weight - it IS written to SAP's own ConfirmedScrap
            # field on this ReportingPoint. The physical by-product output
            # line (e.g. IRON-SCR) was already confirmed ABOVE, using our
            # own gross-minus-net weight calculation - a completely
            # independent value from whatever the user enters here.
            confirmed_scrap=payload.confirmed_scrap,
            deviation_reason_code=payload.deviation_reason_code,
            confirmation_finished=payload.confirmation_finished,
        )
    except SAPProductionLotAuthError as e:
        job_store.update_job(db, job_id, {"status": "failed", "error": _clarify_confirm_error(str(e), payload.production_lot_id)})
        return
    except SAPProductionLotError as e:
        job_store.update_job(db, job_id, {"status": "failed", "error": _clarify_confirm_error(str(e), payload.production_lot_id)})
        return

    if byproduct_confirmation is not None:
        result["byproduct_confirmation"] = byproduct_confirmation

    # Per user's explicit choice, a WIP Clearing Run auto-fires right after a
    # task is successfully marked Finished - so period-end WIP is cleared
    # without a separate manual step.
    if payload.confirmation_finished and result.get("success") and payload.site_id:
        try:
            wip_result = await asyncio.to_thread(
                sap_wip_clearing_client.run_wip_clearing, payload.production_lot_id, payload.site_id,
            )
            result["wip_clearing"] = wip_result
        except SAPWipClearingError as e:
            result["wip_clearing"] = {"success": False, "log": str(e)}

    # Aug 27 2026, user's explicit ask: right after WIP is cleared, a
    # genuine Finished Goods item (per the same FG/Sub-Assembly rule the
    # Inventory page's categorizer uses) needs a follow-up SFG -> FG
    # Goods Movement - a Sub-Assembly (e.g. a bare part later packed into
    # its own FG) stays in SFG on purpose, feeding the next assembly
    # step, so it's deliberately left untouched. If this item was never
    # categorized yet, run the SAME deterministic rule live right now
    # (get_or_classify_category) instead of silently skipping - only
    # falls back to skipping if bom_node_cache has no BOM at all for it
    # (genuinely can't tell). Job status updates here let the frontend
    # show the user exactly which step is running instead of one long
    # opaque "Saving..." spinner.
    if payload.confirmation_finished and result.get("success") and payload.site_id and payload.main_output_product and payload.confirmed_quantity:
        job_store.update_job(db, job_id, {"status": "checking_category"})
        category, _ = await asyncio.to_thread(
            production_confirmation_service.get_or_classify_category, db, payload.main_output_product,
        )
        if category == "Finished Goods":
            job_store.update_job(db, job_id, {"status": "moving_to_fg"})
            owner_party_id, _ = company_and_set_of_books_for_site(payload.site_id)
            fg_movement = await asyncio.to_thread(
                store_approval_service._trigger_goods_movement,
                sap_goods_movement_client, owner_party_id, payload.main_output_product,
                f"{payload.site_id}-SFG", f"{payload.site_id}-FG",
                payload.confirmed_quantity, payload.unit_code or "EA", payload.site_id,
            )
            result["fg_movement"] = fg_movement
        job_store.update_job(db, job_id, {"status": "running"})

    # testing_agent iteration_105: a normal (non-exception) SAP business
    # rejection - e.g. backflush failing for insufficient stock, exactly
    # the Lot 70222 scenario this whole fix was born from - came back as
    # status="done"/success=False with only SAP's raw Log note ("Modif-
    # ication failed"), never through _clarify_confirm_error at all. Apply
    # the same clarifier here so the frontend banner reads the same way
    # regardless of whether SAP failed via an exception or a normal reject.
    if not result.get("success"):
        raw_notes = "; ".join(l.get("note", "") for l in result.get("logs", []) if l.get("note")) or "SAP did not return a clear reason"
        result["clarified_error"] = _clarify_confirm_error(raw_notes, payload.production_lot_id)

    await asyncio.to_thread(production_confirmation_service.log_confirmation, db, payload.actor, payload.dict(), result)
    job_store.update_job(db, job_id, {"status": "done", "result": result})


class RetryWipClearingRequest(BaseModel):
    production_lot_id: str
    site_id: str
    actor: str


@api_router.post("/production-confirmation/retry-wip-clearing")
async def retry_wip_clearing(payload: RetryWipClearingRequest):
    """Re-fires just the WIP Clearing Run for a lot whose confirmation
    already posted successfully but the WIP step itself failed - "Retry"
    button next to the failed "WIP Cleared" chip (Aug 2026, user's
    explicit ask). Does not touch the main confirmation/by-product data
    at all, only overwrites the wip_clearing outcome on that lot's most
    recent history entry."""
    if not payload.actor.strip():
        raise HTTPException(status_code=400, detail="actor (your name) is required")
    try:
        wip_result = await asyncio.to_thread(
            sap_wip_clearing_client.run_wip_clearing, payload.production_lot_id, payload.site_id,
        )
    except SAPWipClearingError as e:
        wip_result = {"success": False, "log": str(e)}
    except Exception as e:
        wip_result = {"success": False, "log": f"Unexpected error: {e}"}
    updated = await asyncio.to_thread(
        production_confirmation_service.retry_wip_clearing_for_lot, db, payload.production_lot_id, wip_result,
    )
    if not updated:
        raise HTTPException(status_code=404, detail=f"No confirmation history found yet for Lot {payload.production_lot_id}")
    return {"wip_clearing": wip_result}


class RetryFgMovementRequest(BaseModel):
    production_lot_id: str
    site_id: str
    main_output_product: str
    confirmed_quantity: float
    unit_code: Optional[str] = None
    actor: str


@api_router.post("/production-confirmation/retry-fg-movement")
async def retry_fg_movement(payload: RetryFgMovementRequest):
    """Re-fires just the SFG -> FG Goods Movement for a lot whose
    confirmation/WIP Clearing already succeeded but the movement itself
    failed - "Retry" button next to a failed "FG Moved" chip (Aug 27
    2026, user's explicit ask). Does not touch the main confirmation/
    WIP/by-product data, only overwrites the fg_movement outcome on
    that lot's most recent history entry."""
    if not payload.actor.strip():
        raise HTTPException(status_code=400, detail="actor (your name) is required")
    owner_party_id, _ = company_and_set_of_books_for_site(payload.site_id)
    fg_movement = await asyncio.to_thread(
        store_approval_service._trigger_goods_movement,
        sap_goods_movement_client, owner_party_id, payload.main_output_product,
        f"{payload.site_id}-SFG", f"{payload.site_id}-FG",
        payload.confirmed_quantity, payload.unit_code or "EA", payload.site_id,
    )
    updated = await asyncio.to_thread(
        production_confirmation_service.retry_fg_movement_for_lot, db, payload.production_lot_id, fg_movement,
    )
    if not updated:
        raise HTTPException(status_code=404, detail=f"No confirmation history found yet for Lot {payload.production_lot_id}")
    return {"fg_movement": fg_movement}


@api_router.get("/production-confirmation/history")
async def get_production_confirmation_history(production_lot_id: Optional[str] = None):
    entries = await asyncio.to_thread(production_confirmation_service.get_confirmation_history, db, production_lot_id)
    return {"entries": entries}


def _site_scope_for(user: dict):
    """None = unrestricted (admin/super_admin); otherwise the exact set of
    sites (possibly empty) a "user"-role account is bound to."""
    if user.get("role") in ("super_admin", "admin"):
        return None
    return set(user.get("bound_sites", []))


@api_router.get("/production-confirmation/confirmed-today")
async def get_confirmed_today(request: Request):
    """"Confirmed Today" dashboard tile (user's explicit ask, Aug 25
    2026) - distinct lots confirmed since midnight IST (the shop floor's
    actual "today", not UTC's)."""
    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    start_ist_naive = datetime(now_ist.year, now_ist.month, now_ist.day)
    start_utc = (start_ist_naive - timedelta(hours=5, minutes=30)).replace(tzinfo=timezone.utc)
    end_utc = start_utc + timedelta(days=1)
    site_ids = _site_scope_for(request.state.user)
    count = await asyncio.to_thread(production_confirmation_service.count_confirmed_today, db, start_utc, end_utc, site_ids)
    return {"confirmed_today": count}


@api_router.get("/production-confirmation/scrap-trend")
async def get_scrap_trend(request: Request):
    """"Scrap Trend" dashboard tile (user's explicit ask, Aug 25 2026) -
    scrap qty + confirmation count grouped by Reason over the last 7
    days, e.g. Quality Issue vs Material Damage."""
    start_utc = datetime.now(timezone.utc) - timedelta(days=7)
    site_ids = _site_scope_for(request.state.user)
    rows, reasons = await asyncio.gather(
        asyncio.to_thread(production_confirmation_service.get_scrap_reason_breakdown, db, start_utc, site_ids),
        asyncio.to_thread(production_confirmation_service.get_deviation_reasons, db),
    )
    label_by_code = {r["code"]: r["label"] for r in reasons}
    breakdown = [
        {
            "code": row["_id"],
            "label": label_by_code.get(row["_id"], row["_id"] or "No reason given"),
            "total_scrap": row["total_scrap"],
            "count": row["count"],
        }
        for row in rows
    ]
    return {"breakdown": breakdown, "days": 7}


@api_router.get("/production-confirmation/urgent-actions")
async def get_urgent_actions(request: Request):
    """Urgent Action Dashboard (user's explicit ask, Aug 25 2026) - one
    summary of 3 things needing attention right now: Overdue POs (from
    the Open-PO Demand feed's last autosaved pull - due_date/target_ship_
    date already past, qty_open still > 0), component Shortages (the
    distinct components currently blocking a pending Store Approval
    request), and Pending Store Approvals themselves. Site-scoped for a
    "user"-role account (bound_sites), unrestricted for admin/super_admin
    - same as the open-lots table above. Overdue POs are sales-order
    lines with no reliable per-row site/plant field in the feed itself,
    so that bucket is shown company-wide regardless of role."""
    site_ids = _site_scope_for(request.state.user)

    pending = await asyncio.to_thread(store_approval_service.list_requests, db)
    pending = [r for r in pending if r.get("status") == "pending"]
    if site_ids is not None:
        pending = [r for r in pending if r.get("site_id") in site_ids]

    shortages_by_product = {}
    for r in pending:
        for c in r.get("components", []):
            entry = shortages_by_product.setdefault(c["product_id"], {"product_id": c["product_id"], "description": c.get("description"), "sites": set()})
            entry["sites"].add(r.get("site_id"))
    shortages = sorted(
        [{**v, "sites": sorted(s for s in v["sites"] if s)} for v in shortages_by_product.values()],
        key=lambda x: x["product_id"],
    )

    po_doc = await asyncio.to_thread(autosave_store.get_autosave, db, "open_po_demand")
    today_str = datetime.now(timezone.utc).date().isoformat()
    overdue_pos = []
    for row in ((po_doc or {}).get("data") or {}).get("rows", []):
        due = row.get("due_date") or row.get("target_ship_date")
        qty_open = row.get("qty_open") or 0
        if due and qty_open > 0 and due < today_str:
            overdue_pos.append(row)
    overdue_pos.sort(key=lambda r: r.get("due_date") or r.get("target_ship_date") or "")

    return {
        "overdue_pos": overdue_pos[:20],
        "overdue_pos_count": len(overdue_pos),
        "shortages": shortages[:20],
        "shortages_count": len(shortages),
        "pending_store_approvals": pending[:20],
        "pending_store_approvals_count": len(pending),
    }


class LatestConfirmationBatchRequest(BaseModel):
    production_lot_ids: List[str]


@api_router.post("/production-confirmation/history/latest-batch")
async def get_latest_confirmation_batch(payload: LatestConfirmationBatchRequest):
    result = await asyncio.to_thread(production_confirmation_service.get_latest_confirmation_by_lot, db, payload.production_lot_ids)
    return result


@api_router.get("/production-confirmation/deviation-reasons")
async def list_deviation_reasons():
    return {"reasons": await asyncio.to_thread(production_confirmation_service.get_deviation_reasons, db)}


class DeviationReasonRequest(BaseModel):
    code: str
    label: str


@api_router.post("/production-confirmation/deviation-reasons")
async def create_deviation_reason(payload: DeviationReasonRequest):
    reasons = await asyncio.to_thread(production_confirmation_service.add_deviation_reason, db, payload.code, payload.label)
    return {"reasons": reasons}


@api_router.delete("/production-confirmation/deviation-reasons/{code}")
async def remove_deviation_reason(code: str):
    reasons = await asyncio.to_thread(production_confirmation_service.delete_deviation_reason, db, code)
    return {"reasons": reasons}


# ---------------------------------------------------------------------
# Create Production Order (Aug 2026). SAP has no direct "create order" API,
# so this creates a Production Proposal (ManageProductionProposalIn) which
# SAP's own scheduled Supply Planning Run converts into a Production
# Request then Order (A010 auto-creation strategy, confirmed by user). SAP
# also has no standard Release API, so a custom OData action
# ("productionorderemergent") was built by the user's SAP admin exposing
# the BO's PSM-released "Release" action - called with a dedicated SAP
# Business User (custom OData actions reject technical/communication
# users).
# ---------------------------------------------------------------------
class CreateProductionProposalRequest(BaseModel):
    material_id: str
    site_id: str
    quantity: float
    unit_code: str
    availability_datetime: Optional[str] = None
    actor: str
    logistic_relationship_uuid: Optional[str] = None
    production_model_uuid: Optional[str] = None
    # Aug 27 2026, user's explicit ask: human-readable Production Model ID
    # (e.g. "BK-0021_1"), saved alongside the UUID purely for display on
    # the Production Confirmation / Proposal-Order History tables - never
    # sent to SAP itself (SAP only needs the UUID).
    production_model_id: Optional[str] = None


@api_router.get("/production-confirmation/source-of-supply-options/{material_id}")
async def get_source_of_supply_options(material_id: str, site_id: str = None):
    """Lists the available Production Models (Source of Supply) for a
    material with multiple valid BOMs at the given site, so the user can
    pick one before creating a Production Order instead of SAP
    auto-defaulting. Scoped by site_id because a material's multiple
    models are often valid at different sites (not a real conflict) -
    only models sharing the SAME site's Supply Planning Area are surfaced
    as genuine choices. Returns an empty list (not an error) if the
    material's product_uuid hasn't been captured yet or there's only one
    valid model at this site - the frontend hides the picker in that case."""
    doc = db["component_master"].find_one({"_id": material_id}) or db["bom_node_cache"].find_one({"_id": material_id})
    material_uuid = (doc or {}).get("product_uuid")
    base_uom = (doc or {}).get("base_uom")
    if base_uom is None:
        try:
            sap_result = await asyncio.to_thread(sap_material_physical_client.get_physical_attributes, material_id)
        except SAPMaterialPhysicalError:
            sap_result = None
        base_uom = (sap_result or {}).get("base_uom")
        if base_uom:
            db["component_master"].update_one({"_id": material_id}, {"$set": {"base_uom": base_uom}}, upsert=True)
    if not material_uuid:
        return {"material_uuid": None, "options": [], "base_uom": base_uom}
    try:
        options = await asyncio.to_thread(sap_production_model_client.get_source_of_supply_options, material_uuid, site_id)
    except SAPProductionModelError as e:
        raise HTTPException(status_code=502, detail=f"SAP error: {e}")
    return {"material_uuid": material_uuid, "options": options, "base_uom": base_uom}


@api_router.post("/production-confirmation/create-proposal")
async def create_production_proposal(payload: CreateProductionProposalRequest):
    if not payload.actor.strip():
        raise HTTPException(status_code=400, detail="actor (your name) is required")
    avail_dt = None
    if payload.availability_datetime:
        try:
            avail_dt = datetime.fromisoformat(payload.availability_datetime.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="availability_datetime must be an ISO date/time")
    try:
        result = await asyncio.to_thread(
            sap_production_proposal_client.create_proposal,
            payload.material_id, payload.site_id, payload.quantity, payload.unit_code, avail_dt,
        )
    except SAPProductionProposalError as e:
        raise HTTPException(status_code=502, detail=f"SAP error: {e}")
    await asyncio.to_thread(
        production_confirmation_service.log_proposal_creation, db, payload.actor, payload.dict(), result,
    )
    return result


CREATE_RELEASE_MAX_WAIT_SECONDS = 20 * 60  # keep polling for the resulting Order for up to 20 min
CREATE_RELEASE_POLL_INTERVAL_SECONDS = 4  # tightened from 10s so a newly-created Order/Lot is detected sooner
CREATE_RELEASE_RETRIGGER_EVERY_SECONDS = 90  # re-fire the Release action periodically in case the first call needs a nudge
SAP_SETTLE_DELAY_SECONDS = 3  # tightened from 8s - still gives SAP a beat to commit before the next read, without wasting time

GOODS_ISSUE_MAX_WAIT_SECONDS = 20 * 60  # SAP's own scheduling converts Customer Requirement -> Outbound Delivery Request; timing not in our control
GOODS_ISSUE_POLL_INTERVAL_SECONDS = 20  # a much slower batch process than Order/Lot creation - no need to hammer it every few seconds

# Phrases SAP uses when a Proposal is rejected for a genuine data/input
# problem (wrong Unit of Measure, bad quantity literal, unknown material,
# etc) rather than a transient network/session hiccup - retrying the exact
# same request 3x for one of these just wastes ~20s, it will never succeed
# without the user changing something on the form first.
_SAP_PERMANENT_ERROR_MARKERS = (
    "invalid", "not valid", "malformed", "does not exist", "not found",
    "not defined", "not allowed", "unauthorized", "authorization role missing",
)


def _clarify_sap_error(raw_error: str, unit_code: str, material_id: str) -> str:
    """Wraps a raw SAP fault string with a clearer, actionable hint when it
    looks like a Unit of Measure / Quantity data problem - the most likely
    cause given the UoM picker is currently a free choice, not validated
    against the material's actual base UoM in SAP."""
    lower = raw_error.lower()
    if "unit" in lower or "uom" in lower or "quantitytype" in lower or "quantity" in lower:
        return (
            f"SAP rejected this request - '{unit_code}' may not be a valid Unit of Measure for "
            f"material '{material_id}'. Try EA (or check with SAP what units this material supports). "
            f"SAP's own message: {raw_error}"
        )
    return raw_error


def _is_permanent_sap_error(raw_error: str) -> bool:
    lower = raw_error.lower()
    return any(marker in lower for marker in _SAP_PERMANENT_ERROR_MARKERS)


async def _create_proposal_for_payload(payload: "CreateProductionProposalRequest", avail_dt, job_id: str) -> str:
    """Creates the SAP Production Proposal for this payload - shared by both
    the normal auto-release path and the insufficient-stock path (which,
    per the Store Approval workflow, still creates the Proposal right away
    and only defers the Release/Order steps until the store resolves the
    stock shortfall)."""
    if payload.logistic_relationship_uuid:
        # User explicitly picked a non-default Production Model - this can
        # ONLY be set at creation time (SAP rejects it on PATCH of an
        # existing Proposal), so this bypasses the normal SOAP path
        # entirely and goes through the custom OData Create action.
        doc = db["component_master"].find_one({"_id": payload.material_id}) or db["bom_node_cache"].find_one({"_id": payload.material_id})
        material_uuid = (doc or {}).get("product_uuid")
        if not material_uuid:
            raise SAPProductionOrderReleaseError(f"Could not resolve product_uuid for material '{payload.material_id}'")
        spa_uuid = await asyncio.to_thread(sap_production_model_client.get_supply_planning_area_uuid, payload.site_id)
        for attempt in range(3):
            try:
                return await asyncio.to_thread(
                    sap_production_proposal_release_client.create_with_source_of_supply,
                    material_uuid, spa_uuid, payload.quantity, payload.unit_code, avail_dt, payload.logistic_relationship_uuid,
                )
            except SAPProductionOrderReleaseError as e:
                if attempt == 2 or _is_permanent_sap_error(str(e)):
                    raise SAPProductionOrderReleaseError(_clarify_sap_error(str(e), payload.unit_code, payload.material_id))
                logger.warning(f"create-and-release job {job_id}: create-with-source-of-supply attempt {attempt + 1}/3 hit a transient SAP error, retrying: {e}")
                await asyncio.sleep(10)
    else:
        for attempt in range(3):
            try:
                proposal_result = await asyncio.to_thread(
                    sap_production_proposal_client.create_proposal,
                    payload.material_id, payload.site_id, payload.quantity, payload.unit_code, avail_dt,
                )
                return proposal_result["production_proposal_id"]
            except SAPProductionProposalError as e:
                if attempt == 2 or _is_permanent_sap_error(str(e)):
                    raise SAPProductionProposalError(_clarify_sap_error(str(e), payload.unit_code, payload.material_id))
                logger.warning(f"create-and-release job {job_id}: proposal creation attempt {attempt + 1}/3 hit a transient SAP error, retrying: {e}")
                await asyncio.sleep(10)


def _claim_order_id(db, order_id: str, job_id: str) -> bool:
    """Atomically claims `order_id` for `job_id` via a unique-`_id` insert
    into `order_claims` - the ONLY reliable guard against two concurrent
    create-and-release jobs (e.g. two users triggering the same material+
    site at once, or a retriggered poll racing itself) both matching the
    same newly-appeared SAP order. Returns False (never raises) if another
    job already claimed it first."""
    try:
        db.order_claims.insert_one({"_id": order_id, "job_id": job_id, "claimed_at": datetime.now(timezone.utc)})
        return True
    except DuplicateKeyError:
        return False


async def _run_create_and_release_job(job_id: str, payload: "CreateProductionProposalRequest", avail_dt):
    """One-click orchestration, run fully in the background so it is never
    bound by the platform's ~60s ingress timeout: Create Proposal -> (settle
    delay) -> trigger SAP's 'Request Production' action (instant
    Proposal->Order conversion, bypassing the slow scheduled planning run)
    -> (settle delay) -> detect the resulting Order by diffing open
    Production Lots for this site before/after -> Release the Order. Fully
    automated - re-fires the Release trigger periodically and keeps polling
    for up to CREATE_RELEASE_MAX_WAIT_SECONDS with zero need for a human to
    look up or type an Order ID. The settle delays exist because SAP's own
    indexing needs a beat after each write before the very next call reads
    reliably - calling Release immediately after Create (0s apart) risked
    the action silently missing the just-created Proposal.

    If a component is short at pre-flight, this no longer just fails the
    job - it still creates the Proposal, then opens a Store Approval
    request and PAUSES (job status "waiting_store_approval") until a
    warehouse user (or the requester, for a partial issue) resolves it -
    see store_approval_service.py + _continue_order_creation below."""
    proposal_id = None  # bound up-front so the generic except below can always report it, even if the failure happens before it's ever assigned
    try:
        # Pre-flight stock check - LIVE from SAP (not the cache) since this
        # gates a real SAP write; ~47s for SAP's inventory report (a heavy
        # OLAP-style query, hence why it's live ONLY here and not on the
        # open-lots list's Stock badges, which stay cache-only/instant on
        # purpose to avoid hammering SAP on every page view). Worth the
        # wait since order creation is infrequent, and running inside this
        # already-backgrounded job means it's never subject to the
        # platform's ~60s ingress timeout the way a blocking pre-flight in
        # the POST endpoint itself would have been.
        job_store.update_job(db, job_id, {"status": "checking_stock"})
        # User-requested stop, checked before anything happens in SAP at all
        # (before this point nothing was ever written) - a Stop clicked
        # during "checking_stock" now takes effect immediately instead of
        # sitting unread until a much-later checkpoint further below.
        if job_store.get_job(db, job_id).get("cancel_requested"):
            job_store.update_job(db, job_id, {"status": "cancelled", "result": {
                "production_proposal_id": None, "production_order_id": None, "released": False,
                "note": "Stopped before anything was created in SAP - nothing to clean up.",
            }})
            return
        override_bom_id = None
        if payload.production_model_uuid:
            # User explicitly picked a Production Model via the Source of
            # Supply picker - look up its REAL, currently-linked BOM (see
            # SAPProductionModelBomClient) so this pre-flight check doesn't
            # rely on bom_cache_service's "highest revision" default guess,
            # which is exactly what caused the false-shortage bug this
            # fixes (e.g. MAZ42117272-TA). Never blocks the job on its own
            # - a lookup failure just leaves override_bom_id None and
            # check_component_availability falls back to its normal
            # cached-default behavior.
            try:
                override_bom_id = await asyncio.to_thread(
                    sap_production_model_bom_client.get_bill_of_material_id_for_model, payload.production_model_uuid,
                )
            except Exception as e:
                # Broad catch is deliberate here (not just SAPProductionModelError)
                # - this OData call can also raise a raw requests transport
                # error (ConnectTimeout/ReadTimeout, common on this tenant)
                # which must degrade to the old cached-default behavior, not
                # fail the whole order job (testing_agent iteration_96 caught
                # this as a HIGH regression - a network timeout on this
                # lookup used to propagate to the job's outer except Exception).
                logger.warning(f"create-and-release job {job_id}: real-BOM-for-model lookup failed, using cached default instead: {e}")
        availability = await asyncio.to_thread(
            production_confirmation_service.check_component_availability, db, payload.material_id, payload.quantity, payload.site_id,
            sap_inventory_client, override_bom_id, sap_soap_client, True, sap_production_model_client, sap_production_model_bom_client,
        )
        short = [c for c in availability["components"] if not c["sufficient"]] if availability["checked"] else []

        # Aug 2026 bug fix (user's explicit ask): a short Sub-Assembly (SFG)
        # component is NOT something the physical Store can issue - it only
        # exists once its own production order is confirmed. Block the
        # whole order creation here, BEFORE any SAP write happens (no
        # Proposal created at all), with a clear on-screen error instead of
        # silently opening a Store Approval request the store can never
        # actually fulfill.
        short_sfg = [c for c in short if c.get("is_sub_assembly")]
        short_rm = [c for c in short if not c.get("is_sub_assembly")]
        if short_sfg:
            def _fmt(c):
                have = "unknown" if c["available_qty"] is None else f"{c['available_qty']:g}"
                return f"{c['product_id']} (need {c['required_qty']:g}, have {have} in the {payload.site_id}-SFG warehouse)"
            error = (
                "Cannot create this order - the following sub-assembly component(s) don't have enough stock yet: "
                + ", ".join(_fmt(c) for c in short_sfg)
                + ". These are produced in-house, not stocked by the Store - create/confirm a production order for "
                "them first (use \"Refresh Live SFG Stock\" once that's done, then retry)."
            )
            # Structured "result" (not just the flattened `error` string
            # above) so the frontend can render a persistent, dismissible
            # detail row instead of a toast - user's explicit ask (Aug
            # 2026) - listing EVERY short sub-assembly with its own
            # required vs available qty, same as the existing "waiting on
            # Store" detail row does for short RM components.
            job_store.update_job(db, job_id, {"status": "failed", "error": error, "result": {
                "reason": "sfg_shortage",
                "site_id": payload.site_id,
                "short_components": [
                    {"product_id": c["product_id"], "description": c.get("description"), "unit_of_measure": c.get("unit_of_measure"),
                     "required_qty": c["required_qty"], "available_qty": c["available_qty"]}
                    for c in short_sfg
                ],
            }})
            return

        job_store.update_job(db, job_id, {"status": "creating_proposal"})
        # Same check, once more right before the one-way SAP write below -
        # this is the LAST point a Stop can still avoid creating a real,
        # undeletable SAP Proposal (see _continue_order_creation's own
        # check for every checkpoint AFTER a Proposal exists).
        if job_store.get_job(db, job_id).get("cancel_requested"):
            job_store.update_job(db, job_id, {"status": "cancelled", "result": {
                "production_proposal_id": None, "production_order_id": None, "released": False,
                "note": "Stopped before anything was created in SAP - nothing to clean up.",
            }})
            return
        proposal_id = await _create_proposal_for_payload(payload, avail_dt, job_id)
        await asyncio.to_thread(
            production_confirmation_service.log_proposal_creation, db, payload.actor, payload.dict(),
            {"production_proposal_id": proposal_id}, job_id,
        )

        if short_rm:
            store_request = await asyncio.to_thread(
                store_approval_service.create_request, db, job_id, payload.dict(), proposal_id, short_rm, payload.actor,
            )
            job_store.update_job(db, job_id, {
                "status": "waiting_store_approval",
                "production_proposal_id": proposal_id,
                "store_request_id": store_request["_id"],
            })
            return

        await _continue_order_creation(job_id, payload, proposal_id)
    except Exception as e:
        logger.error(f"create-and-release job {job_id} failed: {e}")
        # Aug 27 2026 fix (user's explicit ask - "if an error happens
        # during production order creation ... it would be ideal to have
        # a recovery on app frontend"): carry forward whatever proposal_id
        # is already known (None if the failure happened before Create
        # Proposal ever ran) so the frontend can offer a "Resume" action
        # instead of forcing a trip to the SAP UI to find/finish it.
        job_store.update_job(db, job_id, {"status": "failed", "error": str(e), "result": {
            "reason": "pipeline_error", "production_proposal_id": proposal_id, "production_order_id": None,
        }})


async def _continue_order_creation(job_id: str, payload: "CreateProductionProposalRequest", proposal_id: str):
    """Resumes/runs the Proposal -> Order polling + Release pipeline. Called
    either right after Proposal creation (no stock shortfall), or later by
    _resume_order_creation_job() once a paused Store Approval request is
    resolved (store issued enough stock, store chose to proceed with a
    partial issue, or the planner approved a partial issue)."""
    try:
        job_store.update_job(db, job_id, {"status": "waiting_for_order", "production_proposal_id": proposal_id})
        await asyncio.sleep(SAP_SETTLE_DELAY_SECONDS)  # let SAP fully commit the new Proposal before anything reads/acts on it

        open_statuses = ["1", "2", "3"]
        baseline_ids = None
        baseline_prep_ids = None
        while baseline_ids is None or baseline_prep_ids is None:
            try:
                if baseline_ids is None:
                    baseline_ids = {r["production_lot_id"] for r in await asyncio.to_thread(
                        sap_production_lot_client.find_open_lots, open_statuses, payload.site_id, 999
                    )}
                if baseline_prep_ids is None:
                    baseline_prep_ids = await asyncio.to_thread(sap_production_order_release_client.list_ids_by_status, "1")
            except (SAPProductionLotError, SAPProductionOrderReleaseError) as e:
                logger.warning(f"create-and-release job {job_id}: baseline lookup hit a transient SAP error, retrying: {e}")
                await asyncio.sleep(CREATE_RELEASE_POLL_INTERVAL_SECONDS)

        elapsed_start = time.monotonic()
        last_trigger = None  # force an immediate first trigger below
        trigger_count = 0
        new_order_id = None
        order_self_released = False
        while time.monotonic() - elapsed_start <= CREATE_RELEASE_MAX_WAIT_SECONDS:
            # User-requested stop (Aug 2026 - "allow to stop the rotating
            # wheel"): checked once per poll tick, cheap Mongo read. This
            # only stops OUR OWN polling/re-triggering - it can NEVER
            # delete/cancel the SAP Proposal already created (no such SAP
            # API exists, see part-2 session note), so that stays exactly
            # as it is, un-converted, in SAP.
            current_job = job_store.get_job(db, job_id)
            if current_job and current_job.get("cancel_requested"):
                job_store.update_job(db, job_id, {"status": "cancelled", "result": {
                    "production_proposal_id": proposal_id, "production_order_id": None, "released": False,
                    "note": f"Stopped - this app will no longer auto-check for the resulting Order. Proposal {proposal_id} was already created in SAP and is NOT deleted; if SAP still converts it into an Order later, it will need to be released manually.",
                }})
                return
            elapsed = time.monotonic() - elapsed_start
            if last_trigger is None or elapsed - last_trigger >= CREATE_RELEASE_RETRIGGER_EVERY_SECONDS:
                trigger_count += 1
                trigger_ok = False
                try:
                    trigger_result = await asyncio.to_thread(sap_production_proposal_release_client.release_order, proposal_id)
                    trigger_ok = bool(trigger_result.get("success"))
                except SAPProductionOrderReleaseError as e:
                    logger.warning(f"create-and-release job {job_id}: Release-trigger attempt #{trigger_count} failed, will keep polling/retrying: {e}")
                job_store.update_job(db, job_id, {
                    "last_release_trigger_at": datetime.now(timezone.utc).isoformat(),
                    "release_trigger_count": trigger_count, "last_release_trigger_ok": trigger_ok,
                })
                last_trigger = time.monotonic() - elapsed_start
                await asyncio.sleep(SAP_SETTLE_DELAY_SECONDS)  # give SAP a beat to act on the trigger before the very next lookup
            try:
                current_rows = await asyncio.to_thread(
                    sap_production_lot_client.find_open_lots, open_statuses, payload.site_id, 999
                )
                # Same site-wide gap as the self-release branch below - two
                # concurrent orders for DIFFERENT materials at this site
                # could both appear as "new" here. Only consider ones for
                # OUR material (find_open_lots rows already carry it).
                current_ids = {r["production_lot_id"] for r in current_rows if r.get("main_output_product") == payload.material_id}
                new_ids = current_ids - baseline_ids
                if new_ids:
                    candidate_id = max(new_ids, key=lambda x: int(x) if x.isdigit() else -1)
                    # Atomic claim - guards against a SECOND concurrent job
                    # for this same material+site (another user, or the
                    # same user double-clicking) grabbing the same order.
                    if await asyncio.to_thread(_claim_order_id, db, candidate_id, job_id):
                        new_order_id = candidate_id
                        break
                    baseline_ids = current_ids  # already claimed by another job - never reconsider it
            except SAPProductionLotError as e:
                # SAP's tenant sees frequent transient connection timeouts under
                # load - a single failed poll attempt must NEVER kill the job,
                # just skip this round and try again next interval.
                logger.warning(f"create-and-release job {job_id}: poll attempt hit a transient SAP error, will retry: {e}")
            try:
                # SAP does NOT auto-assign a Production Lot (so the poll
                # above alone never fires) until the Order is actually
                # Released - confirmed live. If a brand-new "In
                # Preparation" order shows up, release it ourselves right
                # away instead of waiting on a Lot that will never appear
                # on its own.
                current_prep_ids = await asyncio.to_thread(sap_production_order_release_client.list_ids_by_status, "1")
                new_prep_ids = current_prep_ids - baseline_prep_ids
                if new_prep_ids:
                    candidate_id = max(new_prep_ids, key=lambda x: int(x) if x.isdigit() else -1)
                    # list_ids_by_status() is tenant-wide (no Site/Material
                    # filter exists on this entity) - verify this candidate
                    # is really OUR material+quantity before ever touching
                    # it (real incident: see get_requested_material docstring).
                    candidate_matches = False
                    try:
                        requested = await asyncio.to_thread(sap_production_order_release_client.get_requested_material, candidate_id)
                        candidate_matches = bool(requested) and requested["material_id"] == payload.material_id and (
                            requested["quantity"] is None or abs(requested["quantity"] - payload.quantity) < 0.001
                        )
                    except SAPProductionOrderReleaseError as e:
                        logger.warning(f"create-and-release job {job_id}: could not verify candidate order {candidate_id}'s material, skipping it this round: {e}")
                    # Claim BEFORE releasing anything - guards against a
                    # SECOND concurrent job for this same material+quantity
                    # (or a same-material job at a different site racing
                    # the tenant-wide list) claiming/releasing the same order.
                    if candidate_matches and await asyncio.to_thread(_claim_order_id, db, candidate_id, job_id):
                        release_result = await asyncio.to_thread(sap_production_order_release_client.release_order, candidate_id, True)
                        if release_result.get("success"):
                            new_order_id = candidate_id
                            order_self_released = True
                            break
                    baseline_prep_ids = current_prep_ids  # don't re-consider this same candidate (mismatched material, already claimed, or matched-but-not-releasable) next loop
            except SAPProductionOrderReleaseError as e:
                logger.warning(f"create-and-release job {job_id}: In-Preparation-order poll/self-release hit a transient SAP error, will retry: {e}")
            await asyncio.sleep(CREATE_RELEASE_POLL_INTERVAL_SECONDS)

        if not new_order_id:
            job_store.update_job(db, job_id, {"status": "done", "result": {
                "production_proposal_id": proposal_id, "production_order_id": None, "released": False,
                "note": "SAP hasn't converted this Proposal into an Order after 20 minutes of automatic retries. No action needed from you - check the Proposal/Release History below later to see if it completes.",
            }})
            return

        job_store.update_job(db, job_id, {"status": "releasing_order", "production_order_id": new_order_id})
        released = order_self_released
        if not released:
            for attempt in range(3):
                try:
                    release_result = await asyncio.to_thread(sap_production_order_release_client.release_order, new_order_id, True)
                    released = bool(release_result.get("success"))
                    break
                except SAPProductionOrderReleaseError as e:
                    logger.warning(f"create-and-release job {job_id}: Order release attempt {attempt + 1}/3 failed: {e}")
                    if attempt < 2:
                        await asyncio.sleep(5)
                    else:
                        # All 3 attempts raised (e.g. SAP rejects a repeat Release
                        # call on an already-released order) - check the real
                        # status directly before giving up, since SAP rejecting a
                        # REPEAT call is often a false negative, not a real failure.
                        try:
                            released = await asyncio.to_thread(sap_production_order_release_client.is_released, new_order_id)
                        except SAPProductionOrderReleaseError:
                            pass
        try:
            await asyncio.to_thread(sap_production_order_release_client.tag_with_proposal_id, new_order_id, proposal_id)
        except Exception as e:
            logger.warning(f"create-and-release job {job_id}: tagging order {new_order_id} with proposal_id {proposal_id} failed (non-fatal): {e}")
        await asyncio.to_thread(
            production_confirmation_service.log_order_release, db, payload.actor, new_order_id, {"success": released}, job_id,
        )
        job_store.update_job(db, job_id, {"status": "done", "result": {
            "production_proposal_id": proposal_id, "production_order_id": new_order_id, "released": released,
        }})
    except Exception as e:
        logger.error(f"create-and-release job {job_id} failed: {e}")
        # Same reasoning as _run_create_and_release_job's except block -
        # the Proposal (and possibly an Order) may already exist in SAP by
        # this point, so both are carried forward for the frontend's
        # "Resume" action rather than getting lost behind a dead-end toast.
        job_store.update_job(db, job_id, {"status": "failed", "error": str(e), "result": {
            "reason": "pipeline_error", "production_proposal_id": proposal_id, "production_order_id": new_order_id,
        }})


@api_router.post("/production-confirmation/create-and-release-order")
async def create_and_release_production_order(payload: CreateProductionProposalRequest):
    if not payload.actor.strip():
        raise HTTPException(status_code=400, detail="actor (your name) is required")
    avail_dt = None
    if payload.availability_datetime:
        try:
            avail_dt = datetime.fromisoformat(payload.availability_datetime.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="availability_datetime must be an ISO date/time")

    job_id = str(uuid.uuid4())
    # payload_snapshot is kept on the job doc (not just held in the running
    # asyncio task) so a paused "waiting_store_approval" job can be resumed
    # later from a completely fresh request (store/planner action) without
    # needing the original in-memory task to still exist.
    job_store.create_job(db, job_id, {"status": "running", "result": None, "error": None, "payload_snapshot": payload.dict()})
    asyncio.create_task(_run_create_and_release_job(job_id, payload, avail_dt))
    return {"job_id": job_id}


@api_router.get("/production-confirmation/create-and-release-order/status/{job_id}")
async def get_create_and_release_job_status(job_id: str):
    job = job_store.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return job


@api_router.post("/production-confirmation/create-and-release-order/{job_id}/cancel")
async def cancel_create_and_release_job(job_id: str):
    """"Stop the rotating wheel" (Aug 2026) - the running background job
    checks this flag once per poll tick (see _continue_order_creation) and
    stops itself, but this can only ever stop OUR OWN tracking/polling.
    It does NOT and CANNOT delete/cancel the SAP Proposal already created
    (no such SAP API exists) - it will simply sit un-converted in SAP,
    same as any other orphaned Proposal already documented in this app."""
    job = job_store.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    if job.get("status") in ("done", "failed", "cancelled"):
        raise HTTPException(status_code=400, detail="This order run has already finished - nothing to stop")
    job_store.update_job(db, job_id, {"cancel_requested": True})
    return {"ok": True}


class ResumeFailedOrderRequest(BaseModel):
    actor: str


@api_router.post("/production-confirmation/create-and-release-order/{job_id}/resume")
async def resume_failed_create_and_release_job(job_id: str, payload: ResumeFailedOrderRequest):
    """Recovery action for a job that hit a real error mid-pipeline (Aug 27
    2026, user's explicit ask - "if an error happens during production
    order creation ... it would be ideal to have a recovery on app
    frontend"). Reads the failed job's own payload_snapshot + whatever
    production_proposal_id it had already reached in SAP, and resumes the
    Proposal -> Order -> Release pipeline from exactly that point under a
    fresh job_id - no need to open the SAP UI and finish it by hand."""
    if not payload.actor.strip():
        raise HTTPException(status_code=400, detail="actor (your name) is required")
    old_job = job_store.get_job(db, job_id)
    if old_job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    if old_job.get("status") != "failed":
        raise HTTPException(
            status_code=400,
            detail="This order run is not in a failed state - resuming it would risk a second pipeline running against the same SAP Proposal. Only a failed job can be resumed.",
        )
    snapshot = old_job.get("payload_snapshot")
    proposal_id = (old_job.get("result") or {}).get("production_proposal_id")
    if not snapshot or not proposal_id:
        raise HTTPException(
            status_code=400,
            detail="Cannot resume - no SAP Proposal was ever created for this order, so there's nothing to resume from. Fix the underlying issue and create a new order instead.",
        )
    resumed_payload = CreateProductionProposalRequest(**snapshot)
    new_job_id = str(uuid.uuid4())
    job_store.create_job(db, new_job_id, {"status": "running", "result": None, "error": None, "payload_snapshot": snapshot})
    asyncio.create_task(_continue_order_creation(new_job_id, resumed_payload, proposal_id))
    return {"job_id": new_job_id, "production_proposal_id": proposal_id}


@api_router.post("/production-confirmation/refresh-live-sfg-stock")
async def refresh_live_sfg_stock(site_id: str):
    """"Refresh Live SFG Stock" (Aug 2026, user's explicit ask alongside
    the Sub-Assembly shortage block above): once the planner confirms a
    production order for the short sub-assembly, they need to see its
    now-updated SFG warehouse stock immediately before retrying order
    creation, not wait for the scheduled inventory_cache refresh. Same
    job-based pattern as the Store screen's "Refresh Live Stock Now"
    (refresh_live_stock_for_store above), just scoped to this site's SFG
    warehouse instead of RM+QC."""
    job_id = str(uuid.uuid4())
    job_store.create_job(db, job_id, {"status": "running", "error": None})
    warehouse_ids = [f"{site_id}/{site_id}-SFG"]

    async def run():
        try:
            result = await asyncio.to_thread(refresh_stock_quantities_for_warehouses, db, sap_inventory_client, warehouse_ids)
            job_store.update_job(db, job_id, {"status": "done", "error": None, "result": {"rows_found": result.get("rows_found")}})
        except Exception as e:
            logger.error(f"SFG live stock refresh job {job_id} (warehouses {warehouse_ids}) failed: {e}")
            job_store.update_job(db, job_id, {"status": "failed", "error": str(e)})

    asyncio.create_task(run())
    return {"job_id": job_id}


@api_router.get("/production-confirmation/refresh-live-sfg-stock/{job_id}")
async def get_refresh_live_sfg_stock_status(job_id: str):
    job = job_store.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return {"status": job["status"], "error": job.get("error"), "result": job.get("result")}


async def _resume_order_creation_job(job_id: str):
    """Kicks off _continue_order_creation() again for a job that was
    paused at "waiting_store_approval"/"partial_pending_planner" - used
    once the linked store_requests doc reaches status "resolved"."""
    job = job_store.get_job(db, job_id)
    snapshot = (job or {}).get("payload_snapshot")
    proposal_id = (job or {}).get("production_proposal_id")
    if not snapshot or not proposal_id:
        logger.error(f"_resume_order_creation_job: job {job_id} missing payload_snapshot/production_proposal_id, cannot resume")
        return
    payload = CreateProductionProposalRequest(**snapshot)
    asyncio.create_task(_continue_order_creation(job_id, payload, proposal_id))


class StoreIssueRequest(BaseModel):
    issued: List[dict]  # [{"product_id": str, "issued_qty": float}, ...]
    decision: Optional[str] = None  # required only when a shortfall remains: "proceed" | "send_to_planner"
    actor: str


class PlannerStoreDecisionRequest(BaseModel):
    decision: str  # "approve" | "reject"
    actor: str


# Store Binding (Aug 2026, user's explicit ask): a "store user" (plain
# "user" role) can be bound to 1+ sites on the Access Management ->
# Store Assignment tab, restricting them to only those sites here.
# admin/super_admin are never restricted - they're not "store users".
def _has_site_access(user: dict, site_id: str) -> bool:
    if user.get("role") in ("super_admin", "admin"):
        return True
    return site_id in set(user.get("bound_sites", []))


def _filter_by_site_access(rows: list, user: dict) -> list:
    if user.get("role") in ("super_admin", "admin"):
        return rows
    bound = set(user.get("bound_sites", []))
    return [r for r in rows if r.get("site_id") in bound]


def _visible_sites_for_user(user: dict, all_sites: list) -> list:
    if user.get("role") in ("super_admin", "admin"):
        return list(all_sites)
    bound = set(user.get("bound_sites", []))
    return [s for s in all_sites if s in bound]


@api_router.get("/store-requests")
async def list_store_requests(request: Request):
    """Aug 2026: now requires Entra ID login + the "store_approval" page
    permission (was previously unauthenticated by design) - user's
    explicit ask, now that the actor's name comes from their signed-in
    session instead of a free-text box."""
    rows = await asyncio.to_thread(store_approval_service.list_requests, db)
    return {"requests": _filter_by_site_access(rows, request.state.user)}


@api_router.get("/store-requests/journal")
async def get_store_requests_journal(request: Request, requester: Optional[str] = None):
    """Every store_requests doc regardless of status (pending, awaiting
    requester, resolved, cancelled) - the store team's full history/
    audit log, filtered/sorted/searched client-side since the volume is
    low. Declared BEFORE /store-requests/{request_id} so FastAPI matches
    this static path first instead of treating "journal" as a request_id.
    Bug fix (Aug 2026): this same endpoint also backs MyStockRequestsTab
    (a REQUESTER tracking requests THEY created, e.g. a production
    planner - a completely different persona from a "store user" issuing
    stock). Site Binding's fail-closed default was wrongly blanking out
    that requester's own submissions too. When `requester` is passed, skip
    the site restriction entirely and filter server-side by requester
    name instead - "show me my own requests" was never meant to be
    gated by which sites a store person is bound to."""
    rows = await asyncio.to_thread(store_approval_service.list_all_requests, db)
    if requester:
        name = requester.strip().lower()
        return {"requests": [r for r in rows if (r.get("requester") or "").strip().lower() == name]}
    return {"requests": _filter_by_site_access(rows, request.state.user)}


@api_router.get("/store-requests/balance-pending")
async def get_store_requests_balance_pending(request: Request):
    """Rule 2 (Aug 2026): requests where the store already issued a
    partial quantity and the linked order proceeded, but a balance is
    still outstanding - reopen one of these once more stock physically
    arrives in the RM warehouse. Declared BEFORE /store-requests/{request_id}
    for the same routing reason as /journal above."""
    rows = await asyncio.to_thread(store_approval_service.list_balance_pending, db)
    return {"requests": _filter_by_site_access(rows, request.state.user)}


@api_router.get("/store-requests/known-sites")
async def get_store_requests_known_sites(request: Request):
    """User's bug report (Aug 2026): the Plant/Site filter only listed
    sites that happened to have a currently-PENDING request, so a real
    site like P9 was invisible whenever it had no open request at that
    moment. Sources the full site list from inventory_cache instead
    (same pattern InventoryPage.js already uses for its own Site
    filter) - every site SAP actually has stock data for, not just
    whichever ones happen to be in the queue right now. Declared BEFORE
    /store-requests/{request_id} for the same routing reason as
    /journal above. Restricted (Aug 2026, Store Binding) to whatever
    sites this user is bound to, unless admin/super_admin."""
    sites = await asyncio.to_thread(list_known_sites, db)
    return {"sites": _visible_sites_for_user(request.state.user, sites)}


@api_router.post("/store-requests/refresh-live-stock")
async def refresh_live_stock_for_store(site_id: str, request: Request):
    """"Refresh Live Stock Now" v5 (Aug 2026) - store person's on-demand
    button for the exact "I just posted a Goods Receipt, is it visible
    yet" gap. Deliberately its OWN endpoint (not /api/inventory, which
    sits behind the `inventory` page permission) under the /store-requests
    prefix, now gated by the "store_approval" page permission like the
    rest of this workflow. User's own follow-up asks: (1) "target the
    specific warehouse, not the whole site" and (2) also cover QC (Quality
    Hold) so the store person can see material stuck there too - so this
    now pulls RM + QC together in one filtered SAP call
    (`refresh_stock_quantities_for_warehouses` - verified live: ~6.9s for
    both at once, even faster than either alone).
    Declared BEFORE /store-requests/{request_id} so this literal path
    isn't swallowed by that dynamic route."""
    if not _has_site_access(request.state.user, site_id):
        raise HTTPException(status_code=403, detail="You are not bound to this site")
    job_id = str(uuid.uuid4())
    job_store.create_job(db, job_id, {"status": "running", "error": None})
    warehouse_ids = [f"{site_id}/{site_id}-RM", f"{site_id}/{site_id}-QC"]

    async def run():
        try:
            result = await asyncio.to_thread(refresh_stock_quantities_for_warehouses, db, sap_inventory_client, warehouse_ids)
            job_store.update_job(db, job_id, {"status": "done", "error": None, "result": {"rows_found": result.get("rows_found")}})
        except Exception as e:
            logger.error(f"Store screen live stock refresh job {job_id} (warehouses {warehouse_ids}) failed: {e}")
            job_store.update_job(db, job_id, {"status": "failed", "error": str(e)})

    asyncio.create_task(run())
    return {"job_id": job_id}


@api_router.get("/store-requests/refresh-live-stock/{job_id}")
async def get_refresh_live_stock_status(job_id: str):
    job = job_store.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return {"status": job["status"], "error": job.get("error"), "result": job.get("result")}


@api_router.get("/store-requests/{request_id}")
async def get_store_request_public(request_id: str, request: Request):
    doc = await asyncio.to_thread(store_approval_service.get_request, db, request_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Store request not found")
    if not _has_site_access(request.state.user, doc.get("site_id")):
        raise HTTPException(status_code=403, detail="You are not bound to this site")
    return doc


@api_router.post("/store-requests/{request_id}/issue")
async def issue_store_request(request_id: str, payload: StoreIssueRequest, request: Request):
    """Returns immediately with a job_id - the actual SAP Goods Movement
    calls (up to 3 retries x 5s per component x N components) now run in
    the background (Aug 2026, fixed a real Cloudflare/ingress timeout on
    requests with several short components), same pattern as
    create-and-release-order. Poll GET /store-requests/issue-status/{job_id}."""
    if not payload.actor.strip():
        raise HTTPException(status_code=400, detail="actor (store user's name) is required")
    existing_doc = await asyncio.to_thread(store_approval_service.get_request, db, request_id)
    if existing_doc and not _has_site_access(request.state.user, existing_doc.get("site_id")):
        raise HTTPException(status_code=403, detail="You are not bound to this site")
    try:
        updated = await asyncio.to_thread(
            store_approval_service.start_issue, db, request_id, payload.issued, payload.decision, payload.actor.strip(),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=404, detail="Store request not found")
    job_id = str(uuid.uuid4())
    job_store.create_job(db, job_id, {
        "status": "running", "store_request_id": request_id,
        "progress_current": 0, "progress_total": len(updated["components"]),
    })
    asyncio.create_task(_run_store_issue_job(job_id, request_id))
    return {"job_id": job_id, "request": updated}


@api_router.get("/store-requests/issue-status/{job_id}")
async def get_store_issue_status(job_id: str):
    job = job_store.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return job


async def _run_store_issue_job(job_id: str, request_id: str):
    def progress_cb(current, total, product_id):
        job_store.update_job(db, job_id, {"progress_current": current, "progress_total": total, "current_component": product_id})
    try:
        updated = await asyncio.to_thread(
            store_approval_service.run_issue_movements, db, request_id, sap_goods_movement_client, progress_cb,
        )
        job_store.update_job(db, job_id, {"status": "done", "result": {"request_status": updated["status"], "request_id": request_id}})
        if updated["status"] in ("resolved", "resolved_balance_pending") and not updated.get("order_resumed"):
            # order_resumed guard: fires exactly once - Rule 2 means this
            # SAME request can reach "resolved_balance_pending" again on a
            # later reopen round, but the order-creation job has already
            # moved on by then and must NOT be resumed a second time.
            await _resume_order_creation_job(updated["job_id"])
            await asyncio.to_thread(store_approval_service.mark_order_resumed, db, request_id)
        elif updated["status"] == "partial_pending_planner":
            job_store.update_job(db, updated["job_id"], {"status": "partial_pending_planner", "store_request_id": updated["_id"]})
    except Exception as e:
        logger.error(f"store issue job {job_id} for request {request_id} failed: {e}")
        await asyncio.to_thread(store_approval_service.revert_to_prior_status_if_safe, db, request_id)
        job_store.update_job(db, job_id, {"status": "failed", "error": str(e)})


@api_router.get("/production-confirmation/store-requests/by-job/{job_id}")
async def get_store_request_by_job(job_id: str):
    doc = await asyncio.to_thread(store_approval_service.get_request_by_job, db, job_id)
    if not doc:
        raise HTTPException(status_code=404, detail="No store request linked to this job")
    return doc


@api_router.post("/production-confirmation/store-requests/{request_id}/decision")
async def decide_store_request(request_id: str, payload: PlannerStoreDecisionRequest):
    if not payload.actor.strip():
        raise HTTPException(status_code=400, detail="actor (your name) is required")
    if payload.decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'reject'")
    try:
        updated = await asyncio.to_thread(
            store_approval_service.planner_decision, db, request_id, payload.decision, payload.actor.strip(),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=404, detail="Store request not found")
    if updated["status"] in ("resolved", "resolved_balance_pending") and not updated.get("order_resumed"):
        await _resume_order_creation_job(updated["job_id"])
        await asyncio.to_thread(store_approval_service.mark_order_resumed, db, request_id)
    elif updated["status"] == "cancelled":
        job_store.update_job(db, updated["job_id"], {"status": "cancelled", "result": {
            "production_proposal_id": updated["production_proposal_id"], "production_order_id": None, "released": False,
            "note": "Order creation cancelled - the partial stock issue was rejected. The SAP Proposal was left un-converted (SAP has no API to delete a Production Proposal) - ask your SAP admin to clean it up in Fiori if it needs removing.",
        }})
    return updated


class ReleaseProductionOrderRequest(BaseModel):
    production_order_id: str
    actor: str


@api_router.post("/production-confirmation/release-order")
async def release_production_order(payload: ReleaseProductionOrderRequest):
    if not payload.actor.strip():
        raise HTTPException(status_code=400, detail="actor (your name) is required")
    try:
        result = await asyncio.to_thread(sap_production_order_release_client.release_order, payload.production_order_id, True)
    except SAPProductionOrderReleaseError as e:
        raise HTTPException(status_code=502, detail=f"SAP error: {e}")
    await asyncio.to_thread(
        production_confirmation_service.log_order_release, db, payload.actor, payload.production_order_id, result,
    )
    return result


@api_router.get("/production-confirmation/proposal-history")
async def get_proposal_and_release_history(request: Request):
    # Aug 25 2026, user's explicit ask: same Site Binding scoping as the
    # open-lots table above, applied to the Create Production Order tab's
    # Recent Activity list - admin/super_admin still see every site.
    entries = await asyncio.to_thread(production_confirmation_service.get_proposal_and_release_history, db)
    return {"entries": _filter_by_site_access(entries, request.state.user)}


class ComponentAvailabilityRequest(BaseModel):
    main_output_product: str
    confirmed_quantity: float
    site_id: str
    material_inputs: Optional[List[MaterialInputItem]] = None


@api_router.post("/production-confirmation/component-availability")
async def get_component_availability(payload: ComponentAvailabilityRequest):
    # Aug 2026 bug fix: this endpoint gates the Confirm dialog's real SAP
    # write, so it's supposed to pull LIVE SFG/RM/QC stock (see
    # check_component_availability's docstring) rather than the
    # periodically-refreshed cache - but `sap_inventory_client` was never
    # actually passed here, so it silently always fell back to the stale
    # cache. Confirmed live against a real case (PALL-433727 at site P2):
    # a goods movement outside this app's own tracked Store Approval flow
    # moved SFG stock from 0.24 to 4.24 in SAP itself, and this endpoint
    # kept reporting the old 0.24 (short) until this fix.
    #
    # Aug 27 2026: when the frontend already has this lot's own exact
    # SAP MaterialInput list (attached to the row by SAPProductionLotClient),
    # it's sent here instead of guessing a BOM by main_output_product alone
    # - see check_component_availability_from_material_inputs. Falls back
    # to the old guessing behavior only if a lot genuinely has none.
    if payload.material_inputs:
        result = await asyncio.to_thread(
            production_confirmation_service.check_component_availability_from_material_inputs,
            db, [mi.dict() for mi in payload.material_inputs], payload.confirmed_quantity, payload.site_id, sap_inventory_client,
        )
    else:
        result = await asyncio.to_thread(
            production_confirmation_service.check_component_availability,
            db, payload.main_output_product, payload.confirmed_quantity, payload.site_id, sap_inventory_client,
            None, sap_soap_client, False, sap_production_model_client, sap_production_model_bom_client,
        )
    return result


@api_router.get("/production-confirmation/bom-stock-status")
async def get_bom_stock_status_endpoint(main_output_product: str, production_model_uuid: str = None, live: bool = False):
    """Backs the Source of Supply picker's holistic BOM Component Stock
    Status panel (Aug 2026, user's explicit ask) - see
    production_confirmation_service.get_bom_stock_status. `live=false`
    (default, fired the moment a Production Model is picked) is cache-only
    for an instant first render; `live=true` (the panel's "Check Live
    Stock" button) pulls fresh from SAP. `production_model_uuid`, when
    given, resolves to that model's REAL linked BillOfMaterialID first -
    same override_bom_id pattern as create-and-release-order's own
    pre-flight check above - so this shows the exact BOM SAP will
    actually use, not bom_cache_service's "highest revision" default guess."""
    override_bom_id = None
    if production_model_uuid:
        try:
            override_bom_id = await asyncio.to_thread(
                sap_production_model_bom_client.get_bill_of_material_id_for_model, production_model_uuid,
            )
        except Exception as e:
            logger.warning(f"BOM stock status: real-BOM-for-model lookup failed for model {production_model_uuid}, using cached default instead: {e}")
    result = await asyncio.to_thread(
        production_confirmation_service.get_bom_stock_status,
        db, main_output_product, override_bom_id, sap_inventory_client if live else None, sap_soap_client,
    )
    return result


class ComponentAvailabilityBatchRow(BaseModel):
    main_output_product: Optional[str] = None
    quantity: float
    site_id: Optional[str] = None
    material_inputs: Optional[List[MaterialInputItem]] = None


class ComponentAvailabilityBatchRequest(BaseModel):
    rows: List[ComponentAvailabilityBatchRow]


@api_router.post("/production-confirmation/component-availability-batch")
async def get_component_availability_batch(payload: ComponentAvailabilityBatchRequest):
    """Powers the open-lots list's per-row stock badge - one Mongo round
    trip for all rows instead of one HTTP call per row."""
    results = await asyncio.to_thread(
        production_confirmation_service.check_component_availability_batch, db, [r.dict() for r in payload.rows],
    )
    return {"results": results}


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
    gross_weight_kg: Optional[float] = None


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
        gross_weight_kg=doc.get("gross_weight_kg"),
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
    gross_weight_kg: Optional[float] = None


@api_router.patch("/admin/components/{product_id}", response_model=ComponentMasterItem)
async def update_admin_component(product_id: str, payload: UpdateComponentMasterRequest):
    """Manually correct a component's category (marks it category_source
    "manual" so it's never overwritten by a bulk Re-Categorise) and/or set
    its Minimum Stock Level, which the Purchasing Plan's netting math reads
    directly (see purchasing_plan.get_component_msl), and/or its Procurement
    Lead Time (used only by the SAP Push-to-SAP write-back), and/or its
    Net Weight/Surface Area (local values used by the separate "Push
    Weight & Surface Area to SAP" write-back), and/or its Gross Weight
    (Aug 2026: local-only for now, no SAP field exists for it yet - kept
    for future scrap-calc reference, never sent to SAP)."""
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
    if payload.gross_weight_kg is not None:
        update["gross_weight_kg"] = payload.gross_weight_kg
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
        raise HTTPException(status_code=400, detail="Set Net Weight, Surface Area, and/or Gross Weight for this component before pushing")

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


@api_router.post("/admin/components/push-all-physical-to-sap")
async def start_push_all_physical_to_sap():
    """Bulk counterpart to the per-row 'Push to SAP' in the Weight &
    Surface Area dialog - for after a bulk Excel import of Net Wt./
    Surface Area across many components (see bulk_push_physical_to_sap's
    docstring). Same background-job/progress-polling shape as the MSL/
    Lead Time bulk push above."""
    job_id = str(uuid.uuid4())
    job_store.create_job(db, job_id, {"status": "running", "progress": {"processed": 0, "total": 0}, "result": None, "error": None})

    last_progress = {"processed": 0, "total": 0}

    def progress_callback(processed, total):
        nonlocal last_progress
        last_progress = {"processed": processed, "total": total}
        job_store.update_job(db, job_id, {"progress": last_progress})

    async def run():
        try:
            result = await asyncio.to_thread(bulk_push_physical_to_sap, db, sap_material_physical_client, progress_callback)
            job_store.update_job(db, job_id, {
                "status": "done", "progress": last_progress, "result": result, "error": None,
            })
        except Exception as e:
            logger.error(f"Bulk push-physical-to-SAP failed: {e}")
            job_store.update_job(db, job_id, {
                "status": "failed", "progress": last_progress, "result": None, "error": str(e),
            })

    asyncio.create_task(run())
    return {"job_id": job_id}


@api_router.get("/admin/components/push-all-physical-to-sap/{job_id}", response_model=PushAllToSapJobStatus)
async def get_push_all_physical_to_sap_status(job_id: str):
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


class CreateMaterialRequest(BaseModel):
    material_id: str
    product_category_id: str
    base_uom: str
    description: str


class CreateMaterialResponse(BaseModel):
    material_id: str
    uuid: str


@api_router.post("/admin/create-material", response_model=CreateMaterialResponse)
async def create_material(payload: CreateMaterialRequest):
    """Creates a brand-new Material master record directly in SAP
    (ManageMaterialIn/MaintainBundle_V1, actionCode 01). Checks first via
    QueryMaterialIn that no material with this ID already exists - SAP's
    own create behavior for a colliding ID isn't something we want to
    discover live."""
    material_id = payload.material_id.strip()
    if not material_id:
        raise HTTPException(status_code=400, detail="material_id is required")
    try:
        existing_uuid = await asyncio.to_thread(sap_material_client.resolve_uuid, material_id)
    except SAPMaterialError as e:
        raise HTTPException(status_code=502, detail=f"Could not verify material_id is free: {e}")
    if existing_uuid:
        raise HTTPException(status_code=409, detail=f"Material '{material_id}' already exists in SAP (UUID {existing_uuid})")
    try:
        result = await asyncio.to_thread(
            sap_material_create_client.create_material,
            material_id, payload.product_category_id.strip(), payload.base_uom.strip(), payload.description.strip(),
        )
    except SAPMaterialCreateError as e:
        raise HTTPException(status_code=502, detail=f"SAP rejected the material creation: {e}")
    return CreateMaterialResponse(**result)


class DeleteMaterialRequest(BaseModel):
    material_id: str


@api_router.post("/admin/delete-material")
async def delete_material(payload: DeleteMaterialRequest):
    """Deletes an unused ('In Preparation') Material - intended for
    undoing an accidental/mistaken create, not general-purpose deletion."""
    try:
        await asyncio.to_thread(sap_material_create_client.delete_material, payload.material_id.strip())
    except SAPMaterialCreateError as e:
        raise HTTPException(status_code=502, detail=f"SAP rejected the deletion: {e}")
    return {"deleted": payload.material_id.strip()}


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


class FullSyncStatus(BaseModel):
    status: str  # "idle" | "running" | "done"
    trigger: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    result: Optional[dict] = None


@api_router.get("/admin/full-sync-status", response_model=FullSyncStatus)
async def get_full_sync_status():
    doc = await asyncio.to_thread(db[FULL_SYNC_STATUS_COLLECTION].find_one, {"_id": FULL_SYNC_STATUS_ID})
    if not doc:
        return FullSyncStatus(status="idle")
    return FullSyncStatus(
        status=doc["status"], trigger=doc.get("trigger"),
        started_at=doc["started_at"].isoformat() if doc.get("started_at") else None,
        finished_at=doc["finished_at"].isoformat() if doc.get("finished_at") else None,
        result=doc.get("result"),
    )


@api_router.post("/admin/run-full-sync")
async def trigger_full_sync():
    """Manual "Run Full Sync Now" button (Admin page) - same job the 1 AM
    IST scheduler runs, useful right after a fresh deploy so a freshly-
    provisioned environment's BOM/UUID/inventory caches don't have to wait
    for the next scheduled window. Refuses to start a second overlapping
    run if one is already in progress."""
    existing = await asyncio.to_thread(db[FULL_SYNC_STATUS_COLLECTION].find_one, {"_id": FULL_SYNC_STATUS_ID})
    if existing and existing.get("status") == "running":
        return {"triggered": False, "already_running": True}
    asyncio.create_task(_run_full_sync("manual"))
    return {"triggered": True, "already_running": False}


# ---------------------------------------------------------------------------
# Inter-Plant Stock Transfer Order (Aug 2026) - see stock_transfer_service.py
# module docstring. /orders validates + persists locally first, then fires
# a background job that writes it live to SAP ("ManageCustomerRequirementIn",
# Check-then-Maintain) - wired in Aug 27 2026.
# ---------------------------------------------------------------------------
class StockTransferItemCreate(BaseModel):
    product_id: str
    source_warehouse_id: str
    requested_qty: float


class StockTransferOrderCreate(BaseModel):
    ship_to_site_id: str
    ship_to_location_id: str
    requested_delivery_date: str
    # GST / E-way bill compliance fields (Aug 2026, user's explicit ask) -
    # mandatory; pushed live to SAP's OutboundDeliveryRequest custom
    # `_KUT` fields right before Goods Issue - see
    # stock_transfer_service.py module docstring + sap_outbound_delivery_client.py.
    transportation_mode: str
    vehicle_no: str
    place_of_supply: str
    gr_no: str
    date_of_supply: str
    # Freight Forwarder / Transporter name (Aug 27 2026, user's explicit
    # ask - mandatory) - written to the SAP GST Note + ERP portal's
    # `Trans` field, printed on the Delivery Note/Gate Pass.
    freight_forwarder: str
    items: List[StockTransferItemCreate]


class StockTransferNLParseRequest(BaseModel):
    text: str


def _sto_to_response(doc: dict) -> dict:
    d = dict(doc)
    d["sto_id"] = d.pop("_id")
    if isinstance(d.get("created_at"), datetime):
        d["created_at"] = d["created_at"].isoformat()
    return d


@api_router.get("/stock-transfer/inventory")
async def get_stock_transfer_inventory(product_id: str, include_non_usable: bool = False):
    return await asyncio.to_thread(stock_transfer_service.get_product_stock_locations, db, product_id, include_non_usable, sap_hsn_client)


@api_router.get("/stock-transfer/{sto_id}/delivery-note")
async def get_stock_transfer_delivery_note(sto_id: str):
    try:
        return await asyncio.to_thread(stock_transfer_service.get_delivery_note_data, db, erp_portal_client, sto_id)
    except stock_transfer_service.StockTransferOrderNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@api_router.post("/stock-transfer/refresh-site-stock")
async def post_refresh_site_stock(site_id: str):
    """"Refresh Site Stock" (Aug 27 2026, user's explicit ask) - a Goods
    Movement/Issue just completed live in SAP and the form's cached
    quantities need to catch up immediately, same job-based pattern as
    refresh_live_sfg_stock above."""
    job_id = str(uuid.uuid4())
    job_store.create_job(db, job_id, {"status": "running", "error": None})

    async def run():
        try:
            result = await asyncio.to_thread(refresh_stock_quantities_for_site, db, sap_inventory_client, site_id)
            job_store.update_job(db, job_id, {"status": "done", "error": None, "result": {"rows_found": result.get("rows_found")}})
        except Exception as e:
            logger.error(f"Stock Transfer site stock refresh job {job_id} (site {site_id}) failed: {e}")
            job_store.update_job(db, job_id, {"status": "failed", "error": str(e)})

    asyncio.create_task(run())
    return {"job_id": job_id}


@api_router.get("/stock-transfer/refresh-site-stock/{job_id}")
async def get_refresh_site_stock_status(job_id: str):
    job = job_store.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return {"status": job["status"], "error": job.get("error"), "result": job.get("result")}


@api_router.get("/stock-transfer/ship-to-sites")
async def get_stock_transfer_ship_to_sites(ship_from_site_id: str):
    sites = await asyncio.to_thread(stock_transfer_service.ship_to_sites_for_ship_from_site, db, ship_from_site_id)
    return {"sites": sites}


@api_router.get("/stock-transfer/locations")
async def get_stock_transfer_locations(site_id: str):
    warehouses = await asyncio.to_thread(stock_transfer_service.list_known_warehouses_for_site, db, site_id)
    return {"warehouses": warehouses}


@api_router.get("/stock-transfer/suggest-source")
async def get_stock_transfer_suggested_source(product_id: str, ship_to_site_id: Optional[str] = None):
    return await asyncio.to_thread(stock_transfer_service.suggest_source_warehouse, db, product_id, ship_to_site_id)


@api_router.post("/stock-transfer/orders")
async def post_stock_transfer_order(payload: StockTransferOrderCreate, request: Request):
    actor = (request.state.user.get("name") or request.state.user.get("email") or "Unknown").strip()
    try:
        doc = await asyncio.to_thread(stock_transfer_service.create_stock_transfer_order, db, payload.dict(), actor, sap_hsn_client)
    except stock_transfer_service.StockTransferValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Live SAP write (Aug 27 2026) - Check first (always-on safety net,
    # not a togglable dry-run - user's explicit ask), then the real
    # Maintain write, as a background job so this endpoint still responds
    # instantly regardless of SAP's latency (same pattern as every other
    # SAP write in this app).
    sap_job_id = str(uuid.uuid4())
    job_store.create_job(db, sap_job_id, {"status": "running", "step": "checking", "sto_id": doc["_id"]})
    asyncio.create_task(_run_submit_sto_to_sap_job(sap_job_id, doc["_id"]))
    response = _sto_to_response(doc)
    response["sap_job_id"] = sap_job_id
    return response


async def _run_submit_sto_to_sap_job(job_id: str, sto_id: str):
    try:
        result = await asyncio.to_thread(stock_transfer_service.submit_order_to_sap, db, sap_sto_client, sto_id, job_id, sap_valuation_client)
        job_store.update_job(db, job_id, {"status": "done", "result": result, "error": None})
        # Goods Issue automation (Aug 27 2026, user's explicit ask - "we
        # need full") - fully independent, no job_id exposed to the
        # frontend's confirm-dialog progress bar at all (that dialog only
        # covers the fast Validate/Check/Create steps above and closes
        # right after). This can take much longer (SAP's own scheduling
        # decides when the Customer Requirement becomes an Outbound
        # Delivery Request) - progress is only visible via the STO doc's
        # own gi_status field, same as any other field the Recent Orders
        # table/detail modal already read.
        asyncio.create_task(_run_goods_issue_job(sto_id))
        asyncio.create_task(_run_erp_portal_sync_job(sto_id))
    except Exception as e:
        logger.error(f"Stock Transfer Order {sto_id}: live SAP submit job {job_id} failed: {e}")
        job_store.update_job(db, job_id, {"status": "failed", "result": None, "error": str(e)})


async def _run_erp_portal_sync_job(sto_id: str):
    """Legacy ERP portal sync (Aug 27 2026, user's explicit ask) - fires
    right after SAP order creation succeeds, regardless of Goods Issue
    outcome. Best-effort, same pattern as gst_note_pushed - never blocks
    or fails the SAP write itself."""
    try:
        await asyncio.to_thread(stock_transfer_service.sync_to_erp_portal, db, erp_portal_client, sap_valuation_client, sto_id)
    except Exception as e:
        logger.error(f"Stock Transfer Order {sto_id}: ERP Portal sync failed: {e}")
        await asyncio.to_thread(stock_transfer_service.mark_erp_portal_failed, db, sto_id, str(e))


async def _run_goods_issue_job(sto_id: str):
    await asyncio.to_thread(stock_transfer_service.mark_gi_job_started, db, sto_id)
    elapsed_start = time.monotonic()
    while time.monotonic() - elapsed_start <= GOODS_ISSUE_MAX_WAIT_SECONDS:
        try:
            outcome = await asyncio.to_thread(stock_transfer_service.try_post_goods_issue, db, sap_outbound_delivery_client, sap_inventory_client, sto_id)
            if outcome == "posted":
                return
        except SAPOutboundDeliveryError as e:
            # A real SAP-side rejection of the Goods Issue itself (as
            # opposed to "not created yet") - stop polling, this needs a
            # human to look at it (see gi_status/gi_error on the STO doc).
            logger.error(f"Stock Transfer Order {sto_id}: Goods Issue post failed: {e}")
            return
        except Exception as e:
            logger.warning(f"Stock Transfer Order {sto_id}: Goods Issue poll attempt hit a transient error, will retry: {e}")
        await asyncio.sleep(GOODS_ISSUE_POLL_INTERVAL_SECONDS)
    await asyncio.to_thread(
        stock_transfer_service.mark_goods_issue_timed_out, db, sto_id,
        "SAP hasn't produced the Outbound Delivery Request for this order after 20 minutes of automatic checks. "
        "The Stock Transfer Order itself is still valid in SAP - this only affects automatic Goods Issue posting.",
        "Stock at the source warehouse is still insufficient after 20 minutes of automatic checks. "
        "The Stock Transfer Order itself is still valid in SAP - retry Goods Issue once stock is replenished.",
    )


@api_router.get("/stock-transfer/orders/sap-status/{job_id}")
async def get_stock_transfer_sap_status(job_id: str):
    job = job_store.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return job


@api_router.get("/stock-transfer/orders")
async def get_stock_transfer_orders():
    docs = await asyncio.to_thread(stock_transfer_service.list_stock_transfer_orders, db)
    return [_sto_to_response(d) for d in docs]


@api_router.get("/stock-transfer/orders/{sto_id}")
async def get_stock_transfer_order(sto_id: str):
    """Single-order lookup (Aug 27 2026) - backs the confirm dialog's
    live Goods Issue progress step, polled right after order creation,
    independent of the job-store status used for the fast Check/Create
    steps above."""
    try:
        doc = await asyncio.to_thread(stock_transfer_service.get_stock_transfer_order, db, sto_id)
    except stock_transfer_service.StockTransferOrderNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _sto_to_response(doc)


@api_router.post("/stock-transfer/orders/{sto_id}/retry")
async def post_stock_transfer_order_retry(sto_id: str):
    """Manual retry (Aug 2026, user's explicit ask) for an order that
    failed at SAP's Check step - e.g. after an admin activates a missing
    Planning/Logistics site via /admin/material-sites/activate. Re-runs
    the exact same Check-then-Maintain flow as a fresh order creation."""
    sap_job_id = str(uuid.uuid4())
    job_store.create_job(db, sap_job_id, {"status": "running", "step": "checking", "sto_id": sto_id})
    asyncio.create_task(_run_submit_sto_to_sap_job(sap_job_id, sto_id))
    return {"sap_job_id": sap_job_id}


@api_router.post("/stock-transfer/orders/{sto_id}/retry-goods-issue")
async def post_stock_transfer_order_retry_goods_issue(sto_id: str):
    """Manual "Retry Goods Issue" (Aug 27 2026, user's explicit ask,
    following a real GI failure "Inventory in logistics area not
    available") - for an order whose Goods Issue is "failed" or
    "not_found_timeout". Restarts the same 20-min auto-poll loop, which
    now always live-checks the source warehouse's real stock before
    attempting the Goods Issue again (see try_post_goods_issue)."""
    try:
        await asyncio.to_thread(stock_transfer_service.reset_goods_issue_for_retry, db, sto_id)
    except stock_transfer_service.StockTransferOrderNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except stock_transfer_service.StockTransferValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    asyncio.create_task(_run_goods_issue_job(sto_id))
    return {"status": "restarted"}


@api_router.post("/stock-transfer/orders/{sto_id}/retry-erp-sync")
async def post_stock_transfer_order_retry_erp_sync(sto_id: str):
    """Manual "Retry ERP Sync" (Aug 27 2026, user's explicit ask - a
    failed sync here means no legal Delivery Challan can ever be
    printed, since the Serial Number comes from the ERP portal's own
    Sale_No). erp_portal_client already tries the primary MS SQL host
    first, then the fallback host, on any connection failure - this just
    re-triggers that same sync_to_erp_portal call after a guarded status
    reset (see reset_erp_portal_sync_for_retry - blocks a duplicate
    Delivery Challan if this order already synced successfully)."""
    try:
        await asyncio.to_thread(stock_transfer_service.reset_erp_portal_sync_for_retry, db, sto_id)
    except stock_transfer_service.StockTransferOrderNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except stock_transfer_service.StockTransferValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    asyncio.create_task(_run_erp_portal_sync_job(sto_id))
    return {"status": "restarted"}



@api_router.get("/admin/notifications")
async def get_admin_notifications(request: Request):
    user = await asyncio.to_thread(auth_service.get_current_user, request, db)
    if not user or user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return {"notifications": await asyncio.to_thread(stock_transfer_service.list_open_admin_notifications, db)}


class ActivateMaterialSiteRequest(BaseModel):
    product_id: str
    site_id: str
    notification_id: str = None


@api_router.post("/admin/material-sites/activate")
async def post_activate_material_site(payload: ActivateMaterialSiteRequest, request: Request):
    user = await asyncio.to_thread(auth_service.get_current_user, request, db)
    if not user or user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    company_id, _ = company_and_set_of_books_for_site(payload.site_id)
    result = await asyncio.to_thread(sap_material_create_client.activate_site, payload.product_id, payload.site_id, company_id)
    if payload.notification_id and result.get("planning_logistics") == "ok":
        await asyncio.to_thread(stock_transfer_service.resolve_admin_notification, db, payload.notification_id)
    return result


@api_router.post("/stock-transfer/parse-nl")
async def post_stock_transfer_parse_nl(payload: StockTransferNLParseRequest):
    known_sites = await asyncio.to_thread(list_known_sites, db)
    try:
        return await stock_transfer_service.parse_natural_language_transfer_request(payload.text, known_sites)
    except stock_transfer_service.StockTransferValidationError as e:
        raise HTTPException(status_code=502, detail=str(e))


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
# inventory_service.refresh_inventory_cache) - every 30 min (tightened
# from 2h per user request, Aug 2026 - this SAP report is a genuinely
# heavy OLAP query (~47s for ~6000 rows), so this is a deliberate balance:
# tight enough that staleness rarely matters, not so tight it hammers SAP.
# The one place staleness actually matters (auto-release's stock gate)
# does its own live check instead of relying on this cache - see
# check_component_availability's live-first behavior below.
INVENTORY_CACHE_REFRESH_INTERVAL_SECONDS = 30 * 60


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


# Aug 27 2026, user's explicit ask ("company erp address table also
# store in mongo since it does not change") - Delivery Note/Gate Pass
# previously queried the ERP's `comp` table live on every single print;
# this refreshes ALL sites' Company Name/Address/GSTIN/PAN once at
# startup and every 12h after, so print pages read from Mongo instead.
COMPANY_CACHE_REFRESH_INTERVAL_SECONDS = 12 * 60 * 60


@app.on_event("startup")
async def start_company_cache_refresh_loop():
    async def loop():
        await asyncio.sleep(15)
        while True:
            try:
                companies = await asyncio.to_thread(company_cache_service.refresh_company_cache, db, erp_portal_client)
                logger.info(f"ERP company/address cache background refresh complete: {len(companies)} site(s).")
            except Exception as e:
                logger.error(f"ERP company/address cache background refresh failed: {e}")
            await asyncio.sleep(COMPANY_CACHE_REFRESH_INTERVAL_SECONDS)

    asyncio.create_task(loop())


# Background maintenance: a consolidated "Nightly Sync" scheduled for
# 1 AM IST (a genuinely quiet window for this tenant, per user request) -
# runs the heavier full-catalog jobs back-to-back, off business hours,
# instead of at unpredictable/immediate-after-restart times:
#   0. catalog_prefetch - bulk_prefetch against EVERY current inventory
#      item's product_id (see bom_cache_service.bulk_prefetch) - closes a
#      real gap found live (Aug 2026): `deep_expand_all_known_roots` only
#      ever re-walks roots ALREADY in the cache, so an item that's
#      genuinely a BOM root in SAP but was NEVER individually looked up by
#      any page (BOM Explorer/Purchasing Plan/MRP/L1L2 Report) stayed
#      falsely flagged "No BOM" even after a full nightly/manual sync -
#      this step is what actually discovers it for the first time. This is
#      exactly why a freshly-deployed environment (little page-usage
#      history yet) can show a much higher "No BOM" count than one that's
#      been used for a while, even right after running this same job.
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
FULL_SYNC_STATUS_COLLECTION = "full_sync_status"
FULL_SYNC_STATUS_ID = "latest"
_EXTRA_ROOT_SEED_PATH = os.path.join(os.path.dirname(__file__), "data", "bom_extra_root_seed_ids.json")


def _load_extra_root_seed_ids() -> list:
    """Aug 2026: a big chunk of the "No BOM" gap between environments turned
    out to be genuine top-level assemblies/sub-assemblies that are NOT
    themselves tracked as stocked inventory items (e.g. `5989826`, the
    assembled product, vs `5989826-10`/`5989826-9`, its physical component
    parts which ARE inventory items) - so `catalog_prefetch` above, scoped
    only to current inventory product_ids, can never discover them as
    candidate roots on a lightly-used environment. This ships a one-time
    snapshot of every product_id Preview's `bom_node_cache` had already
    discovered as of Aug 2026 (4,679 ids, ~1,465 of them NOT in the
    inventory catalog) so ANY environment's Full Sync can directly re-check
    them all live against its OWN SAP tenant too - bulk_prefetch already
    skips whatever's already cached, so this is safe/idempotent to keep
    feeding in on every run."""
    try:
        with open(_EXTRA_ROOT_SEED_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"Full sync: could not load extra root seed ids ({e}), skipping")
        return []


def _seconds_until_next_nightly_sync() -> float:
    now_ist = datetime.now(timezone.utc) + IST_OFFSET
    next_run_ist = now_ist.replace(hour=NIGHTLY_SYNC_HOUR_IST, minute=0, second=0, microsecond=0)
    if next_run_ist <= now_ist:
        next_run_ist += timedelta(days=1)
    return (next_run_ist - now_ist).total_seconds()


async def _run_full_sync(trigger: str) -> dict:
    """The actual "Nightly Sync" work (catalog-wide root prefetch -> deep
    BOM re-expansion -> UUID backfill -> inventory/cost refresh), shared
    by the 1 AM IST scheduled loop below AND the manual "Run Full Sync
    Now" admin button - same job, two ways to kick it off (e.g. right
    after a fresh Production deploy, instead of waiting for the next 1 AM
    IST window). Persists progress to a single `full_sync_status` doc so
    the button can show "already running" instead of firing a duplicate
    overlapping run."""
    status_collection = db[FULL_SYNC_STATUS_COLLECTION]
    started_at = datetime.now(timezone.utc)
    status_collection.update_one(
        {"_id": FULL_SYNC_STATUS_ID},
        {"$set": {"status": "running", "trigger": trigger, "started_at": started_at, "finished_at": None, "result": None, "error": None}},
        upsert=True,
    )
    logger.info(f"Full sync ({trigger}) starting: catalog prefetch -> deep BOM expansion -> UUID backfill -> inventory/cost refresh")
    result = {}
    try:
        catalog_product_ids = [it["product_id"] for it in get_cached_inventory(db)["items"]]
        catalog_product_ids = list(set(catalog_product_ids) | set(_load_extra_root_seed_ids()))
        result["catalog_prefetch"] = await asyncio.to_thread(bom_cache_service.bulk_prefetch, catalog_product_ids, sap_soap_client, db)
        logger.info(f"Full sync: catalog prefetch complete: {result['catalog_prefetch']}")
    except Exception as e:
        logger.error(f"Full sync: catalog prefetch failed: {e}")
    try:
        result["bom_expansion"] = await asyncio.to_thread(bom_cache_service.deep_expand_all_known_roots, sap_soap_client, db)
        logger.info(f"Full sync: deep BOM expansion complete: {result['bom_expansion']}")
    except Exception as e:
        logger.error(f"Full sync: deep BOM expansion failed: {e}")
    try:
        result["uuid_backfill"] = await asyncio.to_thread(deep_backfill_uuids, db, sap_soap_client, sap_material_client)
        logger.info(f"Full sync: UUID backfill complete: {result['uuid_backfill']}")
    except Exception as e:
        logger.error(f"Full sync: UUID backfill failed: {e}")
    try:
        cached = await asyncio.to_thread(refresh_inventory_cache, db, sap_inventory_client, sap_valuation_client)
        result["inventory_items_refreshed"] = len(cached["items"])
        logger.info(f"Full sync: inventory/cost refresh complete: {result['inventory_items_refreshed']} item(s)")
    except Exception as e:
        logger.error(f"Full sync: inventory/cost refresh failed: {e}")
    finished_at = datetime.now(timezone.utc)
    status_collection.update_one(
        {"_id": FULL_SYNC_STATUS_ID},
        {"$set": {"status": "done", "finished_at": finished_at, "result": result}},
    )
    logger.info(f"Full sync ({trigger}) finished in {(finished_at - started_at).total_seconds():.0f}s")
    return result


@app.on_event("startup")
async def start_nightly_sync_loop():
    async def loop():
        while True:
            await asyncio.sleep(_seconds_until_next_nightly_sync())
            await _run_full_sync("scheduled")

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

