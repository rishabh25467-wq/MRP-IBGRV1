"""Iteration 169 - Sep 14 2026 fix verification.

Real incident: Store Request P9-000110 asked the store to issue the
FULL BOM requirement (e.g. 320 EA of PDQ80110-2) even though 80 EA was
already sitting in SFG stock (available_qty), ready for this production
- the store should only ever be asked for the net shortfall (240 EA).
"""
import sys
sys.path.insert(0, "/app/backend")
from pymongo import MongoClient
from dotenv import dotenv_values
import store_approval_service

backend_env = dotenv_values("/app/backend/.env")
client = MongoClient(backend_env["MONGO_URL"])
db = client[backend_env["DB_NAME"]]


def _cleanup(doc_id):
    db[store_approval_service.COLLECTION].delete_one({"_id": doc_id})


def test_required_qty_nets_off_available_stock():
    payload = {"material_id": "TEST_PDQ80110", "site_id": "P9", "quantity": 1.0, "unit_code": "EA"}
    short_components = [{
        "product_id": "PDQ80110-2", "description": "Top Filler", "unit_of_measure": "EA",
        "required_qty": 320.0, "available_qty": 80.0, "locations": [],
    }]
    doc = store_approval_service.create_request(db, "TEST_JOB_169", payload, "TEST_PROP_169", short_components, "TEST_QA")
    try:
        assert doc["components"][0]["required_qty"] == 240.0, doc["components"][0]
        assert doc["components"][0]["available_qty"] == 80.0
    finally:
        _cleanup(doc["_id"])


def test_required_qty_floors_at_zero_when_available_exceeds_required():
    short_components = [{
        "product_id": "COMP_X", "required_qty": 50.0, "available_qty": 999.0, "locations": [],
    }]
    doc = store_approval_service.create_request(
        db, "TEST_JOB_169B", {"material_id": "M", "site_id": "P9", "quantity": 1.0, "unit_code": "EA"},
        "TEST_PROP_169B", short_components, "TEST_QA",
    )
    try:
        assert doc["components"][0]["required_qty"] == 0.0
    finally:
        _cleanup(doc["_id"])


def test_required_qty_unchanged_when_no_available_stock():
    short_components = [{"product_id": "COMP_Y", "required_qty": 100.0, "available_qty": None, "locations": []}]
    doc = store_approval_service.create_request(
        db, "TEST_JOB_169C", {"material_id": "M", "site_id": "P9", "quantity": 1.0, "unit_code": "EA"},
        "TEST_PROP_169C", short_components, "TEST_QA",
    )
    try:
        assert doc["components"][0]["required_qty"] == 100.0
    finally:
        _cleanup(doc["_id"])
