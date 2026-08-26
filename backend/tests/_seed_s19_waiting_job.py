"""Seeds a synthetic auth session + a synthetic 'waiting_for_order' background
job (NO SAP interaction - the status endpoint only reads Mongo) so the UI's
Active Orders trigger-detail line and 'Retry Now' button can be verified.
Prints: <session_token> <job_id>"""
import os
import secrets
from datetime import datetime, timedelta, timezone

from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
client = MongoClient(os.environ.get("MONGO_URL") or env.get("MONGO_URL"))
db = client[os.environ.get("DB_NAME") or env.get("DB_NAME")]

USER_ID = "2a94b71e-cd64-4c3d-aede-24eb89ab5fad:TEST-QA-OID-UI19"
TOKEN = "TESTQAUI19" + secrets.token_urlsafe(20)
JOB_ID = "TEST-JOB-S19-" + secrets.token_hex(4)

db.auth_users.replace_one({"_id": USER_ID}, {
    "_id": USER_ID, "tid": "2a94b71e-cd64-4c3d-aede-24eb89ab5fad", "oid": "TEST-QA-OID-UI19",
    "email": "qa.ui19@rampgroup.co.in", "name": "QA UI19", "role": "super_admin",
    "allowed_pages": ["production_confirmation"],
    "created_at": datetime.now(timezone.utc), "last_login_at": datetime.now(timezone.utc),
}, upsert=True)
db.auth_sessions.replace_one({"_id": TOKEN}, {
    "_id": TOKEN, "user_id": USER_ID, "expires_at": datetime.now(timezone.utc) + timedelta(hours=4),
}, upsert=True)

db["background_jobs"].replace_one({"_id": JOB_ID}, {
    "_id": JOB_ID,
    "created_at": datetime.now(timezone.utc),
    "status": "waiting_for_order",
    "result": None,
    "error": None,
    "production_proposal_id": "TEST-PROP-9999",
    "release_trigger_count": 3,
    "last_release_trigger_ok": False,
    "last_release_trigger_error": "TEST simulated SAP verification hiccup",
}, upsert=True)

print(TOKEN, JOB_ID)
