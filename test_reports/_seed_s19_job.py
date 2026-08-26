"""Re-seed only the synthetic waiting_for_order job (kept OUT of /app/backend
so writing it never triggers uvicorn's watchfiles reload, which would flip the
job to 'failed' via job_store.recover_orphaned_jobs)."""
import os
import secrets
from datetime import datetime, timezone

from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
client = MongoClient(os.environ.get("MONGO_URL") or env.get("MONGO_URL"))
db = client[os.environ.get("DB_NAME") or env.get("DB_NAME")]

JOB_ID = "TEST-JOB-S19-" + secrets.token_hex(4)
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
print(JOB_ID)
