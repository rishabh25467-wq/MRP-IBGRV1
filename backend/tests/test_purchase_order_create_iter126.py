"""Iteration 126 - Purchase Order Creation automation (NEW feature).

Covers: auth gating on /api/purchase-orders/* (401 / 403 / 200), sites,
supplier & product autosuggest (from Mongo caches), history, and the
POST /create path. The live create test deliberately uses FAKE
supplier/product references so SAP rejects it (never writes a real PO
into the customer's live tenant). The SUCCESS path is verified with a
monkeypatched sap_po_write_client (in-process call of the endpoint
function), never a real SAP write.
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

frontend_env = dotenv_values("/app/frontend/.env")
BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or frontend_env["REACT_APP_BACKEND_URL"]).rstrip("/")
API = f"{BASE_URL}/api"

backend_env = dotenv_values("/app/backend/.env")
PO_SESSION = "DNHiw5YqmjV4wxPeFjdNi0Q0TfXzpYMkzwitivOXDgY"  # user po.tester, allowed_pages=['purchase_order']

HISTORY_COLL = "purchase_order_creation_history"


@pytest.fixture(scope="module")
def db():
    client = MongoClient(backend_env["MONGO_URL"])
    return client[backend_env["DB_NAME"]]


@pytest.fixture(scope="module")
def po_client():
    s = requests.Session()
    s.cookies.set("vms_session", PO_SESSION)
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def noperm_session(db):
    """Synthetic session for a user WITHOUT the purchase_order page."""
    uid = "iter126-tid:iter126-oid"
    token = "ITER126NOPERM" + uuid.uuid4().hex
    db["auth_users"].update_one({"_id": uid}, {"$set": {
        "tid": "iter126-tid", "oid": "iter126-oid", "email": "TEST_noperm@internal.test",
        "name": "TEST NoPerm", "role": "user", "allowed_pages": ["bom_explorer"], "bound_sites": [],
    }}, upsert=True)
    db["auth_sessions"].insert_one({
        "_id": token, "user_id": uid,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
    })
    yield token
    db["auth_sessions"].delete_one({"_id": token})
    db["auth_users"].delete_one({"_id": uid})


# ---------------- auth gating ----------------
class TestPurchaseOrderAuth:
    def test_no_cookie_is_401(self):
        r = requests.get(f"{API}/purchase-orders/sites")
        assert r.status_code == 401, r.text[:300]

    def test_without_permission_is_403(self, noperm_session):
        s = requests.Session()
        s.cookies.set("vms_session", noperm_session)
        for path in ["/purchase-orders/sites", "/purchase-orders/history",
                     "/purchase-orders/suppliers/search?q=HAM", "/purchase-orders/products/search?q=BOX"]:
            r = s.get(f"{API}{path}")
            assert r.status_code == 403, f"{path} -> {r.status_code} {r.text[:200]}"

    def test_create_without_permission_is_403(self, noperm_session):
        s = requests.Session()
        s.cookies.set("vms_session", noperm_session)
        r = s.post(f"{API}/purchase-orders/create", json={
            "supplier_code": "ZZZTEST999", "purchase_unit_site": "P1", "bill_to_company": "RI",
            "po_date": "2026-08-28", "currency": "INR",
            "items": [{"product_id": "ZZZNOTREAL", "quantity": 1, "unit_of_measure": "EA",
                       "unit_price": 1, "delivery_date": "2026-09-05"}],
        })
        assert r.status_code == 403, r.text[:300]

    def test_with_permission_allowed(self, po_client):
        r = po_client.get(f"{API}/purchase-orders/sites")
        assert r.status_code == 200, r.text[:300]


# ---------------- read endpoints ----------------
class TestPurchaseOrderLookups:
    def test_sites(self, po_client):
        r = po_client.get(f"{API}/purchase-orders/sites")
        assert r.status_code == 200
        sites = r.json()["sites"]
        assert isinstance(sites, list) and len(sites) > 0
        assert all(isinstance(s, str) for s in sites)
        assert "P1" in sites or "P2" in sites, sites

    def test_supplier_search_hamidi(self, po_client):
        r = po_client.get(f"{API}/purchase-orders/suppliers/search", params={"q": "HAMIDI"})
        assert r.status_code == 200
        data = r.json()
        assert len(data) >= 1, data
        codes = [d["supplier_code"] for d in data]
        assert "H1330" in codes, data
        hit = next(d for d in data if d["supplier_code"] == "H1330")
        assert "HAMIDI" in hit["name"].upper()

    def test_supplier_search_by_code_prefix(self, po_client):
        r = po_client.get(f"{API}/purchase-orders/suppliers/search", params={"q": "H133"})
        assert r.status_code == 200
        assert "H1330" in [d["supplier_code"] for d in r.json()]

    def test_supplier_search_no_match_returns_empty(self, po_client):
        r = po_client.get(f"{API}/purchase-orders/suppliers/search", params={"q": "ZZZNOSUCHSUPPLIER"})
        assert r.status_code == 200
        assert r.json() == []

    def test_supplier_search_requires_q(self, po_client):
        r = po_client.get(f"{API}/purchase-orders/suppliers/search")
        assert r.status_code == 422

    def test_supplier_search_regex_special_chars_safe(self, po_client):
        r = po_client.get(f"{API}/purchase-orders/suppliers/search", params={"q": "(("})
        assert r.status_code == 200, r.text[:300]

    def test_product_search_box(self, po_client):
        r = po_client.get(f"{API}/purchase-orders/products/search", params={"q": "BOX"})
        assert r.status_code == 200
        data = r.json()
        assert len(data) >= 1, data
        first = data[0]
        assert isinstance(first["product_id"], str) and first["product_id"]
        assert "unit_of_measure" in first and "description" in first

    def test_product_search_limit_respected(self, po_client):
        r = po_client.get(f"{API}/purchase-orders/products/search", params={"q": "P", "limit": 5})
        assert r.status_code == 200
        assert len(r.json()) <= 5

    def test_history_shape_and_no_raw_xml(self, po_client):
        r = po_client.get(f"{API}/purchase-orders/history")
        assert r.status_code == 200
        docs = r.json()
        assert isinstance(docs, list)
        for d in docs:
            assert "raw_xml" not in d
        # newest first
        stamps = [d.get("created_at") for d in docs if d.get("created_at")]
        assert stamps == sorted(stamps, reverse=True)


# ---------------- live create (must FAIL at SAP, never write a real PO) ----------------
class TestPurchaseOrderCreateLive:
    def test_validation_rejects_bad_bill_to(self, po_client):
        r = po_client.post(f"{API}/purchase-orders/create", json={
            "supplier_code": "ZZZTEST999", "purchase_unit_site": "P1", "bill_to_company": "XX",
            "po_date": "2026-08-28", "currency": "INR",
            "items": [{"product_id": "ZZZNOTREAL", "quantity": 1, "unit_of_measure": "EA",
                       "unit_price": 1, "delivery_date": "2026-09-05"}],
        })
        assert r.status_code == 400, r.text[:300]
        assert "RI" in r.json()["detail"]

    def test_validation_rejects_empty_items(self, po_client):
        r = po_client.post(f"{API}/purchase-orders/create", json={
            "supplier_code": "ZZZTEST999", "purchase_unit_site": "P1", "bill_to_company": "RI",
            "po_date": "2026-08-28", "currency": "INR", "items": [],
        })
        assert r.status_code == 422, r.text[:300]

    def test_validation_rejects_non_positive_qty(self, po_client):
        r = po_client.post(f"{API}/purchase-orders/create", json={
            "supplier_code": "ZZZTEST999", "purchase_unit_site": "P1", "bill_to_company": "RI",
            "po_date": "2026-08-28", "currency": "INR",
            "items": [{"product_id": "ZZZNOTREAL", "quantity": 0, "unit_of_measure": "EA",
                       "unit_price": 1, "delivery_date": "2026-09-05"}],
        })
        assert r.status_code == 422, r.text[:300]

    def test_live_sap_rejects_fake_references(self, po_client, db):
        before = db[HISTORY_COLL].count_documents({})
        r = po_client.post(f"{API}/purchase-orders/create", json={
            "supplier_code": "ZZZTEST999", "purchase_unit_site": "P1", "bill_to_company": "RI",
            "po_date": "2026-08-28", "currency": "INR", "pr_number": "TEST_PR_ITER126",
            "items": [{"product_id": "ZZZNOTREAL", "description": "TEST fake product", "quantity": 2,
                       "unit_of_measure": "EA", "unit_price": 10, "delivery_date": "2026-09-05"}],
        }, timeout=120)
        print(f"LIVE SAP create -> HTTP {r.status_code}: {r.text[:800]}")
        assert r.status_code == 502, f"expected 502 business fault, got {r.status_code}: {r.text[:600]}"
        # KNOWN ENV ISSUE: the k8s/Cloudflare edge replaces an app-returned
        # HTTP 502 with its own "Bad gateway" HTML page, so the SAP fault
        # message never reaches the browser. Verified via localhost that the
        # app itself returns JSON {"detail": "SAP rejected ..."}.
        if "text/html" in r.headers.get("content-type", ""):
            print("EDGE-MANGLED: 502 body replaced by Cloudflare HTML error page - SAP detail lost to the client")
        else:
            detail = r.json().get("detail", "")
            assert isinstance(detail, str) and "SAP" in detail
        # nothing must be persisted when the SAP call failed
        after = db[HISTORY_COLL].count_documents({})
        assert after == before, "history row written despite SAP failure"
        assert db[HISTORY_COLL].count_documents({"supplier_code": "ZZZTEST999"}) == 0


# ---------------- mocked success path (no SAP write) ----------------
class TestPurchaseOrderCreateMocked:
    def test_success_path_derives_company_and_writes_history(self, db, monkeypatch):
        sys.path.insert(0, "/app/backend")
        import server

        captured = {}

        def fake_create(company_code, purchase_unit_site, supplier_code, bill_to_company_code,
                        po_date, currency, items):
            captured.update(dict(company_code=company_code, purchase_unit_site=purchase_unit_site,
                                 supplier_code=supplier_code, bill_to=bill_to_company_code,
                                 po_date=po_date, currency=currency, items=items))
            return {"po_number": "TEST_PO_ITER126", "po_uuid": "test-uuid-126", "raw_xml": "<xml/>"}

        monkeypatch.setattr(server.sap_po_write_client, "create_purchase_order", fake_create)

        class _State:
            user = {"name": "PO Tester", "tid": "po-test-tid", "oid": "po-test-oid"}

        class _Req:
            state = _State()

        payload = server.PurchaseOrderCreateRequest(
            supplier_code="TEST_SUP1", purchase_unit_site="P1", bill_to_company="RT",
            po_date="2026-08-28", currency="INR", pr_number="TEST_PR_M1",
            items=[server.PurchaseOrderLineItemIn(
                product_id="TEST_PROD1", description="TEST prod", quantity=3, unit_of_measure="EA",
                unit_price=25.5, delivery_date="2026-09-10")],
        )
        try:
            resp = asyncio.run(server.create_purchase_order(payload, _Req()))
            assert resp == {"po_number": "TEST_PO_ITER126", "po_uuid": "test-uuid-126"}
            # company auto-derived from site P1 -> RI (independent of bill-to RT)
            assert captured["company_code"] == "RI", captured
            assert captured["bill_to"] == "RT"
            assert captured["items"][0]["site_id"] == "P1"
            doc = db[HISTORY_COLL].find_one({"po_number": "TEST_PO_ITER126"})
            assert doc is not None, "history row not written on success"
            assert doc["company_code"] == "RI"
            assert doc["bill_to_company"] == "RT"
            assert doc["pr_number"] == "TEST_PR_M1"
            assert doc["items"][0]["product_id"] == "TEST_PROD1"
            assert doc["items"][0]["description"] == "TEST prod"
            assert doc["created_by"] == "PO Tester"
        finally:
            db[HISTORY_COLL].delete_many({"po_number": "TEST_PO_ITER126"})

    def test_company_derivation_mapping(self):
        sys.path.insert(0, "/app/backend")
        from sap_wip_clearing_client import company_and_set_of_books_for_site
        for site in ["P1", "P8", "P5", "P1W", "W1", "p1"]:
            assert company_and_set_of_books_for_site(site)[0] == "RI", site
        for site in ["P2", "P3", "P4", "P7", "P9", "XYZ"]:
            assert company_and_set_of_books_for_site(site)[0] == "RT", site


# ---------------- regression on neighbouring endpoints ----------------
class TestRegression:
    def test_existing_suppliers_endpoint_still_ok(self, db):
        # /api/suppliers requires supplier_master; use a synthetic super_admin-ish check via po user is 403-expected
        uid = "iter126b-tid:iter126b-oid"
        token = "ITER126REG" + uuid.uuid4().hex
        db["auth_users"].update_one({"_id": uid}, {"$set": {
            "tid": "iter126b-tid", "oid": "iter126b-oid", "email": "TEST_reg@internal.test",
            "name": "TEST Reg", "role": "super_admin", "allowed_pages": [], "bound_sites": [],
        }}, upsert=True)
        db["auth_sessions"].insert_one({"_id": token, "user_id": uid,
                                       "expires_at": datetime.now(timezone.utc) + timedelta(days=1)})
        try:
            s = requests.Session()
            s.cookies.set("vms_session", token)
            r = s.get(f"{API}/suppliers", timeout=60)
            assert r.status_code == 200, r.text[:300]
            r2 = s.get(f"{API}/products/search", params={"q": "P2"}, timeout=60)
            assert r2.status_code == 200, r2.text[:300]
            r3 = s.get(f"{API}/admin/pages", timeout=30)
            assert r3.status_code == 200, r3.text[:300]
            pages = r3.json()["pages"]
            keys = [p["key"] for p in pages]
            assert "purchase_order" in keys, keys
            label = next(p["label"] for p in pages if p["key"] == "purchase_order")
            assert label == "Purchase Order Creation", label
            # previous permissions still present
            for old in ["bom_explorer", "purchasing_plan", "inventory", "admin", "supplier_portal_admin"]:
                assert old in keys, f"{old} missing from PAGE_CATALOG"
        finally:
            db["auth_sessions"].delete_one({"_id": token})
            db["auth_users"].delete_one({"_id": uid})
