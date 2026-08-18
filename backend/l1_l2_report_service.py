"""Cross-catalog Level-1 / Level-2 BOM line item report.

Answers "for every material in SAP, what are its immediate (L1) and
grandchild (L2) BOM components, their description, and their quantity" -
a flattened, two-level-deep view across the WHOLE catalog rather than one
BOM at a time like BOM Explorer. Built on top of the existing
bom_node_cache (see bom_cache_service.py): reuses whatever's already been
explored, live-fetches (in batched passes, not one call at a time) and
persists anything never seen before, so a second run is near-instant.
"""
import logging
from datetime import datetime, timezone

import bom_cache_service
from bom_cache_service import COLLECTION_NAME as BOM_CACHE_COLLECTION

logger = logging.getLogger(__name__)

REPORT_CACHE_COLLECTION = "l1_l2_report_cache"
REPORT_CACHE_ID = "latest"

# SAP's SOAP response reports the quantity's unit as a generic dimension
# code (MASS/LENGTH) rather than a concrete unit like "kg" for many bulk
# raw materials - map to the friendly label users actually expect to see
# (matches SAP's own native Multi-Level BOM Structure export style).
_FRIENDLY_UOM = {"MASS": "kg", "LENGTH": "m", "EA": "ea", "SET": "set"}


def _friendly_uom(code):
    return _FRIENDLY_UOM.get(code, code)


def build_l1_l2_report(sap_soap_client, sap_inventory_client, db) -> dict:
    """Target root universe = every distinct product_id in SAP's own
    On-Hand Inventory feed, unioned with every product_id already known to
    be a BOM root in bom_node_cache (covers roots that hold zero stock
    right now, e.g. pure make-to-order sub-assemblies). Persists the
    compiled result to REPORT_CACHE_COLLECTION so get_cached_report() is
    instant afterward."""
    bom_collection = db[BOM_CACHE_COLLECTION]

    inventory_ids = {row["product_id"] for row in sap_inventory_client.get_inventory_detail() if row.get("product_id")}
    known_root_ids = {d["_id"] for d in bom_collection.find({"found": True}, {"_id": 1})}
    target_ids = sorted(inventory_ids | known_root_ids)

    # Level 0 -> Level 1: warm the cache for every target root in one
    # batched pass (skips anything already cached).
    bom_cache_service.bulk_prefetch(target_ids, sap_soap_client, db)
    root_docs = {d["_id"]: d for d in bom_collection.find({"_id": {"$in": target_ids}})}

    l1_rows = []
    l1_ids_needed = set()
    for root_id in target_ids:
        doc = root_docs.get(root_id)
        if not doc or not doc.get("found"):
            continue
        for group in doc.get("groups", []):
            for item in group.get("items", []):
                if not item.get("active", True):
                    continue
                l1_rows.append({
                    "root_product_id": root_id,
                    "parent_product_id": root_id,
                    "level": 1,
                    "product_id": item["product_id"],
                    "description": item.get("description"),
                    "quantity": item.get("quantity"),
                    "unit_of_measure": _friendly_uom(item.get("unit_of_measure")),
                    "line_item_group_id": group.get("group_id"),
                    "line_item_id": item.get("item_id"),
                })
                l1_ids_needed.add(item["product_id"])

    # Level 1 -> Level 2: warm+read the cache once for every UNIQUE L1 item
    # across ALL roots combined (heavy overlap expected - common hardware/
    # packaging/raw materials feed into many different finished goods, so
    # this is far cheaper than expanding L2 per-root independently).
    l1_ids_needed = sorted(l1_ids_needed)
    bom_cache_service.bulk_prefetch(l1_ids_needed, sap_soap_client, db)
    l1_docs = {d["_id"]: d for d in bom_collection.find({"_id": {"$in": l1_ids_needed}})}

    l2_rows = []
    for l1_row in l1_rows:
        l1_doc = l1_docs.get(l1_row["product_id"])
        if not l1_doc or not l1_doc.get("found"):
            continue
        for group in l1_doc.get("groups", []):
            for item in group.get("items", []):
                if not item.get("active", True):
                    continue
                l2_rows.append({
                    "root_product_id": l1_row["root_product_id"],
                    "parent_product_id": l1_row["product_id"],
                    "level": 2,
                    "product_id": item["product_id"],
                    "description": item.get("description"),
                    "quantity": item.get("quantity"),
                    "unit_of_measure": _friendly_uom(item.get("unit_of_measure")),
                    "line_item_group_id": group.get("group_id"),
                    "line_item_id": item.get("item_id"),
                })

    items = l1_rows + l2_rows
    # Sort by root then level so a root's L1 and L2 rows sit together -
    # otherwise all 5000+ L1 rows come first and L2 rows are invisible in
    # the UI's default (unfiltered, capped) table view.
    items.sort(key=lambda row: (row["root_product_id"], row["level"], row["product_id"]))
    now = datetime.now(timezone.utc)
    doc = {
        "_id": REPORT_CACHE_ID,
        "items": items,
        "roots_scanned": len(target_ids),
        "roots_with_bom": len({row["root_product_id"] for row in l1_rows}),
        "updated_at": now,
    }
    db[REPORT_CACHE_COLLECTION].replace_one({"_id": REPORT_CACHE_ID}, doc, upsert=True)
    return {"items": items, "roots_scanned": doc["roots_scanned"], "roots_with_bom": doc["roots_with_bom"], "updated_at": now}


def get_cached_report(db) -> dict:
    doc = db[REPORT_CACHE_COLLECTION].find_one({"_id": REPORT_CACHE_ID})
    if not doc:
        return {"items": [], "roots_scanned": 0, "roots_with_bom": 0, "updated_at": None}
    return {
        "items": doc.get("items", []),
        "roots_scanned": doc.get("roots_scanned", 0),
        "roots_with_bom": doc.get("roots_with_bom", 0),
        "updated_at": doc.get("updated_at"),
    }
