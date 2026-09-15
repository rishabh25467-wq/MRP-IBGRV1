"""Seed synthetic super_admin session for iteration_171 admin dropdown visibility test."""
import os, secrets, json
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from pymongo import MongoClient

client = MongoClient(os.environ["MONGO_URL"])
db = client[os.environ["DB_NAME"]]
now = datetime.now(timezone.utc)

USER_ID = "iter171-tid:iter171-super-admin"
tid, oid = USER_ID.split(":", 1)
EMAIL = "iter171-header@example.com"

db["auth_users"].update_one(
    {"_id": USER_ID},
    {"$set": {"tid": tid, "oid": oid, "email": EMAIL, "name": "Ankit Sharma",
              "role": "super_admin", "allowed_pages": [], "bound_sites": [],
              "last_login_at": now},
     "$setOnInsert": {"created_at": now}},
    upsert=True,
)
db["auth_sessions"].delete_many({"user_id": USER_ID})
session_id = secrets.token_urlsafe(32)
db["auth_sessions"].insert_one({
    "_id": session_id, "user_id": USER_ID,
    "expires_at": now + timedelta(days=1),
    "created_at": now,
})
result = {"user_id": USER_ID, "session_id": session_id, "email": EMAIL}
with open("/tmp/iter171_session.json", "w") as f:
    json.dump(result, f, indent=2)
print(json.dumps(result, indent=2))
