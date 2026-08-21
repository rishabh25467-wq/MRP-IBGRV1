"""Seed pending store_requests for /storeapproval UI testing of the
Goods Movement wiring. `python _seed_gm_request.py [cleanup]`"""
import os
import sys

from dotenv import dotenv_values
from pymongo import MongoClient

sys.path.insert(0, "/app/backend")
import store_approval_service  # noqa: E402

env = dotenv_values("/app/backend/.env")
db = MongoClient(os.environ.get("MONGO_URL") or env["MONGO_URL"])[os.environ.get("DB_NAME") or env["DB_NAME"]]
REQUESTER = "TEST_QA_GM_UI"

if sys.argv[1:] and sys.argv[1] == "cleanup":
    print("deleted", db["store_requests"].delete_many({"requester": REQUESTER}).deleted_count)
    sys.exit(0)

for tag in ("A", "B"):
    doc = store_approval_service.create_request(
        db, f"TEST_JOB_GM_UI_{tag}",
        {"material_id": f"TEST_GM_UI_MAT_{tag}", "site_id": "P2", "quantity": 10.0, "unit_code": "EA"},
        "TEST_PROP_GM_UI",
        [
            {"product_id": "TEST_GM_UI_MULTI", "description": "multi-location comp", "unit_of_measure": "KGM",
             "required_qty": 20.0, "available_qty": 15.0,
             "locations": [
                 {"warehouse": "RAW MATERIAL GODOWN-P2", "stock_status": "Unrestricted", "qty": 10.0, "owner": "RI"},
                 {"warehouse": "SEMI-FINISH GODOWN-P2", "stock_status": "Not Assigned", "qty": 5.0, "owner": "RT"},
             ]},
            {"product_id": "TEST_GM_UI_SINGLE", "description": "single-location comp", "unit_of_measure": "EA",
             "required_qty": 5.0, "available_qty": 5.0,
             "locations": [{"warehouse": "RAW MATERIAL GODOWN-P2", "stock_status": "Unrestricted", "qty": 5.0, "owner": "RI"}]},
        ],
        REQUESTER,
    )
    print(tag, doc["_id"])
