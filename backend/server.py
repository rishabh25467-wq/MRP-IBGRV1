import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException, Query
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware

from sap_soap_client import SAPSoapBOMClient, SAPSoapError
from sap_valuation_client import SAPValuationClient, SAPValuationError
from bom_categorizer import categorize_items, BomCategorizerError
from oms_client import OMSClient
from purchasing_plan import build_purchasing_plan

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()
api_router = APIRouter(prefix="/api")

sap_soap_client = SAPSoapBOMClient(
    endpoint=os.environ['SAP_SOAP_ENDPOINT'],
    username=os.environ['SAP_SOAP_USERNAME'],
    password=os.environ['SAP_SOAP_PASSWORD'],
)

sap_valuation_client = SAPValuationClient(
    base_url=os.environ['SAP_ODATA_BASE_URL'],
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
    unit_cost: Optional[float] = None
    currency: Optional[str] = None
    value_by_month: dict[str, Optional[float]]


class MissingBom(BaseModel):
    part_no: str
    sap_id: Optional[str] = None
    reason: str


class PurchasingPlanResponse(BaseModel):
    months: List[str]
    components: List[PurchasingPlanComponent]
    missing_boms: List[MissingBom]


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
        categories = await categorize_items(payload.items)
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
                build_purchasing_plan, oms_client, sap_soap_client, sap_valuation_client, target_month
            )
            # Classify every leaf component by material/type category (same AI
            # categorizer the BOM Explorer uses) so the plan can be split/grouped
            # by category - best-effort, a categorizer hiccup shouldn't fail the plan.
            items = [{"product_id": c["product_id"], "description": c["description"]} for c in result["components"]]
            if items:
                try:
                    categories = await categorize_items(items)
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


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

