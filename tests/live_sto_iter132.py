"""iteration_132 LIVE test - creates 1 single-line + 1 multi-line STO against the
LIVE SAP tenant (1 EA each) and polls gi_status / outbound_delivery_ids until
posted. Run in background: python /app/tests/live_sto_iter132.py > /tmp/live132.log 2>&1 &
"""
import json
import os
import time

import requests
from dotenv import dotenv_values

BASE = (dotenv_values("/app/frontend/.env")["REACT_APP_BACKEND_URL"]).rstrip("/")
TOKEN = "TEST_sto_session_token_solo"
S = requests.Session()
S.cookies.set("vms_session", TOKEN)

COMMON = {
    "ship_to_site_id": "P1",
    "ship_to_location_id": "P1-RM",
    "requested_delivery_date": "2026-08-30",
    "transportation_mode": "By Road",
    "vehicle_no": "UP81AA0099",
    "place_of_supply": "Aligarh",
    "gr_no": "9001",
    "date_of_supply": "2026-08-30",
    "freight_forwarder": "Pooja Transport",
}
SINGLE = dict(COMMON, items=[{"product_id": "G12LW", "source_warehouse_id": "P8-RM", "requested_qty": 1}])
MULTI = dict(COMMON, items=[
    {"product_id": "G12LW", "source_warehouse_id": "P8-RM", "requested_qty": 1},
    {"product_id": "G12FW", "source_warehouse_id": "P8-RM", "requested_qty": 1},
])


def create(payload, label):
    r = S.post(f"{BASE}/api/stock-transfer/orders", json=payload, timeout=180)
    print(f"[{label}] create HTTP {r.status_code}", flush=True)
    if r.status_code != 200:
        print(f"[{label}] body: {r.text[:600]}", flush=True)
        return None
    d = r.json()
    print(f"[{label}] sto_id={d['sto_id']} lines={len(d['items'])} sap_job_id={d.get('sap_job_id')}", flush=True)
    # echo back the metadata fields as persisted
    print(f"[{label}] persisted metadata: " + json.dumps({k: d.get(k) for k in (
        "vehicle_no", "transportation_mode", "place_of_supply", "gr_no", "date_of_supply",
        "freight_forwarder", "ship_from_site_id", "ship_to_site_id", "ship_to_location_id")}), flush=True)
    return d["sto_id"]


def poll(ids, minutes=16):
    deadline = time.time() + minutes * 60
    done = {}
    while time.time() < deadline and len(done) < len(ids):
        time.sleep(30)
        for label, sto_id in ids.items():
            if label in done or not sto_id:
                continue
            try:
                d = S.get(f"{BASE}/api/stock-transfer/orders/{sto_id}", timeout=120).json()
            except Exception as e:
                print(f"[{label}] poll error {e}", flush=True)
                continue
            print(f"[{label}] {sto_id} sap_order_id={d.get('sap_order_id')} gi_status={d.get('gi_status')} "
                  f"delivery_ids={d.get('outbound_delivery_ids')} gi_error={(d.get('gi_error') or '')[:200]}", flush=True)
            if d.get("gi_status") in ("posted", "failed"):
                done[label] = d
    print("=== FINAL ===", flush=True)
    for label, sto_id in ids.items():
        if not sto_id:
            print(f"[{label}] NOT CREATED", flush=True)
            continue
        d = S.get(f"{BASE}/api/stock-transfer/orders/{sto_id}", timeout=120).json()
        print(f"[{label}] {sto_id} lines={len(d['items'])} gi_status={d.get('gi_status')} "
              f"delivery_ids={d.get('outbound_delivery_ids')} erp={d.get('erp_portal_status')} "
              f"gi_error={(d.get('gi_error') or '')[:300]}", flush=True)


if __name__ == "__main__":
    single_id = create(SINGLE, "SINGLE")
    time.sleep(5)
    multi_id = create(MULTI, "MULTI")
    poll({"SINGLE": single_id, "MULTI": multi_id})
    print("DONE", flush=True)
