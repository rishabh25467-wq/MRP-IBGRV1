"""Seeds everything the iteration_117 frontend Playwright run needs:
  - a synthetic Entra session token (printed)
  - a failed wip_clearing history doc for a REAL currently-open lot (printed)
  - a failed create-and-release job with reason=pipeline_error + proposal id (printed)
  - a failed job with reason=sfg_shortage (regression, printed)
"""
import os
import secrets
from datetime import datetime, timedelta, timezone

import requests
from dotenv import dotenv_values
from pymongo import MongoClient

benv = dotenv_values("/app/backend/.env")
fenv = dotenv_values("/app/frontend/.env")
BASE = fenv["REACT_APP_BACKEND_URL"].rstrip("/")
db = MongoClient(benv["MONGO_URL"])[benv["DB_NAME"]]

oid = "TEST-QA-OID-117-UI"
user_id = f"2a94b71e-cd64-4c3d-aede-24eb89ab5fad:{oid}"
token = "TESTQAUI" + secrets.token_urlsafe(24)
db.auth_users.replace_one({"_id": user_id}, {
    "_id": user_id, "tid": user_id.split(":")[0], "oid": oid,
    "email": "qa117ui@rampgroup.co.in", "name": "QA UI 117", "role": "super_admin",
    "allowed_pages": [], "created_at": datetime.now(timezone.utc),
    "last_login_at": datetime.now(timezone.utc)}, upsert=True)
db.auth_sessions.insert_one({"_id": token, "user_id": user_id,
                             "expires_at": datetime.now(timezone.utc) + timedelta(hours=4)})

s = requests.Session()
s.cookies.set("vms_session", token)
r = s.get(f"{BASE}/api/production-confirmation/open-lots", params={"limit": 5}, timeout=180)
lot = None
if r.status_code == 200:
    rows = r.json().get("lots") or r.json().get("rows") or []
    if rows:
        lot = rows[0]
else:
    print("OPEN_LOTS_HTTP", r.status_code, r.text[:200])

if lot:
    lot_id = lot.get("production_lot_id")
    db["production_confirmation_history"].delete_many({"production_lot_id": lot_id, "actor": "QA UI 117"})
    db["production_confirmation_history"].insert_one({
        "actor": "QA UI 117", "production_lot_id": lot_id,
        "main_output_product": lot.get("main_output_product"),
        "confirmed_quantity": 1, "success": True, "logs": [], "confirmation_finished": True,
        "wip_clearing": {"success": False, "log": "seeded failure for UI retry test"},
        "at": datetime.now(timezone.utc),
    })
    print("LOT_ID", lot_id)
    print("LOT_SITE", lot.get("site_id"))
else:
    print("LOT_ID", None)

pipe_job = "TEST_QA_UIJOB_117_" + secrets.token_hex(4)
db.background_jobs.insert_one({
    "_id": pipe_job, "created_at": datetime.now(timezone.utc), "status": "failed",
    "error": "SAP Release step failed: simulated pipeline exception (QA 117)",
    "result": {"reason": "pipeline_error", "production_proposal_id": "TEST_QA_PROP_UI_117",
               "production_order_id": None},
    "payload_snapshot": {"material_id": "P26663", "site_id": "P1", "quantity": 5,
                         "unit_code": "EA", "actor": "QA UI 117"},
})
sfg_job = "TEST_QA_UIJOB_SFG_117_" + secrets.token_hex(4)
db.background_jobs.insert_one({
    "_id": sfg_job, "created_at": datetime.now(timezone.utc), "status": "failed",
    "error": "Short sub-assembly components at P1",
    "result": {"reason": "sfg_shortage", "site_id": "P1", "short_components": [
        {"product_id": "TEST_QA_SFG1", "description": "QA Sub Assy", "unit_of_measure": "EA",
         "required_qty": 10, "available_qty": 2}]},
})
print("TOKEN", token)
print("PIPELINE_JOB", pipe_job)
print("SFG_JOB", sfg_job)
