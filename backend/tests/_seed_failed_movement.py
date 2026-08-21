"""Temp UI-check seed: one resolved store_request whose component has an
attempted-but-SAP-rejected goods movement. Run with `python _seed_failed_movement.py`
and `python _seed_failed_movement.py clean` to remove."""
import os
import sys
from datetime import datetime, timezone

from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
db = MongoClient(env["MONGO_URL"])[env["DB_NAME"]]
RID = "TEST_UI_FAILED_MOVE"

if len(sys.argv) > 1 and sys.argv[1] == "clean":
    db["store_requests"].delete_one({"_id": RID})
    print("cleaned")
    raise SystemExit

now = datetime.now(timezone.utc)
db["store_requests"].replace_one({"_id": RID}, {
    "_id": RID, "job_id": "TEST_UI_JOB", "production_proposal_id": "TEST_UI_PROP",
    "material_id": "TEST_UI_MAT", "site_id": "P2", "quantity": 1.0, "unit_code": "EA",
    "requester": "TEST_QA_UI", "issue_id": "P2-I999999",
    "target_logistics_area_id": "P2/P2-SFG",
    "components": [{
        "product_id": "TEST_UI_COMP", "description": "ui comp", "unit_of_measure": "KGM",
        "required_qty": 0.01, "available_qty": 10.0,
        "locations": [{"warehouse": "RAW MATERIAL GODOWN-P2", "warehouse_id": "P2/P2-RM", "qty": 10.0, "owner": "RI"}],
        "issued_qty": 0.01, "shortfall": 0.0,
        "issued_from_warehouse": "P2/P2-RM", "issued_from_owner": "RI",
        "goods_movement": {"attempted": True, "ok": False, "external_id": "MOV-UITEST",
                           "error": "SAP rejected the movement: Source logistics area is invalid for external id MOV-UITEST"},
    }],
    "status": "resolved", "store_actor": "QA UI", "store_decision": None,
    "planner_actor": None, "planner_decision": None, "resolution": "full_issue",
    "created_at": now, "updated_at": now, "resolved_at": now,
}, upsert=True)
print("seeded", RID)
