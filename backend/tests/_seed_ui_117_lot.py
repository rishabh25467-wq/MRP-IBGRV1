"""Seeds a failed-WIP history doc for a real currently-open lot (iteration_117 UI test)."""
import sys
from datetime import datetime, timezone

import requests
from dotenv import dotenv_values
from pymongo import MongoClient

benv = dotenv_values("/app/backend/.env")
fenv = dotenv_values("/app/frontend/.env")
BASE = (sys.argv[2] if len(sys.argv) > 2 else fenv["REACT_APP_BACKEND_URL"]).rstrip("/")
db = MongoClient(benv["MONGO_URL"])[benv["DB_NAME"]]
token = sys.argv[1]

s = requests.Session()
s.cookies.set("vms_session", token)
r = s.get(f"{BASE}/api/production-confirmation/open-lots", params={"limit": 5}, timeout=180)
print("HTTP", r.status_code)
if r.status_code != 200:
    print(r.text[:300])
    raise SystemExit(1)
data = r.json()
print("keys:", list(data.keys()))
rows = data.get("lots") or data.get("rows") or []
print("count:", len(rows))
if not rows:
    raise SystemExit("no open lots")
lot = rows[0]
print("sample keys:", list(lot.keys()))
lot_id = lot.get("production_lot_id")
db["production_confirmation_history"].delete_many({"production_lot_id": lot_id, "actor": "QA UI 117"})
db["production_confirmation_history"].insert_one({
    "actor": "QA UI 117", "production_lot_id": lot_id,
    "main_output_product": lot.get("main_output_product"),
    "confirmed_quantity": 1, "success": True, "logs": [], "confirmation_finished": True,
    "wip_clearing": {"success": False, "log": "seeded failure for UI retry test"},
    "at": datetime.now(timezone.utc),
})
print("LOT_ID", lot_id, "SITE", lot.get("site_id"))
