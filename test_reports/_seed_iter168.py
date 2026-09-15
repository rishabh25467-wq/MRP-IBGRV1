"""Seed synthetic Entra ID users + sessions for iteration_168 nav tests (Operation dropdown removal)."""
import os, secrets, json
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from pymongo import MongoClient

client = MongoClient(os.environ["MONGO_URL"])
db = client[os.environ["DB_NAME"]]
now = datetime.now(timezone.utc)

SCENARIOS = {
    "test_only": {
        "user_id": "iter168-tid:iter168-test-only",
        "email": "iter168-test-only@example.com",
        "name": "Iter168 Test-Only User",
        "role": "user",
        "allowed_pages": ["production_confirmation_test"],
    },
    "pc_only": {
        "user_id": "iter168-tid:iter168-pc-only",
        "email": "iter168-pc-only@example.com",
        "name": "Iter168 PC-Only User",
        "role": "user",
        "allowed_pages": ["production_confirmation"],
    },
    "both": {
        "user_id": "iter168-tid:iter168-both",
        "email": "iter168-both@example.com",
        "name": "Iter168 Both User",
        "role": "user",
        "allowed_pages": ["production_confirmation", "production_confirmation_test"],
    },
}

result = {}
for key, s in SCENARIOS.items():
    tid, oid = s["user_id"].split(":", 1)
    db["auth_users"].update_one(
        {"_id": s["user_id"]},
        {"$set": {"tid": tid, "oid": oid, "email": s["email"], "name": s["name"],
                  "role": s["role"], "allowed_pages": s["allowed_pages"],
                  "bound_sites": [], "last_login_at": now},
         "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    db["auth_sessions"].delete_many({"user_id": s["user_id"]})
    session_id = secrets.token_urlsafe(32)
    db["auth_sessions"].insert_one({
        "_id": session_id, "user_id": s["user_id"],
        "expires_at": now + timedelta(days=1),
        "created_at": now,
    })
    result[key] = {"user_id": s["user_id"], "session_id": session_id, "email": s["email"]}

with open("/tmp/iter168_sessions.json", "w") as f:
    json.dump(result, f, indent=2)
print(json.dumps(result, indent=2))
