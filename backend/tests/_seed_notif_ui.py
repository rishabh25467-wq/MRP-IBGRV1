"""Seeds UI-test sessions (role=user w/ inventory access, role=admin) and one
synthetic unresolved admin notification using the SAFE product/site pair
(P27175/P1) so the Action Needed panel can be exercised end to end.
Run with --cleanup to remove everything."""
import sys
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
db = MongoClient(env["MONGO_URL"])[env["DB_NAME"]]
now = datetime.now(timezone.utc)

SPECS = {
    "notifui_user": ("user", ["inventory"]),
    "notifui_admin": ("admin", []),
}
NOTIF_ID = "TESTNOTIF-p27175-p1"
cleanup = "--cleanup" in sys.argv

for key, (role, pages) in SPECS.items():
    user_id = f"TESTtid:TESToid-{key}"
    token = "TEST_uiauth_" + uuid.uuid5(uuid.NAMESPACE_DNS, user_id).hex
    if cleanup:
        db["auth_sessions"].delete_one({"_id": token})
        db["auth_users"].delete_one({"_id": user_id})
        print(f"cleaned {key}")
        continue
    db["auth_users"].replace_one({"_id": user_id}, {
        "_id": user_id, "tid": "TESTtid", "oid": f"TESToid-{key}",
        "email": f"TEST_{key}@example.test", "name": f"TEST QA {key}",
        "role": role, "allowed_pages": pages, "created_at": now, "last_login_at": now,
    }, upsert=True)
    db["auth_sessions"].replace_one({"_id": token}, {
        "_id": token, "user_id": user_id, "expires_at": now + timedelta(days=1),
    }, upsert=True)
    print(f"{key}={token}")

if cleanup:
    db["admin_notifications"].delete_one({"_id": NOTIF_ID})
    print("cleaned notification")
else:
    db["admin_notifications"].replace_one({"_id": NOTIF_ID}, {
        "_id": NOTIF_ID, "type": "missing_planning_data",
        "product_id": "P27175", "site_id": "P1", "resolved": False,
        "sto_id": "STO-000021",
        "message": "No valid planning data exists for product P27175 in site P1",
        "created_at": now,
    }, upsert=True)
    print(f"notification={NOTIF_ID}")
