"""iteration_101: creates a synthetic Entra-SSO session for UI testing of
/production-confirmation, prints the session token. Delete with --cleanup."""
import secrets
import sys
from datetime import datetime, timedelta, timezone

from dotenv import dotenv_values
from pymongo import MongoClient

be = dotenv_values("/app/backend/.env")
client = MongoClient(be["MONGO_URL"])
db = client[be["DB_NAME"]]

USER_ID = "TEST-TID-101:TEST-OID-101"
TOKEN = "TESTSESSION101" + secrets.token_hex(16)

if "--cleanup" in sys.argv:
    print("deleted users:", db["auth_users"].delete_many({"_id": USER_ID}).deleted_count)
    print("deleted sessions:", db["auth_sessions"].delete_many({"user_id": USER_ID}).deleted_count)
else:
    now = datetime.now(timezone.utc)
    db["auth_users"].replace_one({"_id": USER_ID}, {
        "_id": USER_ID, "tid": "TEST-TID-101", "oid": "TEST-OID-101",
        "email": "qa.iteration101@test.local", "name": "QA Iteration 101",
        "role": "super_admin", "allowed_pages": ["production_confirmation"],
        "created_at": now, "last_login_at": now,
    }, upsert=True)
    db["auth_sessions"].insert_one({"_id": TOKEN, "user_id": USER_ID, "expires_at": now + timedelta(hours=6)})
    print("TOKEN=" + TOKEN)
client.close()
