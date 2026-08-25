"""Company/Address cache for the legacy ERP's `comp` table (Aug 27 2026,
user's explicit ask: "company erp address table also store in mongo
since it does not change") - refreshed periodically in the background
(see server.py's refresh loop) instead of a live MS SQL query on every
Delivery Note/Gate Pass print. A one-off live fallback covers a site
that's somehow missing from the cache (e.g. right after a brand new
site is added to `comp`, before the next scheduled refresh runs)."""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

COMPANY_CACHE_COLLECTION = "erp_company_cache"


def refresh_company_cache(db, erp_portal_client) -> dict:
    companies = erp_portal_client.get_all_company_info()
    db[COMPANY_CACHE_COLLECTION].update_one(
        {"_id": "latest"}, {"$set": {"companies": companies, "updated_at": datetime.now(timezone.utc)}}, upsert=True,
    )
    return companies


def get_cached_company_info(db, codes: list, erp_portal_client=None) -> dict:
    codes = [(c or "").strip().upper() for c in codes if c]
    doc = db[COMPANY_CACHE_COLLECTION].find_one({"_id": "latest"}) or {}
    companies = doc.get("companies") or {}
    missing = [c for c in codes if c not in companies]
    if missing and erp_portal_client:
        try:
            fresh = erp_portal_client.get_company_info(missing)
        except Exception as e:
            logger.warning(f"Company cache: live fallback fetch failed for {missing}: {e}")
            fresh = {}
        if fresh:
            companies.update(fresh)
            db[COMPANY_CACHE_COLLECTION].update_one(
                {"_id": "latest"},
                {"$set": {**{f"companies.{k}": v for k, v in fresh.items()}, "updated_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
    return {c: companies[c] for c in codes if c in companies}
