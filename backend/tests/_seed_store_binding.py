"""Seeds synthetic Entra sessions for Store Binding tests. Prints token per role.
Run with --cleanup to remove them. Also prints known sites + sample pending requests."""
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
client = MongoClient(env["MONGO_URL"])
db = client[env["DB_NAME"]]

PAGES = ["store_approval", "inventory"]
SPECS = {
    # key: (role, allowed_pages, bound_sites)
    "sb_super_admin": ("super_admin", [], []),
    "sb_admin": ("admin", [], []),
    "sb_user_unbound": ("user", PAGES, []),
    "sb_user_p1": ("user", PAGES, ["P1"]),
    "sb_bind_target": ("user", PAGES, []),
}
now = datetime.now(timezone.utc)
out = {}
for key, (role, pages, bound) in SPECS.items():
    user_id = f"TESTtid:TESToid-{key}"
    token = "TEST_sb_" + uuid.uuid5(uuid.NAMESPACE_DNS, user_id).hex
    if "--cleanup" in sys.argv:
        db["auth_sessions"].delete_one({"_id": token})
        db["auth_users"].delete_one({"_id": user_id})
        continue
    db["auth_users"].replace_one(
        {"_id": user_id},
        {"_id": user_id, "tid": "TESTtid", "oid": f"TESToid-{key}",
         "email": f"TEST_{key}@example.test", "name": f"TEST QA {key}",
         "role": role, "allowed_pages": pages, "bound_sites": bound,
         "created_at": now, "last_login_at": now},
        upsert=True,
    )
    db["auth_sessions"].replace_one(
        {"_id": token},
        {"_id": token, "user_id": user_id, "expires_at": now + timedelta(days=2)},
        upsert=True,
    )
    out[key] = {"token": token, "user_id": user_id}

if "--cleanup" in sys.argv:
    print("cleaned")
else:
    sites = sorted({d.get("site_id") for d in db["inventory_cache"].find({}, {"site_id": 1}) if d.get("site_id")})
    reqs = [{"id": d["_id"], "site_id": d.get("site_id"), "status": d.get("status")}
            for d in db["store_requests"].find({}, {"site_id": 1, "status": 1}).limit(2000)]
    print(json.dumps({"sessions": out, "sites": sites,
                      "requests_by_site": reqs[:200]}, indent=1, default=str))
