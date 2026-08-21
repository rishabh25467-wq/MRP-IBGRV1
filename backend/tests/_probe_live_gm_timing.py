"""One-off probe (iteration_101): measures how long the LIVE goods-movement
path takes end-to-end through the running server and captures the exact
response shape, to explain the 502 seen through the public ingress.
Uses a non-existent product so SAP cannot actually move any stock.
"""
import json
import os
import sys
import time

import requests
from dotenv import dotenv_values
from pymongo import MongoClient

sys.path.insert(0, "/app/backend")
import store_approval_service  # noqa: E402

be = dotenv_values("/app/backend/.env")
client = MongoClient(be["MONGO_URL"])
db = client[be["DB_NAME"]]

doc = store_approval_service.create_request(
    db, "TEST_JOB_PROBE",
    {"material_id": "TEST_PROBE_MAT", "site_id": "P2", "quantity": 1.0, "unit_code": "EA"},
    "TEST_PROP_PROBE",
    [{"product_id": "TEST_PROBE_COMP", "description": "probe", "unit_of_measure": "KGM",
      "required_qty": 0.01, "available_qty": 1.0,
      "locations": [{"warehouse": "RAW MATERIAL GODOWN-P2", "warehouse_id": "P2/P2-RM",
                     "stock_status": "Unrestricted", "qty": 10.0, "owner": "RI"}]}],
    "TEST_QA_PROBE",
)
rid = doc["_id"]
t0 = time.time()
r = requests.post(
    f"http://localhost:{os.environ.get('BACKEND_PORT', '8001')}/api/store-requests/{rid}/issue",
    json={"actor": "QA PROBE", "issued": [{"product_id": "TEST_PROBE_COMP", "issued_qty": 0.01}]},
    timeout=300,
)
elapsed = time.time() - t0
print("HTTP", r.status_code, f"elapsed={elapsed:.1f}s")
if r.status_code == 200:
    gm = r.json()["components"][0]["goods_movement"]
    print("gm keys:", sorted(gm.keys()))
    print(json.dumps({k: (str(v)[:300]) for k, v in gm.items()}, indent=1))
else:
    print(r.text[:600])

db["store_requests"].delete_many({"requester": "TEST_QA_PROBE"})
client.close()
