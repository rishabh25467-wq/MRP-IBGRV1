"""Seed a synthetic super_admin session + one TEST STO carrying the new
outbound_delivery_ids field, for iteration_128 frontend verification."""
import sys
from datetime import datetime, timedelta, timezone

from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
client = MongoClient(env["MONGO_URL"])
db = client[env["DB_NAME"]]

TOKEN = "TEST_iter128_sto_ui_session"
USER_ID = "testtid:testoid-iter128"
STO_ID = "TEST-STO-IDS-128"

if len(sys.argv) > 1 and sys.argv[1] == "cleanup":
    db["auth_sessions"].delete_one({"_id": TOKEN})
    db["auth_users"].delete_one({"_id": USER_ID})
    db["stock_transfer_orders"].delete_one({"_id": STO_ID})
    print("cleaned up")
    raise SystemExit(0)

db["auth_users"].replace_one({"_id": USER_ID}, {
    "_id": USER_ID, "tid": "testtid", "oid": "testoid-iter128",
    "email": "qa.iter128@example.test", "name": "QA 128",
    "role": "super_admin", "allowed_pages": [], "bound_sites": [],
    "created_at": datetime.now(timezone.utc), "last_login_at": datetime.now(timezone.utc),
}, upsert=True)
db["auth_sessions"].replace_one({"_id": TOKEN}, {
    "_id": TOKEN, "user_id": USER_ID,
    "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
}, upsert=True)

legacy = db["stock_transfer_orders"].find_one({"_id": "STO-000045"}) or db["stock_transfer_orders"].find_one({"gi_status": "posted"})
doc = dict(legacy or {})
doc.update({
    "_id": STO_ID,
    "status": "created_in_sap",
    "gi_status": "posted",
    "gi_error": None,
    "outbound_delivery_ids": ["P8D1-185", "P8D1-186"],
    "outbound_delivery_object_id": "OLD-OBJ-ID-XYZ",
    "created_at": datetime.now(timezone.utc),
})
db["stock_transfer_orders"].replace_one({"_id": STO_ID}, doc, upsert=True)
print("seeded", TOKEN, STO_ID)
for sid in ("STO-000042", "STO-000045"):
    d = db["stock_transfer_orders"].find_one({"_id": sid}, {"gi_status": 1, "outbound_delivery_ids": 1, "outbound_delivery_object_id": 1})
    print(sid, d)
client.close()
