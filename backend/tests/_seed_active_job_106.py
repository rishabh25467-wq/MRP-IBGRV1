"""Iteration 106 helper: creates (a) a synthetic auth session for the
Production Confirmation page and (b) a synthetic in-flight
create-and-release job doc in background_jobs (NO SAP write at all) so the
Active Orders row / Stop button / reload-hydration fix can be tested.

Usage:
  python _seed_active_job_106.py seed      -> prints "TOKEN JOB_ID"
  python _seed_active_job_106.py cancel JOB_ID  -> flips job to cancelled
  python _seed_active_job_106.py status JOB_ID  -> prints job doc
  python _seed_active_job_106.py cleanup JOB_ID -> deletes session/user/job
"""
import os
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
client = MongoClient(os.environ.get("MONGO_URL") or env.get("MONGO_URL"))
db = client[os.environ.get("DB_NAME") or env.get("DB_NAME")]

USER_ID = "2a94b71e-cd64-4c3d-aede-24eb89ab5fad:TEST-QA-106"
cmd = sys.argv[1]

if cmd == "seed":
    token = "TESTQA106" + secrets.token_urlsafe(24)
    db.auth_users.replace_one({"_id": USER_ID}, {
        "_id": USER_ID,
        "tid": "2a94b71e-cd64-4c3d-aede-24eb89ab5fad",
        "oid": "TEST-QA-106",
        "email": "qa106@rampgroup.co.in",
        "name": "QA Tester 106",
        "role": "user",
        "allowed_pages": ["production_confirmation"],
        "created_at": datetime.now(timezone.utc),
        "last_login_at": datetime.now(timezone.utc),
    }, upsert=True)
    db.auth_sessions.insert_one({"_id": token, "user_id": USER_ID,
                                 "expires_at": datetime.now(timezone.utc) + timedelta(hours=4)})
    job_id = str(uuid.uuid4())
    db.background_jobs.insert_one({
        "_id": job_id,
        "created_at": datetime.now(timezone.utc),
        "status": "checking_stock",
        "result": None,
        "error": None,
        "payload_snapshot": {"material_id": "TEST_MAZ42117269", "site_id": "P2",
                             "quantity": 1, "unit_code": "EA", "actor": "QA106"},
    })
    print(f"{token} {job_id}")
elif cmd == "cancel":
    db.background_jobs.update_one({"_id": sys.argv[2]},
                                  {"$set": {"status": "cancelled",
                                            "result": {"note": "TEST_ Order creation stopped by the user"}}})
    print("cancelled")
elif cmd == "status":
    print(db.background_jobs.find_one({"_id": sys.argv[2]}))
elif cmd == "cleanup":
    db.auth_sessions.delete_many({"user_id": USER_ID})
    db.auth_users.delete_many({"_id": USER_ID})
    if len(sys.argv) > 2:
        db.background_jobs.delete_many({"_id": sys.argv[2]})
    print("cleaned")
