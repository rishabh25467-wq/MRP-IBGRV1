import asyncio
import logging
import os
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException, Query
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware

from sap_soap_client import SAPSoapBOMClient, SAPSoapError

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


class BomRow(BaseModel):
    level: int
    group_id: Optional[str] = None
    item_id: Optional[str] = None
    product_id: str
    description: Optional[str] = None
    quantity: Optional[float] = None
    unit_of_measure: Optional[str] = None
    eco_id: Optional[str] = None
    active: bool = True
    has_sub_bom: bool = False


class BomSearchResponse(BaseModel):
    bom_id: str
    total_components: int
    max_level: int
    rows: List[BomRow] = []


class ConnectionStatus(BaseModel):
    connected: bool
    message: str


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

    max_level = max((row["level"] for row in result["rows"]), default=1)

    return BomSearchResponse(
        bom_id=result["bom_id"],
        total_components=result["total_components"],
        max_level=max_level,
        rows=result["rows"],
    )


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

