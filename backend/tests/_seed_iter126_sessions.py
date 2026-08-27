"""Seed 2 temporary synthetic sessions for iteration 126 UI checks.
Run with `python _seed_iter126_sessions.py` (creates) or `... delete`."""
import sys
from datetime import datetime, timedelta, timezone

from dotenv import dotenv_values
from pymongo import MongoClient

e = dotenv_values("/app/backend/.env")
db = MongoClient(e["MONGO_URL"])[e["DB_NAME"]]

USERS = [
    ("iter126ui-tid:noperm", "TEST_iter126_noperm@internal.test", "user", ["bom_explorer"], "ITER126UI_NOPERM_TOKEN"),
    ("iter126ui-tid:admin", "TEST_iter126_admin@internal.test", "super_admin", [], "ITER126UI_ADMIN_TOKEN"),
]

if len(sys.argv) > 1 and sys.argv[1] == "delete":
    for uid, _, _, _, token in USERS:
        db["auth_sessions"].delete_one({"_id": token})
        db["auth_users"].delete_one({"_id": uid})
    print("deleted")
else:
    for uid, email, role, pages, token in USERS:
        tid, oid = uid.split(":")
        db["auth_users"].update_one({"_id": uid}, {"$set": {
            "tid": tid, "oid": oid, "email": email, "name": email, "role": role,
            "allowed_pages": pages, "bound_sites": [],
        }}, upsert=True)
        db["auth_sessions"].update_one({"_id": token}, {"$set": {
            "user_id": uid, "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
        }}, upsert=True)
        print("seeded", email, token)
