"""UI seed/cleanup for iteration_104 (Rule 1/Rule 2/new tables).
python /app/tests/seed_ui_104.py            -> seed + print session token
python /app/tests/seed_ui_104.py --cleanup  -> remove everything
"""
import secrets
import sys
from datetime import datetime, timedelta, timezone

from dotenv import dotenv_values
from pymongo import MongoClient

be = dotenv_values("/app/backend/.env")
client = MongoClient(be["MONGO_URL"])
db = client[be["DB_NAME"]]

SITE = "ZZ95"
REQUESTER = "QA Tester 104"
USER_ID = "TEST-TID-104:TEST-OID-104"
TOKEN_FILE = "/tmp/iteration_104_token.txt"
IDS = {
    "balance": "TEST_UI104-BAL",
    "pending": "TEST_UI104-PEND",
    "resolved": "TEST_UI104-DONE",
}


def comp(pid, req, issued, short, locations=None):
    return {"product_id": pid, "description": f"UI104 {pid}", "unit_of_measure": "EA",
            "required_qty": req, "available_qty": issued, "locations": locations or [],
            "issued_qty": issued, "shortfall": short}


def seed():
    now = datetime.now(timezone.utc)
    docs = [
        {"_id": IDS["balance"], "status": "resolved_balance_pending", "resolution": "store_proceeded_partial",
         "components": [comp("TEST_UI104_A", 5.0, 3.0, 2.0), comp("TEST_UI104_B", 10.0, 10.0, 0.0)],
         "store_actor": "QA Store", "store_decision": "proceed", "resolved_at": now},
        {"_id": IDS["pending"], "status": "pending", "resolution": None,
         "components": [comp("TEST_UI104_A", 4.0, None, None), comp("TEST_UI104_C", 7.0, None, None)],
         "store_actor": None, "store_decision": None, "resolved_at": None},
        {"_id": IDS["resolved"], "status": "resolved", "resolution": "full_issue",
         "components": [comp("TEST_UI104_A", 6.0, 6.0, 0.0)],
         "store_actor": "QA Store", "store_decision": None, "resolved_at": now},
    ]
    for i, d in enumerate(docs):
        d.update({
            "job_id": f"TEST_UI104_JOB_{i}", "production_proposal_id": "TEST_UI104_PROP",
            "material_id": f"TEST_UI104_MAT_{i}", "site_id": SITE, "quantity": 10.0, "unit_code": "EA",
            "requester": REQUESTER, "planner_actor": None, "planner_decision": None,
            "created_at": now - timedelta(minutes=10 * i), "updated_at": now - timedelta(minutes=i),
        })
        db["store_requests"].replace_one({"_id": d["_id"]}, d, upsert=True)

    token = "TESTSESSION104" + secrets.token_hex(16)
    db["auth_users"].replace_one({"_id": USER_ID}, {
        "_id": USER_ID, "tid": "TEST-TID-104", "oid": "TEST-OID-104",
        "email": "qa.iteration104@test.local", "name": "QA Iteration 104",
        "role": "super_admin", "allowed_pages": [], "created_at": datetime.now(timezone.utc),
        "last_login_at": datetime.now(timezone.utc),
    }, upsert=True)
    db["auth_sessions"].insert_one({"_id": token, "user_id": USER_ID,
                                    "expires_at": datetime.now(timezone.utc) + timedelta(hours=6)})
    open(TOKEN_FILE, "w").write(token)
    print("TOKEN=" + token)


def cleanup():
    print("requests:", db["store_requests"].delete_many({"_id": {"$in": list(IDS.values())}}).deleted_count)
    print("sessions:", db["auth_sessions"].delete_many({"user_id": USER_ID}).deleted_count)
    print("users:", db["auth_users"].delete_many({"_id": USER_ID}).deleted_count)


if "--cleanup" in sys.argv:
    cleanup()
else:
    seed()
client.close()
