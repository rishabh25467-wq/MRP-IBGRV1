"""iteration_132 - independent SAP-side verification that the multi-line STO's
combined Outbound Delivery really carries the 5 structured metadata fields
(written by the new Playwright UI automation, not just as a Note)."""
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")
BASE = os.environ["BYD_ODATA_BASE"]
VHOST = os.environ["BYD_ODATA_VHOST"]
AUTH = (os.environ["SAP_USERNAME"], os.environ["SAP_PASSWORD"])

delivery_ids = sys.argv[1:] or ["P8D1-206"]
for did in delivery_ids:
    params = {"$filter": f"ID eq '{did}'", "$format": "json", "sap-vhost": VHOST}
    r = requests.get(f"{BASE}/OutboundDeliveryCollection", params=params, auth=AUTH,
                     headers={"Accept": "application/json"}, timeout=60)
    print(f"{did}: HTTP {r.status_code}")
    if r.status_code != 200:
        print(r.text[:500])
        continue
    rows = r.json().get("d", {}).get("results", [])
    if not rows:
        print(f"{did}: no rows returned")
        continue
    row = rows[0]
    keys = [k for k in row if k.endswith("_KUT")] or list(row.keys())
    print(f"{did} _KUT fields:", {k: row.get(k) for k in keys})
    print(f"{did} status:", {k: row.get(k) for k in row if "Status" in k})
