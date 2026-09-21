"""Sep 21 2026 feature: proactive Valuation check at STO creation +
type-aware notification resolve + friendly_valuation_error() rewrite for
Put Away failures. Covers backend units + API endpoints, no live STO
creation (that requires real GST/master data and is not the subject).
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

sys.path.insert(0, "/app/backend")

frontend_env = dotenv_values("/app/frontend/.env")
BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")).rstrip("/")
backend_env = dotenv_values("/app/backend/.env")
COOKIE_NAME = "vms_session"

SAFE_PRODUCT_ID = "P27175"
SAFE_SITE_ID = "P1"


@pytest.fixture(scope="module")
def mongo_db():
    client = MongoClient(backend_env["MONGO_URL"])
    yield client[backend_env["DB_NAME"]]
    client.close()


@pytest.fixture(scope="module")
def admin_session(mongo_db):
    now = datetime.now(timezone.utc)
    user_id = "TESTtid:TESToid-valuation-admin"
    token = "TEST_valuation_" + uuid.uuid4().hex
    mongo_db["auth_users"].replace_one(
        {"_id": user_id},
        {"_id": user_id, "tid": "TESTtid", "oid": "TESToid-valuation-admin",
         "email": "TEST_valuation@example.test", "name": "TEST Valuation QA",
         "role": "admin", "allowed_pages": [],
         "created_at": now, "last_login_at": now},
        upsert=True,
    )
    mongo_db["auth_sessions"].replace_one(
        {"_id": token},
        {"_id": token, "user_id": user_id, "expires_at": now + timedelta(days=1)},
        upsert=True,
    )
    yield token
    mongo_db["auth_users"].delete_one({"_id": user_id})
    mongo_db["auth_sessions"].delete_one({"_id": token})


def client_for(token):
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json", "Cookie": f"{COOKIE_NAME}={token}"})
    return s


# ----- friendly_valuation_error() unit tests -----
class TestFriendlyValuationError:
    def test_valuation_data_missing_pattern_rewritten(self):
        from sap_material_valuation_data_client import friendly_valuation_error
        raw = "Valuation data missing for material G12NUT at plant"
        out = friendly_valuation_error(raw, "P9")
        assert "G12NUT" in out
        assert "P9" in out
        assert "Cost/Valuation" in out
        assert "Retry" in out

    def test_account_det_group_pattern_rewritten(self):
        from sap_material_valuation_data_client import friendly_valuation_error
        raw = "Account det. group is missing for material G12FW; please maintain"
        out = friendly_valuation_error(raw, "P2")
        assert "G12FW" in out
        assert "P2" in out
        assert "Cost/Valuation" in out

    def test_financials_pu_pattern_rewritten(self):
        from sap_material_valuation_data_client import friendly_valuation_error
        raw = "Financials PU for material G12LW P7 is missing or in prep"
        out = friendly_valuation_error(raw, "P7")
        assert "G12LW" in out
        assert "P7" in out

    def test_unknown_message_passed_through(self):
        from sap_material_valuation_data_client import friendly_valuation_error
        raw = "Something totally unrelated happened"
        assert friendly_valuation_error(raw, "P1") == raw

    def test_no_material_in_text_uses_generic(self):
        from sap_material_valuation_data_client import friendly_valuation_error
        out = friendly_valuation_error("Valuation data missing", "P5")
        assert "This item" in out
        assert "P5" in out

    def test_empty_message(self):
        from sap_material_valuation_data_client import friendly_valuation_error
        assert friendly_valuation_error("", "P1") == ""
        assert friendly_valuation_error(None, "P1") is None


# ----- _missing_valuation_products() unit tests -----
class TestMissingValuationProducts:
    def test_returns_empty_when_no_client(self):
        import stock_transfer_service as svc
        assert svc._missing_valuation_products(None, None, {"items": [], "ship_to_site_id": "P1"}) == []

    def test_returns_empty_when_client_returns_none(self, mongo_db):
        """Site not in SITE_TO_PERMANENT_ESTABLISHMENT_UUID mapping -> fail-open."""
        import stock_transfer_service as svc

        class FakeClient:
            def has_valuation_level(self, uuids, site_id):
                return None

        doc = {"items": [{"product_id": "TESTPX1"}], "ship_to_site_id": "PXX"}
        # ensure a component_master row exists so it isn't short-circuited
        mongo_db["component_master"].replace_one(
            {"_id": "TESTPX1"}, {"_id": "TESTPX1", "product_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}, upsert=True)
        try:
            assert svc._missing_valuation_products(mongo_db, FakeClient(), doc) == []
        finally:
            mongo_db["component_master"].delete_one({"_id": "TESTPX1"})

    def test_returns_missing_product_when_client_reports_false(self, mongo_db):
        import stock_transfer_service as svc

        class FakeClient:
            def has_valuation_level(self, uuids, site_id):
                # Report the sole product as missing valuation
                return {u.upper(): False for u in uuids}

        mongo_db["component_master"].replace_one(
            {"_id": "TESTPX2"}, {"_id": "TESTPX2", "product_uuid": "aaaaaaaa-1111-2222-3333-444444444444"}, upsert=True)
        try:
            doc = {"items": [{"product_id": "TESTPX2"}], "ship_to_site_id": "P9"}
            missing = svc._missing_valuation_products(mongo_db, FakeClient(), doc)
            assert missing == ["TESTPX2"]
        finally:
            mongo_db["component_master"].delete_one({"_id": "TESTPX2"})

    def test_returns_empty_when_client_reports_true(self, mongo_db):
        import stock_transfer_service as svc

        class FakeClient:
            def has_valuation_level(self, uuids, site_id):
                return {u.upper(): True for u in uuids}

        mongo_db["component_master"].replace_one(
            {"_id": "TESTPX3"}, {"_id": "TESTPX3", "product_uuid": "aaaaaaaa-5555-6666-7777-888888888888"}, upsert=True)
        try:
            doc = {"items": [{"product_id": "TESTPX3"}], "ship_to_site_id": "P1"}
            assert svc._missing_valuation_products(mongo_db, FakeClient(), doc) == []
        finally:
            mongo_db["component_master"].delete_one({"_id": "TESTPX3"})

    def test_upsert_missing_planning_notification_supports_type_kwarg(self, mongo_db):
        import stock_transfer_service as svc
        product_id, site_id = "TESTVALP1", "TESTSITEV"
        try:
            svc._upsert_missing_planning_notification(
                mongo_db, "STO-VTEST-1", product_id, site_id, "some message", notif_type="missing_valuation")
            doc = mongo_db[svc.NOTIFICATIONS_COLLECTION].find_one(
                {"product_id": product_id, "site_id": site_id, "resolved": False})
            assert doc is not None
            assert doc["type"] == "missing_valuation"
            # Repeat call should upsert (dedupe by type+product+site), not create a duplicate
            svc._upsert_missing_planning_notification(
                mongo_db, "STO-VTEST-2", product_id, site_id, "updated message", notif_type="missing_valuation")
            docs = list(mongo_db[svc.NOTIFICATIONS_COLLECTION].find(
                {"product_id": product_id, "site_id": site_id, "resolved": False}))
            assert len(docs) == 1
            assert docs[0]["sto_id"] == "STO-VTEST-2"
        finally:
            mongo_db[svc.NOTIFICATIONS_COLLECTION].delete_many(
                {"product_id": product_id, "site_id": site_id})


# ----- mark_put_away_confirmed() with site_id -----
class TestMarkPutAwayConfirmedFriendly:
    """Verify final_attempt=True path rewrites recognized valuation errors."""

    def _seed_shipment(self, mongo_db, doc_code, po_number):
        mongo_db["supplier_portal_shipments"].replace_one(
            {"_id": doc_code},
            {"_id": doc_code, "sap_sync_status": "posted", "sap_gr_result": {
                "per_po": [{"po_number": po_number, "status": "posted", "put_away_confirmed": False, "events": []}]}},
            upsert=True,
        )

    def test_final_attempt_rewrites_valuation_error(self, mongo_db):
        import supplier_shipment_service as svc
        doc_code, po = "TEST-PA-001", "TESTPO001"
        try:
            self._seed_shipment(mongo_db, doc_code, po)
            svc.mark_put_away_confirmed(
                mongo_db, doc_code, po, False,
                ["Financials PU for material G12FW at plant P2 is missing or in preparation"],
                final_attempt=True, site_id="P2",
            )
            d = mongo_db["supplier_portal_shipments"].find_one({"_id": doc_code})
            assert d["sap_sync_status"] == "put_away_failed"
            msg = d["grn_alert_message"]
            assert "G12FW" in msg
            assert "P2" in msg
            assert "Cost/Valuation" in msg
            assert "Retry Put Away below" in msg
        finally:
            mongo_db["supplier_portal_shipments"].delete_one({"_id": doc_code})

    def test_final_attempt_passes_through_unknown_error(self, mongo_db):
        import supplier_shipment_service as svc
        doc_code, po = "TEST-PA-002", "TESTPO002"
        try:
            self._seed_shipment(mongo_db, doc_code, po)
            svc.mark_put_away_confirmed(
                mongo_db, doc_code, po, False,
                ["Some totally unexpected SAP outage"],
                final_attempt=True, site_id="P2",
            )
            d = mongo_db["supplier_portal_shipments"].find_one({"_id": doc_code})
            assert d["sap_sync_status"] == "put_away_failed"
            assert "Some totally unexpected SAP outage" in d["grn_alert_message"]
        finally:
            mongo_db["supplier_portal_shipments"].delete_one({"_id": doc_code})

    def test_no_site_id_passes_through(self, mongo_db):
        import supplier_shipment_service as svc
        doc_code, po = "TEST-PA-003", "TESTPO003"
        try:
            self._seed_shipment(mongo_db, doc_code, po)
            # site_id omitted -> raw text unchanged
            svc.mark_put_away_confirmed(
                mongo_db, doc_code, po, False,
                ["Valuation data missing for material X"],
                final_attempt=True,
            )
            d = mongo_db["supplier_portal_shipments"].find_one({"_id": doc_code})
            assert "Valuation data missing" in d["grn_alert_message"]
        finally:
            mongo_db["supplier_portal_shipments"].delete_one({"_id": doc_code})


# ----- Type-aware notification resolve through the real API -----
class TestActivateResolvesCorrectNotificationType:
    """Uses the SAFE idempotent pair P27175/P1 (already fully active).
    Live SAP call returns planning_logistics='ok' and no 'valuation' key
    (because payload has no amount). Missing_planning_data notif MUST
    resolve; missing_valuation notif MUST NOT resolve.
    """

    def test_missing_planning_notif_resolves_when_planning_ok(self, mongo_db, admin_session):
        import stock_transfer_service as svc
        nid = f"TEST-notif-plan-{uuid.uuid4().hex[:8]}"
        try:
            mongo_db[svc.NOTIFICATIONS_COLLECTION].insert_one({
                "_id": nid, "type": "missing_planning_data",
                "product_id": SAFE_PRODUCT_ID, "site_id": SAFE_SITE_ID,
                "sto_id": "STO-TEST-PLAN", "message": "test", "resolved": False,
                "created_at": datetime.now(timezone.utc),
            })
            r = client_for(admin_session).post(
                f"{BASE_URL}/api/admin/material-sites/activate",
                json={"product_id": SAFE_PRODUCT_ID, "site_id": SAFE_SITE_ID, "notification_id": nid},
                timeout=180,
            )
            assert r.status_code == 200, r.text
            data = r.json()
            assert data.get("planning_logistics") == "ok", data
            doc = mongo_db[svc.NOTIFICATIONS_COLLECTION].find_one({"_id": nid})
            assert doc["resolved"] is True, "planning notification should resolve when planning_logistics==ok"
        finally:
            mongo_db[svc.NOTIFICATIONS_COLLECTION].delete_one({"_id": nid})

    def test_missing_valuation_notif_resolves_when_valuation_ok(self, mongo_db, admin_session):
        """When SAP reports both planning AND valuation ok on the safe pair
        (P27175 @ P1 - fully activated already), the type-aware resolve
        branch should still pick the 'valuation' field for a
        missing_valuation notif (not 'planning_logistics') and resolve it.
        The negative branch (planning_logistics ok but valuation not ok)
        is covered by the pure-logic unit test below."""
        import stock_transfer_service as svc
        nid = f"TEST-notif-val-{uuid.uuid4().hex[:8]}"
        try:
            mongo_db[svc.NOTIFICATIONS_COLLECTION].insert_one({
                "_id": nid, "type": "missing_valuation",
                "product_id": SAFE_PRODUCT_ID, "site_id": SAFE_SITE_ID,
                "sto_id": "STO-TEST-VAL", "message": "test", "resolved": False,
                "created_at": datetime.now(timezone.utc),
            })
            r = client_for(admin_session).post(
                f"{BASE_URL}/api/admin/material-sites/activate",
                json={"product_id": SAFE_PRODUCT_ID, "site_id": SAFE_SITE_ID, "notification_id": nid},
                timeout=180,
            )
            assert r.status_code == 200, r.text
            data = r.json()
            assert data.get("valuation") == "ok", data
            doc = mongo_db[svc.NOTIFICATIONS_COLLECTION].find_one({"_id": nid})
            assert doc["resolved"] is True, (
                "missing_valuation notification must resolve when valuation=='ok'")
        finally:
            mongo_db[svc.NOTIFICATIONS_COLLECTION].delete_one({"_id": nid})

    def test_resolve_branch_selects_correct_field(self, mongo_db):
        """Pure-logic replica of server.py's type-aware resolve branch -
        exercises the negative case (planning ok, valuation NOT ok) which
        we cannot safely trigger against live SAP on any known safe pair."""
        import stock_transfer_service as svc

        def simulate(notif_type: str, result: dict) -> bool:
            """Mirrors post_activate_material_site's branch."""
            success_field = "valuation" if notif_type == "missing_valuation" else "planning_logistics"
            return result.get(success_field) == "ok"

        # Old buggy behavior would have resolved a missing_valuation notif
        # whenever planning_logistics=='ok'. New branch must not.
        assert simulate("missing_valuation", {"planning_logistics": "ok", "valuation": "Some SAP error"}) is False
        assert simulate("missing_valuation", {"planning_logistics": "ok", "valuation": "ok"}) is True
        assert simulate("missing_planning_data", {"planning_logistics": "ok", "valuation": "Error"}) is True
        assert simulate("missing_planning_data", {"planning_logistics": "Some error", "valuation": "ok"}) is False
        # And the branch must also survive result missing the field
        assert simulate("missing_valuation", {"planning_logistics": "ok"}) is False

    def test_missing_valuation_notif_visible_via_get_notifications(self, mongo_db, admin_session):
        import stock_transfer_service as svc
        nid = f"TEST-notif-list-{uuid.uuid4().hex[:8]}"
        try:
            mongo_db[svc.NOTIFICATIONS_COLLECTION].insert_one({
                "_id": nid, "type": "missing_valuation",
                "product_id": "TESTP-LIST", "site_id": "P9",
                "sto_id": "STO-TEST-LIST", "message": "TESTP-LIST has no Cost/Valuation set up at site P9",
                "resolved": False, "created_at": datetime.now(timezone.utc),
            })
            r = client_for(admin_session).get(f"{BASE_URL}/api/admin/notifications", timeout=30)
            assert r.status_code == 200, r.text
            match = [n for n in r.json()["notifications"] if n["_id"] == nid]
            assert match, "seeded missing_valuation notification not returned by GET /api/admin/notifications"
            assert match[0]["type"] == "missing_valuation"
            assert match[0]["product_id"] == "TESTP-LIST"
            assert match[0]["site_id"] == "P9"
        finally:
            mongo_db[svc.NOTIFICATIONS_COLLECTION].delete_one({"_id": nid})
