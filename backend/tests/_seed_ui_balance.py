"""Seeds synthetic store_requests (EMPTY locations -> no real SAP write) plus a
super_admin auth session, for UI testing of the Rule 2 reopen flow and the new
'My Stock Requests' tab. Prints: <session_token> <balance_req_id> <pending_req_id>
Cleanup: python _seed_ui_balance.py --clean
"""
import secrets
import sys
from datetime import datetime, timedelta, timezone

from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
db = MongoClient(env["MONGO_URL"])[env["DB_NAME"]]

REQUESTER = "QA UI Tester"
SITE = "ZZ96"
BAL_ID = f"{SITE}-UITEST01"
PEND_ID = f"{SITE}-UITEST02"
USER_ID = "2a94b71e-cd64-4c3d-aede-24eb89ab5fad:TEST-UI-BAL"

if "--clean" in sys.argv:
    print(db.store_requests.delete_many({"_id": {"$in": [BAL_ID, PEND_ID]}}).deleted_count)
    db.auth_sessions.delete_many({"user_id": USER_ID})
    db.auth_users.delete_one({"_id": USER_ID})
    db.background_jobs.delete_many({"production_proposal_id": "TEST_PROPOSAL_UI"})
    sys.exit(0)

now = datetime.now(timezone.utc)
token = "TESTUIBAL" + secrets.token_urlsafe(24)
db.auth_users.replace_one({"_id": USER_ID}, {
    "_id": USER_ID, "tid": "2a94b71e-cd64-4c3d-aede-24eb89ab5fad", "oid": "TEST-UI-BAL",
    "email": "qa.ui@rampgroup.co.in", "name": "QA UI Tester", "role": "super_admin",
    "allowed_pages": [], "created_at": now, "last_login_at": now,
}, upsert=True)
db.auth_sessions.insert_one({"_id": token, "user_id": USER_ID,
                             "expires_at": now + timedelta(hours=6)})

db.store_requests.delete_many({"_id": {"$in": [BAL_ID, PEND_ID]}})
db.store_requests.insert_one({
    "_id": BAL_ID, "job_id": "UI-TEST-JOB-1", "production_proposal_id": "TEST_PROPOSAL_UI",
    "material_id": "TEST_UI_MAT", "site_id": SITE, "quantity": 2.0, "unit_code": "EA",
    "requester": REQUESTER, "status": "resolved_balance_pending",
    "resolution": "store_proceeded_partial", "order_resumed": True,
    "store_actor": "QA Store", "store_decision": "proceed",
    "planner_actor": None, "planner_decision": None,
    "components": [
        {"product_id": "TEST_UI_A", "description": "UI Test Component A", "unit_of_measure": "EA",
         "required_qty": 5.0, "available_qty": 3.0, "locations": [], "issued_qty": 3.0,
         "issued_this_round": 3.0, "shortfall": 2.0},
        {"product_id": "TEST_UI_B", "description": "UI Test Component B", "unit_of_measure": "MASS",
         "required_qty": 10.0, "available_qty": 10.0, "locations": [], "issued_qty": 10.0,
         "issued_this_round": 10.0, "shortfall": 0.0},
    ],
    "created_at": now, "updated_at": now, "resolved_at": now,
})
db.store_requests.insert_one({
    "_id": PEND_ID, "job_id": "UI-TEST-JOB-2", "production_proposal_id": "TEST_PROPOSAL_UI",
    "material_id": "TEST_UI_MAT2", "site_id": SITE, "quantity": 1.0, "unit_code": "EA",
    "requester": REQUESTER, "status": "pending", "resolution": None,
    "store_actor": None, "store_decision": None, "planner_actor": None, "planner_decision": None,
    "components": [
        {"product_id": "TEST_UI_A", "description": "UI Test Component A", "unit_of_measure": "EA",
         "required_qty": 4.0, "available_qty": 0.0, "locations": [], "issued_qty": None,
         "shortfall": None},
    ],
    "created_at": now, "updated_at": now, "resolved_at": None,
})
print(token, BAL_ID, PEND_ID)
