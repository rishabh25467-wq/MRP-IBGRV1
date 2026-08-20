"""Creates a synthetic Entra-SSO session in Mongo for testing (no real MS login
possible from an agent). Prints the session token. Delete with --cleanup."""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv("/app/backend/.env")
client = MongoClient(os.environ["MONGO_URL"])
db = client[os.environ["DB_NAME"]]

WORKER = os.environ.get("PYTEST_XDIST_WORKER", "solo")
USER_ID = f"testtid:testoid-scrapcalc-{WORKER}"
TOKEN = "TEST_scrapcalc_session_token_" + uuid.uuid5(uuid.NAMESPACE_DNS, USER_ID).hex[:12]

if "--cleanup" in sys.argv:
    db["auth_sessions"].delete_one({"_id": TOKEN})
    db["auth_users"].delete_one({"_id": USER_ID})
    print("cleaned")
else:
    db["auth_users"].replace_one(
        {"_id": USER_ID},
        {"_id": USER_ID, "tid": "testtid", "oid": "testoid-scrapcalc",
         "email": "qa.scrapcalc@example.test", "name": "QA ScrapCalc",
         "role": "super_admin", "allowed_pages": [],
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
