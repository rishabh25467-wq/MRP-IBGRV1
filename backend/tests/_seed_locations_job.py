"""Seeds a paused background_job + store_request (with per-component 'locations')
for UI testing of the Active Orders table breakdown. Prints job_id and request_id.
Usage: python _seed_locations_job.py <status>   # waiting_store_approval | partial_pending_planner
Delete with: python _seed_locations_job.py --clean
"""
import os
import sys
import uuid
from datetime import datetime, timezone

from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
db = MongoClient(os.environ.get("MONGO_URL") or env["MONGO_URL"])[os.environ.get("DB_NAME") or env["DB_NAME"]]

TAG = "TEST_UI_LOCATIONS"

if "--clean" in sys.argv:
    j = db.background_jobs.delete_many({"payload_snapshot.actor": TAG})
    r = db.store_requests.delete_many({"requester": TAG})
    print(f"deleted jobs={j.deleted_count} requests={r.deleted_count}")
    raise SystemExit

status = sys.argv[1] if len(sys.argv) > 1 else "waiting_store_approval"
partial = status == "partial_pending_planner"
now = datetime.now(timezone.utc)
job_id = str(uuid.uuid4())
req_id = str(uuid.uuid4())

comps = [
    {"product_id": "TEST_UI_COMP_A", "description": "UI comp A", "unit_of_measure": "EA",
     "required_qty": 200.0, "available_qty": 100.0,
     "locations": [{"warehouse": "P2-WH-RM", "stock_status": "Unrestricted", "qty": 100.0}, {"warehouse": "P2-WH-RM", "stock_status": "Quality Inspection", "qty": 50.0}],
     "issued_qty": 120.0 if partial else None, "shortfall": 80.0 if partial else None},
    {"product_id": "TEST_UI_COMP_B", "description": "UI comp B", "unit_of_measure": "KGM",
     "required_qty": 10.0, "available_qty": 0.0,
     "locations": [{"warehouse": "P2-WH-FG", "stock_status": "Unrestricted", "qty": 7.5}],
     "issued_qty": 2.0 if partial else None, "shortfall": 8.0 if partial else None},
]
db.background_jobs.insert_one({
    "_id": job_id, "status": status, "result": None, "error": None,
    "production_proposal_id": "TEST_PROPOSAL_UI_LOC", "store_request_id": req_id,
    "payload_snapshot": {"material_id": "TEST_UI_MAT", "site_id": "ZZ99", "quantity": 3.0,
                         "unit_code": "EA", "availability_datetime": None, "actor": TAG,
                         "logistic_relationship_uuid": None},
    "created_at": now, "updated_at": now,
})
db.store_requests.insert_one({
    "_id": req_id, "job_id": job_id, "production_proposal_id": "TEST_PROPOSAL_UI_LOC",
    "material_id": "TEST_UI_MAT", "site_id": "ZZ99", "quantity": 3.0, "unit_code": "EA",
    "requester": TAG, "components": comps,
    "status": "partial_pending_planner" if partial else "pending",
    "store_actor": TAG if partial else None, "store_decision": "send_to_planner" if partial else None,
    "planner_actor": None, "planner_decision": None, "resolution": None,
    "created_at": now, "updated_at": now, "resolved_at": None,
})
print(f"{job_id} {req_id}")
