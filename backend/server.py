import asyncio
import logging
import os
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException, Query
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware

from sap_client import SAPBOMClient, SAPConnectionError

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()
api_router = APIRouter(prefix="/api")

sap_client = SAPBOMClient(
    instance_url=os.environ['SAP_INSTANCE_URL'],
    username=os.environ['SAP_USERNAME'],
    password=os.environ['SAP_PASSWORD'],
)


class BomComponent(BaseModel):
    material_id: Optional[str] = None
    quantity: Optional[float] = None
    unit_of_measure: Optional[str] = None
    eco_id: Optional[str] = None
    quantity_fixed: bool = False
    active: bool = True


class BomGroup(BaseModel):
    group_id: Optional[str] = None
    group_number: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    components: List[BomComponent] = []


class BomSearchResponse(BaseModel):
    bom_id: str
    object_id: Optional[str] = None
    total_components: int
    total_groups: int
    groups: List[BomGroup] = []


class ConnectionStatus(BaseModel):
    connected: bool
    message: str


@api_router.get("/")
async def root():
    return {"message": "SAP BOM Lookup API"}


@api_router.get("/bom/connection-status", response_model=ConnectionStatus)
async def connection_status():
    try:
        await asyncio.to_thread(sap_client.check_connection)
        return ConnectionStatus(connected=True, message="Connected to SAP Business ByDesign")
    except SAPConnectionError as e:
        return ConnectionStatus(connected=False, message=str(e))


@api_router.get("/bom/search", response_model=BomSearchResponse)
async def search_bom(bom_id: str = Query(..., min_length=1)):
    try:
        result = await asyncio.to_thread(sap_client.search_bom, bom_id.strip())
    except SAPConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if result is None:
        raise HTTPException(status_code=404, detail=f"BOM '{bom_id}' not found in SAP")

    return BomSearchResponse(
        bom_id=result["bom_id"],
        object_id=result["object_id"],
        total_components=result["total_components"],
        total_groups=len(result["groups"]),
        groups=result["groups"],
    )


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)
