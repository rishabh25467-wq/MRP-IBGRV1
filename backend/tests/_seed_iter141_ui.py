"""Seeds/removes the synthetic internal sessions used for iteration_141 UI testing.

python _seed_iter141_ui.py seed | clean
"""
import sys
from datetime import datetime, timedelta, timezone

from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
db = MongoClient(env["MONGO_URL"])[env["DB_NAME"]]

SESSIONS = [
    ("zz141uisuper", "zz141ui-tid:super-oid", "zz141ui.super@internal.test", "super_admin", []),
    ("zz141uiblocked", "zz141ui-tid:blocked-oid", "zz141ui.blocked@internal.test", "user", ["P3"]),
]
RI_SHIP = "ZZ141R"


def seed():
    now = datetime.now(timezone.utc)
    for sid, uid, email, role, bound in SESSIONS:
        db["auth_users"].update_one({"_id": uid}, {"$set": {
            "tid": "zz141ui-tid", "oid": uid.split(":")[1], "email": email, "name": "ZZ141 UI Tester",
            "role": role, "allowed_pages": ["supplier_portal_admin"], "bound_sites": bound,
        }}, upsert=True)
        db["auth_sessions"].replace_one({"_id": sid}, {
            "_id": sid, "user_id": uid, "expires_at": now + timedelta(days=1)}, upsert=True)
    db["supplier_portal_shipments"].replace_one({"_id": RI_SHIP}, {
        "_id": RI_SHIP, "account_id": "zz-141-ui", "vendor_code": "H1330",
        "company_name": "TEST_ENTITY_141_UI", "status": "in_transit",
        "created_at": now, "updated_at": now,
        "items": [{"po_number": "ZZPO141U", "item_number": "10", "product_id": "ZZNOTREAL141",
                   "description": "TEST_ UI item", "po_qty": 10, "already_shipped_qty": 0,
                   "ship_qty": 5, "unit_of_measure": "EA", "buyer_code": "RI"}],
    }, upsert=True)
    print("seeded", [s[0] for s in SESSIONS], RI_SHIP)


def clean():
    db["auth_sessions"].delete_many({"_id": {"$in": [s[0] for s in SESSIONS]}})
    db["auth_users"].delete_many({"_id": {"$in": [s[1] for s in SESSIONS]}})
    r = db["supplier_portal_shipments"].delete_many({"company_name": {"$in": ["TEST_ENTITY_141_UI", "TEST_ENTITY_141"]}})
    print("cleaned sessions/users, shipments removed:", r.deleted_count)
    print("residue sessions:", db["auth_sessions"].count_documents({"_id": {"$regex": "^zz141"}}))
    print("residue po_cache:", db["supplier_portal_po_cache"].count_documents({"po_number": {"$regex": "^ZZ"}}))


if __name__ == "__main__":
    (seed if sys.argv[1] == "seed" else clean)()
