"""End-to-end probe: Delivery Note payload for a synthetic P2W ship-from STO
must now carry GSTIN/PAN (Aug 27 2026 bug). Read-only apart from the temp STO."""
import os
from datetime import datetime, timezone

import requests
from dotenv import dotenv_values
from pymongo import MongoClient

benv = dotenv_values("/app/backend/.env")
fenv = dotenv_values("/app/frontend/.env")
BASE = fenv["REACT_APP_BACKEND_URL"].rstrip("/")
c = MongoClient(benv["MONGO_URL"])
db = c[benv["DB_NAME"]]

USER_ID = "testtid:testoid-p2wprobe"
TOKEN = "TEST_p2w_probe_session"
db["auth_users"].replace_one({"_id": USER_ID}, {
    "_id": USER_ID, "tid": "testtid", "oid": "testoid-p2wprobe",
    "email": "qa.p2w@example.test", "name": "QA P2W", "role": "super_admin",
    "allowed_pages": [], "bound_sites": [],
}, upsert=True)
from datetime import timedelta
db["auth_sessions"].replace_one({"_id": TOKEN}, {
    "_id": TOKEN, "user_id": USER_ID, "expires_at": datetime.now(timezone.utc) + timedelta(hours=2),
}, upsert=True)

STO = "TEST_STO_P2W_PROBE"
db["stock_transfer_orders"].replace_one({"_id": STO}, {
    "_id": STO, "status": "created_in_sap", "gi_status": "posted",
    "ship_from_site_id": "P2W", "ship_to_site_id": "P1",
    "sap_order_id": "TESTP2W", "sap_order_uuid": "00000000-0000-0000-0000-000000000000",
    "created_at": datetime.now(timezone.utc),
    "items": [{"product_id": "HRPIPE3329", "requested_qty": 5.0, "source_warehouse_id": "RM01",
               "description": "HR PIPE", "rate": 100.0, "hsn_code": "7306", "unit_of_measure": "KG"}],
}, upsert=True)

try:
    r = requests.get(f"{BASE}/api/stock-transfer/{STO}/delivery-note", cookies={"vms_session": TOKEN}, timeout=120)
    print("HTTP", r.status_code)
    if r.status_code == 200:
        d = r.json()
        for key in ("ship_from_company", "ship_to_company", "serial_number"):
            if key in d:
                print(key, "->", d[key])
        print("TOP KEYS:", list(d))
    else:
        print(r.text[:500])
finally:
    db["stock_transfer_orders"].delete_one({"_id": STO})
    db["auth_sessions"].delete_one({"_id": TOKEN})
    db["auth_users"].delete_one({"_id": USER_ID})
    print("cleaned up")
