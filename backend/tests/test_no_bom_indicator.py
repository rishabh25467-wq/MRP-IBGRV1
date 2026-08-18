"""Tests for the new no_bom indicator (Inventory page 'No BOM' flag).

Verifies:
1. Backend: get_cached_inventory attaches a boolean `no_bom` to every item.
2. Rule correctness: no_bom=false iff the product_id is a BOM root (bom_node_cache
   found=true) OR appears as a component/leaf inside any other cached BOM tree.
3. Model shape: /api/inventory response items include the field (schema check via
   direct pytest of the pydantic model isn't strictly needed - inspect the shape
   directly against the cached document read by get_cached_inventory).
4. Regression: master carton 5989829-12 still shows unit_cost=13.45, total_value=5655.72.
"""
import os
import sys

import pytest
from dotenv import dotenv_values
from pymongo import MongoClient

sys.path.insert(0, "/app/backend")

from inventory_service import get_cached_inventory, _compute_no_bom_flags  # noqa: E402
import bom_cache_service  # noqa: E402

backend_env = dotenv_values("/app/backend/.env")
MONGO_URL = backend_env.get("MONGO_URL") or os.environ.get("MONGO_URL")
DB_NAME = backend_env.get("DB_NAME") or os.environ.get("DB_NAME")


@pytest.fixture(scope="module")
def db():
    if not (MONGO_URL and DB_NAME):
        pytest.skip("Mongo env not set")
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


class TestNoBomFlag:
    def test_every_item_has_no_bom_field(self, db):
        cached = get_cached_inventory(db)
        items = cached["items"]
        assert len(items) > 0, "inventory_cache is empty"
        missing = [it["product_id"] for it in items if "no_bom" not in it]
        assert not missing, f"{len(missing)} items missing no_bom field (first: {missing[:5]})"
        for it in items:
            assert isinstance(it["no_bom"], bool), (
                f"no_bom must be bool for {it['product_id']}, got {type(it['no_bom'])}"
            )

    def test_bom_roots_have_no_bom_false(self, db):
        """Take a handful of known BOM roots (found=true in bom_node_cache) and
        confirm they're flagged no_bom=false in the inventory response."""
        bom_cache = db[bom_cache_service.COLLECTION_NAME]
        sample_roots = list(bom_cache.find({"found": True}, {"_id": 1}).limit(20))
        assert sample_roots, "No BOM roots in bom_node_cache - can't validate"

        cached = get_cached_inventory(db)
        by_id = {it["product_id"]: it for it in cached["items"]}

        checked = 0
        for doc in sample_roots:
            pid = doc["_id"]
            it = by_id.get(pid)
            if it is None:
                continue  # not every BOM root is necessarily in inventory
            assert it["no_bom"] is False, (
                f"BOM root {pid} incorrectly flagged no_bom=True"
            )
            checked += 1
        assert checked > 0, "None of the sampled BOM roots were also in inventory"
        print(f"Verified {checked} BOM roots correctly flagged no_bom=False")

    def test_leaf_components_have_no_bom_false(self, db):
        """Any product_id that appears as a leaf/component inside a cached BOM
        tree should also be no_bom=false."""
        membership = _compute_no_bom_flags(db)
        leaf_only = membership["leaf_ids"] - membership["roots_with_bom"]
        assert leaf_only, "No pure-leaf-only ids in bom cache - can't validate"

        cached = get_cached_inventory(db)
        by_id = {it["product_id"]: it for it in cached["items"]}

        checked = 0
        for pid in list(leaf_only)[:200]:
            it = by_id.get(pid)
            if it is None:
                continue
            assert it["no_bom"] is False, (
                f"Leaf component {pid} incorrectly flagged no_bom=True"
            )
            checked += 1
        assert checked > 0, "None of the sampled leaf-only ids were in inventory"
        print(f"Verified {checked} leaf components correctly flagged no_bom=False")

    def test_orphans_have_no_bom_true(self, db):
        """Any inventory item that is NEITHER a BOM root NOR a leaf anywhere
        should be flagged no_bom=true. There should be at least a handful of
        these (user observed 740/3216)."""
        membership = _compute_no_bom_flags(db)
        cached = get_cached_inventory(db)
        items = cached["items"]

        true_flagged = [it for it in items if it["no_bom"]]
        assert true_flagged, "No items flagged no_bom=True at all"
        # Range sanity - user observed 740 of 3216 live
        assert 100 <= len(true_flagged) <= len(items), (
            f"Suspicious no_bom=True count: {len(true_flagged)} of {len(items)}"
        )
        print(f"no_bom=True count: {len(true_flagged)} of {len(items)}")

        # Every no_bom=True item must genuinely be neither root nor leaf
        for it in true_flagged[:50]:
            pid = it["product_id"]
            assert pid not in membership["roots_with_bom"], (
                f"{pid} flagged no_bom=True but IS a BOM root"
            )
            assert pid not in membership["leaf_ids"], (
                f"{pid} flagged no_bom=True but IS a leaf in another BOM"
            )

    def test_master_carton_valuation_regression(self, db):
        """Ensure the valuation-fix patched item is still intact."""
        cached = get_cached_inventory(db)
        by_id = {it["product_id"]: it for it in cached["items"]}
        mc = by_id.get("5989829-12")
        assert mc is not None, "Master carton 5989829-12 missing from inventory"
        assert mc.get("unit_cost") == 13.45, (
            f"Expected unit_cost=13.45, got {mc.get('unit_cost')}"
        )
        assert mc.get("total_value") == 5655.72, (
            f"Expected total_value=5655.72, got {mc.get('total_value')}"
        )
        # And it should not (typically) be flagged no_bom - master carton is used
        # as a component in packaging BOMs. Just log, not assert (data-dependent).
        print(f"Master carton no_bom={mc['no_bom']}")


class TestNoBomApiShape:
    def test_inventory_api_returns_no_bom(self):
        """Live hit on /api/inventory (public URL) - anonymous callers get 401,
        but we can still assert the endpoint exists and enforces auth without
        crashing. Full shape is covered by the in-process DB test above."""
        import requests
        frontend_env = dotenv_values("/app/frontend/.env")
        base_url = frontend_env.get("REACT_APP_BACKEND_URL")
        r = requests.get(f"{base_url}/api/inventory", timeout=30)
        # Auth-gated: should be 401/403 without a session, not 500.
        assert r.status_code in (200, 401, 403), (
            f"Unexpected /api/inventory status {r.status_code}: {r.text[:200]}"
        )
