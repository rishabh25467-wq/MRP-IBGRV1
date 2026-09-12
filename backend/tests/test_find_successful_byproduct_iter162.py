"""Iteration 162: unit tests for find_successful_byproduct_confirmation
(Sep 12 2026 bug fix - real Lot 72722 by-product over-post incident)."""
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/app/backend")

import pytest
from pymongo import MongoClient

import production_confirmation_service as pcs

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

TEST_LOT = "TESTLOT_ITER162_A"
TEST_LOT_B = "TESTLOT_ITER162_B"
TEST_RP = "RP-TEST-162-10"
TEST_RP_OTHER = "RP-TEST-162-20"


@pytest.fixture
def db():
    client = MongoClient(MONGO_URL)
    d = client[DB_NAME]
    d[pcs.HISTORY_COLLECTION].delete_many({"production_lot_id": {"$in": [TEST_LOT, TEST_LOT_B]}})
    yield d
    d[pcs.HISTORY_COLLECTION].delete_many({"production_lot_id": {"$in": [TEST_LOT, TEST_LOT_B]}})
    client.close()


def _insert(db, lot, rp, success, bp_success, at, bp_uuid=None, new_bp_pid=None):
    doc = {
        "production_lot_id": lot,
        "reporting_point_id": rp,
        "success": success,
        "byproduct_material_output_uuid": bp_uuid,
        "new_byproduct_product_id": new_bp_pid,
        "byproduct_confirmation": ({"success": bp_success, "logs": [{"note": "ok"}]}
                                    if bp_success is not None else None),
        "at": at,
    }
    db[pcs.HISTORY_COLLECTION].insert_one(doc)


NOW = datetime.now(timezone.utc)


def test_returns_bp_when_main_failed_and_bp_succeeded_matching_uuid(db):
    _insert(db, TEST_LOT, TEST_RP, success=False, bp_success=True,
            at=NOW, bp_uuid="uuid-A")
    result = pcs.find_successful_byproduct_confirmation(db, TEST_LOT, TEST_RP, byproduct_material_output_uuid="uuid-A")
    assert result is not None
    assert result["success"] is True


def test_returns_none_for_different_uuid(db):
    _insert(db, TEST_LOT, TEST_RP, success=False, bp_success=True,
            at=NOW, bp_uuid="uuid-A")
    result = pcs.find_successful_byproduct_confirmation(db, TEST_LOT, TEST_RP, byproduct_material_output_uuid="uuid-DIFFERENT")
    assert result is None


def test_returns_none_for_different_reporting_point(db):
    _insert(db, TEST_LOT, TEST_RP, success=False, bp_success=True,
            at=NOW, bp_uuid="uuid-A")
    result = pcs.find_successful_byproduct_confirmation(db, TEST_LOT, TEST_RP_OTHER, byproduct_material_output_uuid="uuid-A")
    assert result is None


def test_returns_none_when_latest_main_succeeded(db):
    # Older attempt: main failed, bp succeeded
    _insert(db, TEST_LOT, TEST_RP, success=False, bp_success=True,
            at=NOW - timedelta(minutes=5), bp_uuid="uuid-A")
    # Newer attempt: main finally succeeded
    _insert(db, TEST_LOT, TEST_RP, success=True, bp_success=True,
            at=NOW, bp_uuid="uuid-A")
    result = pcs.find_successful_byproduct_confirmation(db, TEST_LOT, TEST_RP, byproduct_material_output_uuid="uuid-A")
    assert result is None


def test_returns_none_when_no_history(db):
    result = pcs.find_successful_byproduct_confirmation(db, TEST_LOT, TEST_RP)
    assert result is None


def test_returns_none_when_bp_did_not_succeed(db):
    _insert(db, TEST_LOT, TEST_RP, success=False, bp_success=False,
            at=NOW, bp_uuid="uuid-A")
    result = pcs.find_successful_byproduct_confirmation(db, TEST_LOT, TEST_RP, byproduct_material_output_uuid="uuid-A")
    assert result is None


def test_new_byproduct_product_id_matching(db):
    _insert(db, TEST_LOT, TEST_RP, success=False, bp_success=True,
            at=NOW, new_bp_pid="IRON-SCR")
    # Matching new_byproduct_product_id -> reuse
    r1 = pcs.find_successful_byproduct_confirmation(db, TEST_LOT, TEST_RP, new_byproduct_product_id="IRON-SCR")
    assert r1 is not None
    # Different -> None
    r2 = pcs.find_successful_byproduct_confirmation(db, TEST_LOT, TEST_RP, new_byproduct_product_id="OTHER-SCR")
    assert r2 is None


def test_only_considers_latest_record(db):
    # Very old success (irrelevant)
    _insert(db, TEST_LOT, TEST_RP, success=True, bp_success=True,
            at=NOW - timedelta(hours=2), bp_uuid="uuid-A")
    # Latest: main failed, bp succeeded
    _insert(db, TEST_LOT, TEST_RP, success=False, bp_success=True,
            at=NOW, bp_uuid="uuid-A")
    result = pcs.find_successful_byproduct_confirmation(db, TEST_LOT, TEST_RP, byproduct_material_output_uuid="uuid-A")
    assert result is not None


def test_log_confirmation_stores_new_byproduct_product_id(db):
    payload = {
        "production_lot_id": TEST_LOT_B, "reporting_point_id": TEST_RP,
        "new_byproduct_product_id": "SOME-SCRAP",
    }
    result = {"success": False, "byproduct_confirmation": {"success": True}}
    pcs.log_confirmation(db, "tester", payload, result)
    doc = db[pcs.HISTORY_COLLECTION].find_one({"production_lot_id": TEST_LOT_B})
    assert doc["new_byproduct_product_id"] == "SOME-SCRAP"
