"""Seed two in_transit S9999 fixture shipments for iteration-125 UI testing.
Lives outside /app/backend so writing it doesn't trigger a uvicorn --reload."""
import requests
from dotenv import dotenv_values

BASE = (dotenv_values("/app/frontend/.env")["REACT_APP_BACKEND_URL"]).rstrip("/") + "/api"
s = requests.Session()
r = s.post(f"{BASE}/supplier-portal/login", json={"email": "vendor1@testco.com", "password": "DummyTest123"}, timeout=120)
assert r.status_code == 200, r.text[:300]
for items in (
    [{"po_number": "TESTGRN1", "item_number": "1", "ship_qty": 4},
     {"po_number": "TESTGRN1", "item_number": "2", "ship_qty": 2}],
    [{"po_number": "TESTGRN2", "item_number": "1", "ship_qty": 3}],
):
    r = s.post(f"{BASE}/supplier-portal/shipments", json={"items": items}, timeout=120)
    assert r.status_code == 200, r.text[:300]
    print("CREATED", r.json()["_id"], r.json()["status"], [i["item_number"] for i in r.json()["items"]])
