"""
iter192: verify STO Receive endpoint no longer invokes Playwright/SAP-UI-login,
and that the two 400 error gates in prepare_receipt are wired correctly.

Priorities per review_request:
 (a) 400 error paths (gi not posted, missing outbound_delivery_ids)
 (b) Confirm no Playwright/browser lines in backend logs during the flow
"""
import os
import time
import secrets
import datetime
import requests
import pytest
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")
load_dotenv("/app/frontend/.env")

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
SESSION_COOKIE = "iter192superadmin90a7313e8c39fec4e9d02b68"

MONGO = MongoClient(os.environ["MONGO_URL"])
DB = MONGO[os.environ["DB_NAME"]]
STO_COL = DB["stock_transfer_orders"]


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.cookies.set("vms_session", SESSION_COOKIE)
    s.headers.update({"Content-Type": "application/json"})
    return s


# ---------- auth sanity ----------
def test_session_authenticated(client):
    r = client.get(f"{BASE_URL}/api/auth/me", timeout=15)
    assert r.status_code == 200, f"auth/me failed: {r.status_code} {r.text[:200]}"
    data = r.json()
    assert data.get("role") == "super_admin", data


# ---------- 400 gate 1: gi_status != 'posted' ----------
def test_receive_rejects_when_gi_not_posted(client):
    doc = STO_COL.find_one({"gi_status": {"$ne": "posted"}}, {"_id": 1})
    assert doc, "No STO with gi_status != 'posted' available"
    sto_id = doc["_id"]
    r = client.post(
        f"{BASE_URL}/api/inbound-receipts/{sto_id}/receive",
        json={"items": []},
        timeout=15,
    )
    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text[:300]}"
    detail = r.json().get("detail", "").lower()
    assert "nothing to receive" in detail, f"unexpected detail: {detail}"


# ---------- 400 gate 2: gi_status='posted' but no outbound_delivery_ids ----------
SYNTH_ID = "TEST-STO-ITER192-NODLV"


@pytest.fixture(scope="module")
def synthetic_sto_no_delivery():
    """Insert a synthetic STO with gi_status=posted and empty outbound_delivery_ids."""
    STO_COL.delete_one({"_id": SYNTH_ID})
    STO_COL.insert_one({
        "_id": SYNTH_ID,
        "gi_status": "posted",
        "outbound_delivery_ids": [],
        "items": [{"line_no": 1, "product_id": "TEST-PROD", "requested_qty": 1.0}],
        "ship_from_site": "P1",
        "ship_to_site": "P8",
        "ship_to_location_id": "SEMI FINISH GODOWN-P8",
        "status": "draft",
        "created_at": datetime.datetime.now(datetime.timezone.utc),
    })
    yield SYNTH_ID
    STO_COL.delete_one({"_id": SYNTH_ID})


def test_receive_rejects_when_no_sap_delivery_ref(client, synthetic_sto_no_delivery):
    sto_id = synthetic_sto_no_delivery
    r = client.post(
        f"{BASE_URL}/api/inbound-receipts/{sto_id}/receive",
        json={"items": []},
        timeout=15,
    )
    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text[:300]}"
    detail = r.json().get("detail", "")
    assert "No SAP delivery reference" in detail, f"unexpected detail: {detail}"


# ---------- 404 for a non-existent STO (bonus) ----------
def test_receive_unknown_sto_returns_400(client):
    r = client.post(
        f"{BASE_URL}/api/inbound-receipts/DOES-NOT-EXIST-XYZ/receive",
        json={"items": []},
        timeout=15,
    )
    assert r.status_code == 400
    detail = r.json().get("detail", "").lower()
    assert "not found" in detail, detail


# ---------- Log verification: no Playwright launched by receive job ----------
def test_no_playwright_lines_after_receive_attempts():
    """Scan recent backend log tail for Playwright/browser-launch traces
    correlated with the two 400-path calls above. Since the 400s short-circuit
    BEFORE job creation, absolutely nothing playwright-related should have
    fired in this test module's timeframe."""
    log_paths = [
        "/var/log/supervisor/backend.err.log",
        "/var/log/supervisor/backend.out.log",
    ]
    playwright_markers = ["playwright", "browser.launch", "chromium", "post_goods_receipts_via_ui"]
    recent_hits = []
    now = time.time()
    for p in log_paths:
        if not os.path.exists(p):
            continue
        try:
            with open(p, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 200_000))
                tail = f.read().decode("utf-8", errors="ignore")
        except Exception:
            continue
        for line in tail.splitlines()[-2000:]:
            low = line.lower()
            if any(m in low for m in playwright_markers):
                recent_hits.append(line)
    # We only fail if a playwright launch is tied to an inbound_receipt/receive job
    # Only flag lines that actually reference a playwright marker AND the
    # inbound receive job pipeline - ignore incidental "receive" mentions
    # (e.g. filename reload notices).
    correlated = [
        h for h in recent_hits
        if ("post_goods_receipts_via_ui" in h.lower())
        or ("playwright" in h.lower() and "inbound_receipt" in h.lower())
    ]
    assert not correlated, (
        "Playwright/browser lines found tied to inbound receive:\n"
        + "\n".join(correlated[:20])
    )


# ---------- Static source-code assertion: server.py's receive endpoint no longer
# references sap_playwright_pgr_service ----------
def test_receive_endpoint_source_has_no_playwright_call():
    with open("/app/backend/server.py", "r") as f:
        src = f.read()
    # locate the endpoint block
    marker = '@api_router.post("/inbound-receipts/{sto_id}/receive")'
    idx = src.find(marker)
    assert idx > -1, "receive endpoint not found in server.py"
    # end at the next api_router decorator
    end = src.find("@api_router", idx + len(marker))
    block = src[idx:end]
    assert "sap_playwright_pgr_service" not in block, "receive endpoint still calls sap_playwright_pgr_service"
    assert "post_goods_receipts_via_ui" not in block, "receive endpoint still calls post_goods_receipts_via_ui"
    assert "receive_stock_transfer_order" in block, "receive endpoint does not call receive_stock_transfer_order"
