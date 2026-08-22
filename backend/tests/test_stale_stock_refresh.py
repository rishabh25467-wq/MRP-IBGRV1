"""Aug 2026 stale-stock fix: a store_request's component `locations` used to
be frozen at request-creation time. Now refresh_component_locations() re-reads
inventory_cache on every read (get/queue/journal/balance-pending) and again
inside start_issue().

Scenario: a request created BEFORE a Goods Receipt (only an SFG row on file),
then a fresh RM row lands in inventory_cache -> the RM row must now show up.

All data is synthetic (unique TEST_ product id, own site code) and removed in
teardown - both the store_requests docs AND the injected inventory_cache item.
"""
import os
import uuid
from datetime import datetime, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

frontend_env = dotenv_values("/app/frontend/.env")
backend_env = dotenv_values("/app/backend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing from env and /app/frontend/.env")
API = f"{base_url.rstrip('/')}/api"

MONGO_URL = os.environ.get("MONGO_URL") or backend_env.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME") or backend_env.get("DB_NAME")

TAG = "TEST_STALE"
SITE = "ZZ96"
PRODUCT = "TEST_STALE_PROD_1"
UNCACHED_PRODUCT = "TEST_STALE_PROD_UNCACHED"


@pytest.fixture(scope="module")
def db():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


@pytest.fixture(scope="module")
def tracker():
    return {"requests": []}


@pytest.fixture(scope="module", autouse=True)
def cleanup(db, tracker):
    yield
    if tracker["requests"]:
        db["store_requests"].delete_many({"_id": {"$in": tracker["requests"]}})
    db["inventory_cache"].update_one(
        {"_id": "latest"},
        {"$pull": {"items": {"product_id": {"$in": [PRODUCT, UNCACHED_PRODUCT]}}}},
    )


STALE_SFG_ONLY = [
    {"warehouse": "SEMI FINISH GODOWN-" + SITE, "warehouse_id": f"{SITE}/{SITE}-SFG",
     "stock_status": "Unrestricted-Use", "qty": 4.0, "owner": "RT"},
]


def seed_request(db, tracker, status="pending", product=PRODUCT, locations=None):
    req_id = f"{TAG}-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    doc = {
        "_id": req_id, "job_id": str(uuid.uuid4()), "production_proposal_id": "TEST_PROP_STALE",
        "material_id": "TEST_MAT_STALE", "site_id": SITE, "quantity": 1.0, "unit_code": "EA",
        "requester": "TEST_STALE_REQUESTER",
        "components": [{
            "product_id": product, "description": f"{TAG} component", "unit_of_measure": "EA",
            "required_qty": 10.0, "available_qty": 4.0,
            "locations": [dict(loc) for loc in (STALE_SFG_ONLY if locations is None else locations)],
            "issued_qty": 3.0 if status == "resolved_balance_pending" else None,
            "shortfall": 7.0 if status == "resolved_balance_pending" else None,
        }],
        "status": status, "store_actor": None, "store_decision": None,
        "planner_actor": None, "planner_decision": None,
        "resolution": "store_proceeded_partial" if status == "resolved_balance_pending" else None,
        "created_at": now, "updated_at": now, "resolved_at": None,
    }
    db["store_requests"].insert_one(doc)
    tracker["requests"].append(req_id)
    return req_id


def inject_goods_receipt(db):
    """Simulates a Goods Receipt that reached inventory_cache AFTER the
    request was created - a brand-new RM row for the same product."""
    db["inventory_cache"].update_one(
        {"_id": "latest"}, {"$pull": {"items": {"product_id": PRODUCT}}})
    db["inventory_cache"].update_one({"_id": "latest"}, {"$push": {"items": {
        "product_id": PRODUCT, "description": f"{TAG} component", "total_qty": 29.0, "uom": "EA",
        "locations": [
            {"site": f"TEST COMPANY-{SITE}", "logistics_area": f"SEMI FINISH GODOWN-{SITE}",
             "logistics_area_id": f"{SITE}/{SITE}-SFG", "stock_status": "Unrestricted-Use",
             "qty": 4.0, "company_code": "RT", "company_name": "TEST COMPANY"},
            {"site": f"TEST COMPANY-{SITE}", "logistics_area": f"RAW MATERIAL GODOWN-{SITE}",
             "logistics_area_id": f"{SITE}/{SITE}-RM", "stock_status": "Unrestricted-Use",
             "qty": 25.0, "company_code": "RT", "company_name": "TEST COMPANY"},
            {"site": f"OTHER COMPANY-P9", "logistics_area": "RAW MATERIAL GODOWN-P9",
             "logistics_area_id": "P9/P9-RM", "stock_status": "Unrestricted-Use",
             "qty": 999.0, "company_code": "RT", "company_name": "OTHER COMPANY"},
        ],
        "category": "Other", "unit_cost": 1.0, "currency": "INR", "total_value": 29.0,
    }}})


def rm_row(component):
    return next((loc for loc in component["locations"]
                 if loc.get("warehouse_id") == f"{SITE}/{SITE}-RM"), None)


