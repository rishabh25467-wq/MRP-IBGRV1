"""Creates a synthetic super_admin auth session in MongoDB for testing the
Production Confirmation page (app uses Entra ID SSO which can't be automated).
Prints the session token."""
import os
import secrets
from datetime import datetime, timedelta, timezone

from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
mongo_url = os.environ.get("MONGO_URL") or env.get("MONGO_URL")
db_name = os.environ.get("DB_NAME") or env.get("DB_NAME")
client = MongoClient(mongo_url)
db = client[db_name]

USER_ID = "2a94b71e-cd64-4c3d-aede-24eb89ab5fad:TEST-QA-OID"
TOKEN = "TESTQA" + secrets.token_urlsafe(24)

db.auth_users.replace_one(
    {"_id": USER_ID},
    {
        "_id": USER_ID,
        "tid": "2a94b71e-cd64-4c3d-aede-24eb89ab5fad",
        "oid": "TEST-QA-OID",
        "email": "qa.tester@rampgroup.co.in",
        "name": "QA Tester",
        "role": "super_admin",
        "allowed_pages": [],
        "created_at": datetime.now(timezone.utc),
        "last_login_at": datetime.now(timezone.utc),
    },
    upsert=True,
)
db.auth_sessions.insert_one(
    {"_id": TOKEN, "user_id": USER_ID, "expires_at": datetime.now(timezone.utc) + timedelta(hours=8)}
)
print(TOKEN)
