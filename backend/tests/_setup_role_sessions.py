"""Creates synthetic Entra sessions for UI role testing. Prints token per role.
Run with --cleanup to remove them."""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
client = MongoClient(env["MONGO_URL"])
db = client[env["DB_NAME"]]

SPECS = {
    "ui_store_user": ("user", ["store_approval"]),
    "ui_plain_user": ("user", []),
    "ui_admin": ("admin", []),
    "ui_super_admin": ("super_admin", []),
}
now = datetime.now(timezone.utc)
for key, (role, pages) in SPECS.items():
    user_id = f"TESTtid:TESToid-{key}"
    token = "TEST_uiauth_" + uuid.uuid5(uuid.NAMESPACE_DNS, user_id).hex
    if "--cleanup" in sys.argv:
        db["auth_sessions"].delete_one({"_id": token})
        db["auth_users"].delete_one({"_id": user_id})
        print(f"cleaned {key}")
        continue
    db["auth_users"].replace_one(
        {"_id": user_id},
        {"_id": user_id, "tid": "TESTtid", "oid": f"TESToid-{key}",
         "email": f"TEST_{key}@example.test", "name": f"TEST QA {key}",
         "role": role, "allowed_pages": pages,
         "created_at": now, "last_login_at": now},
        upsert=True,
    )
    db["auth_sessions"].replace_one(
        {"_id": token},
        {"_id": token, "user_id": user_id, "expires_at": now + timedelta(days=2)},
        upsert=True,
    )
    print(f"{key}={token}")
