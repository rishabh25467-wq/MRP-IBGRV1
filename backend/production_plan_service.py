"""Production's resolution of genuine BOM alternates - see
sap_soap_client._parse for how an "alternate" is detected (multiple
currently-Consistent BOM revisions for the same product that use DIFFERENT
raw materials, not just an administrative revision chain).

Purchasing can't make this call - only production knows which material a
given run will actually use, so this is a standing, global-per-component
decision (not per parent-product context, not per purchasing-plan-run) that
production sets once here and every BOM explosion (BOM Explorer, Purchasing
Plan, Inventory backfill) then respects via bom_cache_service._fetch_live's
override check, until changed."""
from datetime import datetime, timezone

import bom_cache_service

OVERRIDES_COLLECTION_NAME = bom_cache_service.OVERRIDES_COLLECTION_NAME


def list_alternates(db) -> list:
    """Every product/sub-assembly currently known to have genuine BOM
    alternates (from whatever's already been explored via BOM Explorer,
    Purchasing Plan, or the periodic BOM cache refresh - a product never
    looked up yet simply won't show here until it is). Returns
    [{product_id, options: [{bom_id, description, sample_item_id,
    sample_item_description}], default_bom_id, resolved_bom_id}] sorted
    with unresolved items first, then by product_id."""
    docs = db[bom_cache_service.COLLECTION_NAME].find(
        {"alternates.1": {"$exists": True}}, {"bom_id": 1, "alternates": 1}
    )
    candidates = list(docs)
    if not candidates:
        return []

    product_ids = [d["_id"] for d in candidates]
    overrides = {
        o["_id"]: o["chosen_bom_id"]
        for o in db[OVERRIDES_COLLECTION_NAME].find({"_id": {"$in": product_ids}}, {"chosen_bom_id": 1})
    }

    results = []
    for d in candidates:
        results.append({
            "product_id": d["_id"],
            "options": d.get("alternates", []),
            "default_bom_id": d.get("bom_id"),
            "resolved_bom_id": overrides.get(d["_id"]),
        })
    results.sort(key=lambda r: (r["resolved_bom_id"] is not None, r["product_id"]))
    return results


def resolve_alternate(db, product_id: str, chosen_bom_id: str):
    """Production's choice for this component - persists globally, applies
    everywhere this component is used, until changed or cleared."""
    db[OVERRIDES_COLLECTION_NAME].update_one(
        {"_id": product_id},
        {"$set": {"chosen_bom_id": chosen_bom_id, "chosen_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


def clear_alternate_resolution(db, product_id: str):
    """Revert to the default (highest-revision) selection."""
    db[OVERRIDES_COLLECTION_NAME].delete_one({"_id": product_id})
