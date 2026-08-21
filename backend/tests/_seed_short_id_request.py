"""Seed one short-ID (SR-XXXXXX) store request via the real service function
so the /storeapproval UI can be verified with a NEW-format ID. Prints the id."""
import os
import sys

from dotenv import dotenv_values
from pymongo import MongoClient

sys.path.insert(0, "/app/backend")
import store_approval_service  # noqa: E402

env = dotenv_values("/app/backend/.env")
db = MongoClient(os.environ.get("MONGO_URL") or env["MONGO_URL"])[os.environ.get("DB_NAME") or env["DB_NAME"]]

if sys.argv[1:] and sys.argv[1] == "cleanup":
    r = db["store_requests"].delete_many({"requester": "TEST_QA_UI"})
    print("deleted", r.deleted_count)
else:
    doc = store_approval_service.create_request(
        db,
        "TEST_JOB_UI",
        {"material_id": "TEST_UI_MAT", "site_id": "P2", "quantity": 25.0, "unit_code": "EA"},
        "TEST_PROP_UI",
        [{
            "product_id": "TEST_UI_COMP", "description": "TEST UI component", "unit_of_measure": "KGM",
            "required_qty": 40.0, "available_qty": 12.0,
            "locations": [{"warehouse": "RAW MATERIAL GODOWN-P2", "stock_status": "Unrestricted", "qty": 12.0}],
        }],
        "TEST_QA_UI",
    )
    print(doc["_id"])
