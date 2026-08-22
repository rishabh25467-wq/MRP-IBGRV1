"""Seed synthetic store_requests for frontend (Playwright) testing of the
async Issue-Stock job. Deliberately placed OUTSIDE /app/backend so writing
it never triggers uvicorn's --reload watcher.

Components have NO `locations` (except the restricted-stock regression doc,
whose locations are all Quality-Inspection) so no real SAP write can fire.

Usage:  python /app/tests/seed_store_ui_requests.py [--cleanup]
"""
import sys
from datetime import datetime, timezone

from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
client = MongoClient(env["MONGO_URL"])
db = client[env["DB_NAME"]]

SITE = "ZZ97"
TAG = "TEST_UI_ISSUE"
IDS = {
    "pending": f"{SITE}-UI0001",
    "issuing": f"{SITE}-UI0002",
    "restricted": f"{SITE}-UI0003",
}


def base_doc(req_id, status, components):
    now = datetime.now(timezone.utc)
    job_id = f"{TAG}-job-{req_id}"
    db["background_jobs"].replace_one({"_id": job_id}, {
        "_id": job_id, "status": "waiting_store_approval", "result": None, "error": None,
        "production_proposal_id": "TEST_UI_PROPOSAL",
        "store_request_id": req_id,
        "payload_snapshot": {
            "material_id": "TEST_UI_MAT", "site_id": SITE, "quantity": 2.0, "unit_code": "EA",
            "availability_datetime": None, "actor": TAG, "logistic_relationship_uuid": None,
        },
        "created_at": now, "updated_at": now,
    }, upsert=True)
    return {
        "_id": req_id, "job_id": job_id, "production_proposal_id": "TEST_UI_PROPOSAL",
        "material_id": "TEST_UI_MAT", "site_id": SITE, "quantity": 2.0, "unit_code": "EA",
        "requester": TAG, "components": components, "status": status,
        "store_actor": None, "store_decision": None, "planner_actor": None,
        "planner_decision": None, "resolution": None,
        "created_at": now, "updated_at": now, "resolved_at": None,
    }


def comps(with_issued=False):
    out = []
    for pid, uom, req in (("TEST_UI_COMP_A", "EA", 10.0), ("TEST_UI_COMP_B", "MASS", 4.0)):
        out.append({
            "product_id": pid, "description": f"{TAG} {pid}", "unit_of_measure": uom,
            "required_qty": req, "available_qty": 1.0, "locations": [],
            "issued_qty": req if with_issued else None,
            "shortfall": 0.0 if with_issued else None,
            "goods_movement": None,
        })
    return out


def cleanup():
    db["store_requests"].delete_many({"_id": {"$in": list(IDS.values())}})
    db["background_jobs"].delete_many({"_id": {"$regex": f"^{TAG}-job-"}})
    print("cleaned up", list(IDS.values()))


def seed():
    db["store_requests"].replace_one({"_id": IDS["pending"]}, base_doc(IDS["pending"], "pending", comps()), upsert=True)
    db["store_requests"].replace_one({"_id": IDS["issuing"]}, base_doc(IDS["issuing"], "issuing", comps(with_issued=True)), upsert=True)
    restricted = [{
        "product_id": "TEST_UI_COMP_HOLD", "description": f"{TAG} on-hold comp", "unit_of_measure": "MASS",
        "required_qty": 6.0, "available_qty": 6.0,
        "locations": [
            {"warehouse_id": f"{SITE}/{SITE}-RM", "warehouse": f"{SITE}-RM", "qty": 6.0,
             "stock_status": "Inspection", "owner": "TEST_OWNER"},
        ],
        "issued_qty": None, "shortfall": None, "goods_movement": None,
    }]
    db["store_requests"].replace_one({"_id": IDS["restricted"]}, base_doc(IDS["restricted"], "pending", restricted), upsert=True)
    print("seeded", list(IDS.values()))


if __name__ == "__main__":
    cleanup() if "--cleanup" in sys.argv else seed()
