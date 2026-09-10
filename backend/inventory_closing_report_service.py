"""Historical closing-inventory report (Sep 2026, user's explicit ask):
"31st Aug inventory data, company/plant wise Qty with value, Item code,
Item name" - built from SAP's own historical Material Inventories -
Balance Summary report (see sap_inventory_closing_client.py) rather than
this app's live-only inventory_cache. Flat rows + a single Total row at
the end (user's explicit choice, not per-site subtotals).
"""
import logging
from datetime import datetime, timezone

import inventory_service

logger = logging.getLogger(__name__)


def build_closing_inventory_report(sap_inventory_closing_client, db, key_date: str) -> dict:
    site_ids = inventory_service.list_known_sites(db)
    rows = sap_inventory_closing_client.get_closing_inventory(key_date, site_ids)
    rows.sort(key=lambda r: (r["site_id"], r["product_id"]))
    total_qty = sum(r["qty"] for r in rows)
    total_value = sum(r["value"] for r in rows)
    return {
        "key_date": key_date,
        "rows": rows,
        "total_qty": total_qty,
        "total_value": total_value,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
