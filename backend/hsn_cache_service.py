"""Persistent, no-expiry HSN Code cache (Aug 27 2026, user's explicit
ask: "store hsn in cache or mongo at once") - unlike inventory_cache or
comp/company data, an HSN Code never changes once maintained in SAP for
a material, so once looked up for a product it is cached FOREVER here -
no periodic refresh job needed. This turns the "Add Item" step on the
Stock Transfer form (previously a live SAP Business Analytics report
call on every single add) into a live SAP call only the FIRST time any
given product is ever touched anywhere in the app, ever again after
that it's a plain Mongo read.

A product with no HSN code maintained in SAP yet is deliberately NOT
cached (so it's safely retried live next time, in case someone
maintains it in SAP later) - only a real, non-blank code is ever
persisted.
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

HSN_CACHE_COLLECTION = "hsn_code_cache"


def get_hsn_codes_cached(db, product_ids: list, sap_hsn_client=None) -> dict:
    """Returns {product_id: hsn_code} - Mongo cache first, live SAP only
    for whatever product_ids aren't cached yet (and only if a client was
    given; callers with no SAP access on hand just get the cached subset)."""
    product_ids = list({(p or "").strip().upper() for p in product_ids if p})
    if not product_ids:
        return {}

    codes = {d["_id"]: d["hsn_code"] for d in db[HSN_CACHE_COLLECTION].find({"_id": {"$in": product_ids}})}
    missing = [p for p in product_ids if p not in codes]
    if missing and sap_hsn_client:
        try:
            fresh = sap_hsn_client.get_hsn_codes(missing)
        except Exception as e:
            logger.warning(f"HSN cache: live SAP lookup failed for {len(missing)} product(s), returning cached-only result: {e}")
            fresh = {}
        now = datetime.now(timezone.utc)
        for product_id, code in fresh.items():
            if code:
                db[HSN_CACHE_COLLECTION].update_one({"_id": product_id}, {"$set": {"hsn_code": code, "updated_at": now}}, upsert=True)
                codes[product_id] = code
    return codes
