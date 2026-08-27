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
    """Aug 27 2026 bug fix (real incident, Delivery Note print for
    Ship-from Site "P2W" came out with GSTIN/PAN blank): SAP's own real
    Site ID for a couple of warehouses does NOT match that same
    company's `Ccode` in the legacy ERP's `comp` table - e.g. SAP calls
    this site "P2W" (confirmed live: inventory_cache location strings
    literally say "RADISH TECHNOLOGIES-P2W"), but `comp.Ccode` for that
    same physical warehouse is "W2", with "P2W" only present as THAT
    row's `pcode` column. A direct `codes` vs `companies` dict-key match
    therefore silently misses it. Falls back to matching each requested
    code against every cached company's OWN `pcode` field before giving
    up on it."""
    codes = [(c or "").strip().upper() for c in codes if c]
    doc = db[COMPANY_CACHE_COLLECTION].find_one({"_id": "latest"}) or {}
    companies = doc.get("companies") or {}
    pcode_index = {(v.get("pcode") or "").strip().upper(): v for v in companies.values() if v.get("pcode")}

    def _resolve(code):
        return companies.get(code) or pcode_index.get(code)

    missing = [c for c in codes if _resolve(c) is None]
    if missing and erp_portal_client:
        try:
            fresh = erp_portal_client.get_company_info(missing)
        except Exception as e:
            logger.warning(f"Company cache: live fallback fetch failed for {missing}: {e}")
            fresh = {}
        if fresh:
            companies.update(fresh)
            pcode_index.update({(v.get("pcode") or "").strip().upper(): v for v in fresh.values() if v.get("pcode")})
            db[COMPANY_CACHE_COLLECTION].update_one(
                {"_id": "latest"},
                {"$set": {**{f"companies.{k}": v for k, v in fresh.items()}, "updated_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
    return {c: _resolve(c) for c in codes if _resolve(c) is not None}
