"""Seed/cleanup synthetic store_requests for UI testing of the Issue ID,
Target Bin dropdown and Movement History tab. Usage:
    python _seed_ui_issueid.py seed
    python _seed_ui_issueid.py clean
"""
import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
client = MongoClient(os.environ.get("MONGO_URL") or env["MONGO_URL"])
db = client[os.environ.get("DB_NAME") or env["DB_NAME"]]
TAG = "TESTUI"
SITE_A = "QAUI1"  # has bins on file already
SITE_B = "QAUI2"  # no bins yet


def doc(_id, site, status, comp, requester, bin_id=None, issue_id=None, actor=None, minutes=5):
    now = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    d = {
        "_id": _id, "job_id": f"{_id}_job", "production_proposal_id": f"{TAG}_PROP",
        "material_id": f"{TAG}_MAT_{site}", "site_id": site, "quantity": 3.0, "unit_code": "EA",
        "requester": requester, "components": [comp], "status": status,
        "store_actor": actor, "store_decision": None, "planner_actor": None,
        "planner_decision": None, "resolution": None,
        "created_at": now, "updated_at": now, "resolved_at": now if status == "resolved" else None,
    }
    if bin_id:
        d["target_logistics_area_id"] = bin_id
    if issue_id:
        d["issue_id"] = issue_id
    return d


def comp(pid, desc, required, issued=None, wh=None, owner=None, movement=None):
    return {
        "product_id": pid, "description": desc, "unit_of_measure": "KG",
        "required_qty": required, "available_qty": required,
        "locations": [{"warehouse": "QAUI-WH-MAIN", "stock_status": "Unrestricted", "qty": required, "owner": "RI"}],
        "issued_qty": issued, "shortfall": None if issued is None else max(0.0, required - issued),
        "issued_from_warehouse": wh, "issued_from_owner": owner, "goods_movement": movement,
    }


DRY = {"attempted": True, "ok": True, "dry_run": True, "external_id": "QAUI-DRY-1"}

DOCS = [
    # pending at SITE_A (bins exist -> dropdown)
    doc(f"{TAG}_PEND_A", SITE_A, "pending", comp(f"{TAG}_COMP_A", "QA UI comp alpha", 6.0), "TESTUI_Ankit"),
    # pending at SITE_B (no bins -> free text auto)
    doc(f"{TAG}_PEND_B", SITE_B, "pending", comp(f"{TAG}_COMP_B", "QA UI comp bravo", 4.0), "TESTUI_Bharat"),
    # historic resolved at SITE_A providing the bins
    doc(f"{TAG}_RES_1", SITE_A, "resolved", comp(f"{TAG}_COMP_A", "QA UI comp alpha", 6.0, 6.0, "QAUI-WH-MAIN", "RI", DRY),
        "TESTUI_Ankit", bin_id="QAUI-WIP", issue_id=f"{SITE_A}-I000901", actor="TESTUI_Storekeeper", minutes=60),
    doc(f"{TAG}_RES_2", SITE_A, "resolved", comp(f"{TAG}_COMP_C", "QA UI comp charlie", 2.0, 1.0, "QAUI-WH-MAIN", "RI", None),
        "TESTUI_Chetan", bin_id="QAUI-STAGE", issue_id=f"{SITE_A}-I000902", actor="TESTUI_OtherKeeper", minutes=90),
]


if sys.argv[1] == "seed":
    db["store_requests"].delete_many({"_id": {"$regex": f"^{TAG}_"}})
    db["store_requests"].insert_many(DOCS)
    print("seeded", [d["_id"] for d in DOCS])
else:
    print(db["store_requests"].delete_many({"_id": {"$regex": f"^{TAG}_"}}).deleted_count, "deleted")
    print(db["store_request_counters"].delete_many({"_id": {"$in": [SITE_A, SITE_B, f"{SITE_A}:issue", f"{SITE_B}:issue"]}}).deleted_count, "counters deleted")
