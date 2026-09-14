"""Iteration 170 - Sep 14 2026 fix verification.

Real incident: 6800-003989-270 ("KNOB, 1/4-20 STUD") has a cached BOM
in SAP (Knob + Steel Insert) but 6,612 EA sits in the RM warehouse -
clearly bought/stocked as a complete unit. `is_sub_assembly` used to
ALWAYS block Store Request creation for anything with a cached BOM,
even after the user manually re-categorized it away from "Sub-Assembly"
(category_source="manual") - the category was never wired into that
check at all.
"""
import sys
sys.path.insert(0, "/app/backend")
from pymongo import MongoClient
from dotenv import dotenv_values
import production_confirmation_service as pcs

backend_env = dotenv_values("/app/backend/.env")
client = MongoClient(backend_env["MONGO_URL"])
db = client[backend_env["DB_NAME"]]

BOM_ID = "TEST_SUBASM_170"
PLAIN_ID = "TEST_PLAIN_170"


def setup_module(module):
    db["bom_node_cache"].update_one(
        {"_id": BOM_ID},
        {"$set": {"found": True, "groups": [{"group_id": "10", "items": [{"item_id": "10", "product_id": "X"}]}]}},
        upsert=True,
    )
    db["component_master"].delete_many({"_id": {"$in": [BOM_ID, PLAIN_ID]}})


def teardown_module(module):
    db["bom_node_cache"].delete_one({"_id": BOM_ID})
    db["component_master"].delete_many({"_id": {"$in": [BOM_ID, PLAIN_ID]}})


def test_bom_item_treated_as_sub_assembly_by_default():
    result = pcs._sub_assembly_ids(db, [BOM_ID, PLAIN_ID])
    assert BOM_ID in result
    assert PLAIN_ID not in result


def test_manual_override_away_from_sub_assembly_wins():
    db["component_master"].update_one(
        {"_id": BOM_ID}, {"$set": {"category": "Hardware", "category_source": "manual"}}, upsert=True,
    )
    try:
        result = pcs._sub_assembly_ids(db, [BOM_ID, PLAIN_ID])
        assert BOM_ID not in result
    finally:
        db["component_master"].delete_one({"_id": BOM_ID})


def test_rule_or_ai_categorized_still_blocked():
    db["component_master"].update_one(
        {"_id": BOM_ID}, {"$set": {"category": "Hardware", "category_source": "rule"}}, upsert=True,
    )
    try:
        result = pcs._sub_assembly_ids(db, [BOM_ID, PLAIN_ID])
        assert BOM_ID in result, "rule/ai categorization must not override the BOM heuristic - only manual should"
    finally:
        db["component_master"].delete_one({"_id": BOM_ID})


def test_manual_override_still_sub_assembly_if_category_is_sub_assembly():
    db["component_master"].update_one(
        {"_id": BOM_ID}, {"$set": {"category": "Sub-Assembly", "category_source": "manual"}}, upsert=True,
    )
    try:
        result = pcs._sub_assembly_ids(db, [BOM_ID, PLAIN_ID])
        assert BOM_ID in result
    finally:
        db["component_master"].delete_one({"_id": BOM_ID})
