"""Synthetic Entra-SSO session for Stock Transfer page testing. Prints token. --cleanup to remove."""
import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv("/app/backend/.env")
client = MongoClient(os.environ["MONGO_URL"])
db = client[os.environ["DB_NAME"]]

WORKER = os.environ.get("PYTEST_XDIST_WORKER", "solo")
USER_ID = f"testtid:testoid-sto-{WORKER}"
TOKEN = f"TEST_sto_session_token_{WORKER}"

if "--cleanup" in sys.argv:
    db["auth_sessions"].delete_one({"_id": TOKEN})
    db["auth_users"].delete_one({"_id": USER_ID})
    print("cleaned")
else:
    db["auth_users"].replace_one(
        {"_id": USER_ID},
        {"_id": USER_ID, "tid": "testtid", "oid": f"testoid-sto-{WORKER}",
         "email": "qa.sto@example.test", "name": "QA STO",
         "role": "user", "allowed_pages": ["inventory", "stock_transfer"],
         "created_at": datetime.now(timezone.utc), "last_login_at": datetime.now(timezone.utc)},
        upsert=True,
    )
    db["auth_sessions"].replace_one(
        {"_id": TOKEN},
        {"_id": TOKEN, "user_id": USER_ID,
         "expires_at": datetime.now(timezone.utc) + timedelta(days=1)},
        upsert=True,
    )
    print(TOKEN)