class TestStaleStockRefresh:
    def test_detail_read_picks_up_new_rm_row(self, db, tracker):
        req_id = seed_request(db, tracker)

        # before the goods receipt: only the frozen SFG row
        before = requests.get(f"{API}/store-requests/{req_id}", timeout=120)
        assert before.status_code == 200, before.text
        assert rm_row(before.json()["components"][0]) is None

        inject_goods_receipt(db)

        after = requests.get(f"{API}/store-requests/{req_id}", timeout=120)
        assert after.status_code == 200, after.text
        comp = after.json()["components"][0]
        rm = rm_row(comp)
        assert rm is not None, f"live refresh did not pick up the new RM row: {comp['locations']}"
        assert rm["qty"] == 25.0
        assert rm["owner"] == "RT"
        assert rm["stock_status"] == "Unrestricted-Use"
        # other sites must never leak in
        assert all(SITE in (loc.get("warehouse_id") or "") for loc in comp["locations"]), comp["locations"]
        assert len(comp["locations"]) == 2

    def test_queue_journal_and_balance_pending_reflect_refresh(self, db, tracker):
        inject_goods_receipt(db)
        pending_id = seed_request(db, tracker, status="pending")
        balance_id = seed_request(db, tracker, status="resolved_balance_pending")

        queue = requests.get(f"{API}/store-requests", timeout=120).json()["requests"]
        row = next((r for r in queue if r["_id"] == pending_id), None)
        assert row is not None, "seeded pending request missing from queue"
        assert rm_row(row["components"][0]) is not None, "queue view still shows stale locations"

        journal = requests.get(f"{API}/store-requests/journal", timeout=120).json()["requests"]
        jrow = next((r for r in journal if r["_id"] == pending_id), None)
        assert jrow is not None
        assert rm_row(jrow["components"][0]) is not None, "journal still shows stale locations"

        bp = requests.get(f"{API}/store-requests/balance-pending", timeout=120).json()["requests"]
        brow = next((r for r in bp if r["_id"] == balance_id), None)
        assert brow is not None, "seeded resolved_balance_pending request missing from balance-pending"
        assert rm_row(brow["components"][0]) is not None, "balance-pending still shows stale locations"

    def test_refresh_is_display_only_not_persisted_over_history(self, db, tracker):
        """The refresh must not silently rewrite the stored snapshot behind
        the store user's back (read-time only)."""
        req_id = seed_request(db, tracker)
        inject_goods_receipt(db)
        requests.get(f"{API}/store-requests/{req_id}", timeout=120)
        stored = db["store_requests"].find_one({"_id": req_id})
        assert rm_row(stored["components"][0]) is None, (
            "read-time refresh persisted back into Mongo - acceptable only if intentional")

    def test_product_absent_from_cache_keeps_frozen_snapshot(self, db, tracker):
        """None (no cache entry at all) must NOT wipe the snapshot."""
        req_id = seed_request(db, tracker, product=UNCACHED_PRODUCT)
        r = requests.get(f"{API}/store-requests/{req_id}", timeout=120)
        assert r.status_code == 200, r.text
        locs = r.json()["components"][0]["locations"]
        assert len(locs) == 1 and locs[0]["warehouse_id"] == f"{SITE}/{SITE}-SFG"

    def test_issue_uses_refreshed_rm_location(self, db, tracker):
        """start_issue() refreshes again, so the movement now finds the new
        RM row instead of skipping with 'No stock on file'. Dry-run guard is
        asserted first so no live SAP write can happen."""
        # NOTE: read the flag from backend/.env, NOT os.environ - the
        # pytest process never runs load_dotenv(), so is_dry_run() would
        # always report "dry run" here even when the server is live.
        dry = (backend_env.get("SAP_GOODS_MOVEMENT_DRY_RUN") or "true").strip('"').lower() != "false"
        if not dry:
            pytest.skip("SAP_GOODS_MOVEMENT_DRY_RUN is off - refusing to fire a live movement")
        inject_goods_receipt(db)
        req_id = seed_request(db, tracker)
        r = requests.post(f"{API}/store-requests/{req_id}/issue", json={
            "issued": [{"product_id": PRODUCT, "issued_qty": 10}], "decision": None, "actor": "QA Stale",
        }, timeout=120)
        assert r.status_code == 200, r.text
        comp = r.json()["request"]["components"][0]
        assert rm_row(comp) is not None, "start_issue did not refresh locations before computing the movement"

        import time
        job_id = r.json()["job_id"]
        for _ in range(60):
            job = requests.get(f"{API}/store-requests/issue-status/{job_id}", timeout=120).json()
            if job.get("status") in ("done", "failed"):
                break
            time.sleep(1)
        assert job["status"] == "done", job
        final = db["store_requests"].find_one({"_id": req_id})
        mv = final["components"][0].get("goods_movement")
        assert mv is not None, "no goods_movement recorded"
        assert mv.get("attempted") is True, f"movement was skipped despite fresh RM stock: {mv}"
        assert final["status"] == "resolved"
